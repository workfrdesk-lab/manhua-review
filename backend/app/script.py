"""Authenticated, chapter-owned script generation and review endpoints."""

import json
import re
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth import require_user
from app.db import get_db
from app.jobs import get_job_queue
from app.models import (
    OCRResult,
    Page,
    Panel,
    ScriptEvidence,
    ScriptGenerationJob,
    ScriptSegment,
    ScriptVersion,
    StoryAudit,
    StoryVersion,
    User,
)
from app.projects import owned_chapter, protect_write
from app.script_providers import configured_script_provider, profile_fingerprint
from app.script_schemas import GenerateScriptInput, ScriptProfile, ScriptStructuredOutput
from app.script_validation import validate_script
from app.story_providers import ProviderNotConfigured
from app.story_review import ReviewState
from app.story_schemas import StoryStructuredOutput

router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


class ScriptStatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(needs_review|confirmed|rejected)$")


class SegmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration_text: str | None = Field(default=None, min_length=1, max_length=5000)
    dialogue_text: str | None = Field(default=None, min_length=1, max_length=1000)
    sequence: int | None = Field(default=None, ge=1)
    estimated_duration: float | None = Field(default=None, gt=0, le=3600)
    confidence: float | None = Field(default=None, ge=0, le=1)
    speaker_ref: str | None = Field(default=None, min_length=1, max_length=100)
    source_dialogue: bool | None = None
    segment_type: str | None = Field(
        default=None, pattern="^(narration|dialogue|transition|intro|outro)$"
    )


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", min_length=1, max_length=300)
    hook: str = Field(default="", max_length=5000)
    intro: str = Field(default="", max_length=5000)
    outro: str = Field(default="", max_length=5000)


def output(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def script_etag(script):
    return f'"script:{script.id}:{script.revision}"'


def check_if_match(value, script):
    if value is None:
        raise HTTPException(428, "If-Match is required")
    if not re.fullmatch(
        r'"script:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[1-9][0-9]*"',
        value,
    ):
        raise HTTPException(400, "If-Match must be one strong script ETag")
    if value != script_etag(script):
        raise HTTPException(412, "Script revision is stale")


def dependency(script, source_valid=True):
    status = script.status.value if hasattr(script.status, "value") else script.status
    reasons = []
    if status == "needs_review":
        reasons.append("review_required")
    elif status == "rejected":
        reasons.append("rejected")
    if status == "confirmed" and script.approved_revision != script.revision:
        reasons.append("approval_unbound")
    if script.profile.get("language", "en") not in {"ar", "en"}:
        reasons.append("unsupported_language")
    if not source_valid:
        reasons.append("source_invalid")
    reasons.sort()
    return {
        "story_version_id": str(script.story_version_id),
        "script_version_id": str(script.id),
        "revision": script.revision,
        "language": script.profile.get("language", "en"),
        "profile_fingerprint": script.profile_fingerprint,
        "eligible": not reasons,
        "reasons": reasons,
    }


async def validated_candidate(db, script, data):
    story = await db.get(StoryVersion, script.story_version_id)
    if story is None:
        raise ValueError("Missing snapshot")
    pages, panels, ocr = await source_records(db, script.chapter_id)
    result = ScriptStructuredOutput.model_validate(data)
    validate_script(
        result,
        StoryStructuredOutput.model_validate(story.data),
        pages,
        panels,
        ocr,
        ScriptProfile.model_validate(script.profile),
        generated=False,
    )
    return result


async def current_dependency(db, script):
    try:
        await validated_candidate(db, script, script.data)
    except ValueError:
        return dependency(script, source_valid=False)
    return dependency(script)


async def script_output(db, script):
    return {**output(script), "dependency": await current_dependency(db, script)}


def script_response(value, script, response):
    response.headers["ETag"] = script_etag(script)
    response.headers["Cache-Control"] = "no-store"
    return value


async def locked_script(db, script_id, user, if_match):
    script = await owned_script(db, script_id, user)
    # A no-change UPDATE serializes writers on both PostgreSQL and SQLite.
    await db.execute(
        update(ScriptVersion)
        .where(ScriptVersion.id == script_id)
        .values(updated_at=ScriptVersion.updated_at)
    )
    await db.refresh(script)
    check_if_match(if_match, script)
    return script


async def mutation_before(db, script):
    return {
        "revision_before": script.revision,
        "approved_revision_before": script.approved_revision,
        "review_before": script.status,
        "dependency_before": await current_dependency(db, script),
        "content_before": deepcopy(script.data),
    }


async def mutation_after(db, script, before, reason):
    after = await current_dependency(db, script)
    result = {
        **{key: value for key, value in before.items() if key != "content_before"},
        "revision_after": script.revision,
        "approved_revision_after": script.approved_revision,
        "review_after": script.status,
        "dependency_after": after,
        "invalidation_reason": reason,
    }
    if script.approved_revision == script.revision:
        result["approval_token"] = {
            key: value for key, value in after.items() if key not in {"eligible", "reasons"}
        }
    return result


async def owned_script(db, script_id: UUID, user: User):
    script = await db.scalar(select(ScriptVersion).where(ScriptVersion.id == script_id))
    if not script:
        raise HTTPException(404, "Script not found")
    await owned_chapter(db, script.chapter_id, user)
    return script


async def source_records(db, chapter_id):
    pages = (
        await db.scalars(
            select(Page).where(Page.chapter_id == chapter_id).order_by(Page.page_number)
        )
    ).all()
    panels = (await db.scalars(select(Panel).join(Page).where(Page.chapter_id == chapter_id))).all()
    ids = [p.id for p in panels]
    ocr = (
        (await db.scalars(select(OCRResult).where(OCRResult.panel_id.in_(ids)))).all()
        if ids
        else []
    )
    return pages, panels, ocr


@router.post("/chapters/{chapter_id}/scripts", status_code=201)
async def create_script(
    chapter_id: UUID,
    payload: GenerateScriptInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    await owned_chapter(db, chapter_id, user)
    story = await db.scalar(
        select(StoryVersion).where(
            StoryVersion.id == payload.story_version_id, StoryVersion.chapter_id == chapter_id
        )
    )
    if not story:
        raise HTTPException(404, "StoryVersion not found")
    if payload.profile.language not in {"ar", "en"}:
        raise HTTPException(422, "Only ar and en are supported for new generation")
    story_id = story.id
    try:
        provider = configured_script_provider()
    except ProviderNotConfigured:
        raise HTTPException(503, "Script generation provider unavailable") from None
    profile_hash = sha256(
        json.dumps(
            [profile_fingerprint(payload.profile), provider.name, provider.model, "script-v1"],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    active = await db.scalar(
        select(ScriptGenerationJob)
        .where(
            ScriptGenerationJob.story_version_id == story.id,
            ScriptGenerationJob.profile_fingerprint == profile_hash,
            ScriptGenerationJob.status.in_(["queued", "running", "completed"]),
        )
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    if active:
        return {
            "job": output(active),
            "script": None
            if not active.script_version_id
            else await script_output(db, await db.get(ScriptVersion, active.script_version_id)),
        }
    previous = await db.scalar(
        select(ScriptGenerationJob)
        .where(
            ScriptGenerationJob.story_version_id == story.id,
            ScriptGenerationJob.profile_fingerprint == profile_hash,
        )
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    # A competing request may have committed between the two reads.
    if previous and previous.status in {"queued", "running", "completed"}:
        return {"job": output(previous), "script": None}
    job = ScriptGenerationJob(
        chapter_id=chapter_id,
        story_version_id=story.id,
        actor_id=user.id,
        profile_fingerprint=profile_hash,
        profile=payload.profile.model_dump(mode="json"),
        provider=provider.name,
        model=provider.model,
        attempt=(previous.attempt + 1 if previous else 1),
    )
    db.add(job)
    try:
        await db.flush()
        from app.script_service import audit

        await db.run_sync(lambda session: audit(session, job, "script_generation_requested"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        active = await db.scalar(
            select(ScriptGenerationJob)
            .where(
                ScriptGenerationJob.story_version_id == story_id,
                ScriptGenerationJob.profile_fingerprint == profile_hash,
                ScriptGenerationJob.status.in_(["queued", "running", "completed"]),
            )
            .order_by(ScriptGenerationJob.attempt.desc())
        )
        if active:
            return {"job": output(active), "script": None}
        raise HTTPException(409, "Script generation conflicts with an existing job") from None
    await db.refresh(job)
    await dispatch(db, job)
    await db.refresh(job)
    return {"job": output(job), "script": None}


async def dispatch(db, job):
    try:
        await run_in_threadpool(get_job_queue().submit, f"script:{job.id}")
    except Exception:
        claimed = await db.execute(
            update(ScriptGenerationJob)
            .where(ScriptGenerationJob.id == job.id, ScriptGenerationJob.status == "queued")
            .values(status="failed", error="Script queue dispatch failed", finished_at=func.now())
        )
        if claimed.rowcount:
            from app.script_service import audit

            await db.run_sync(lambda session: audit(session, job, "script_generation_failed"))
        await db.commit()


@router.get("/chapters/{chapter_id}/scripts")
async def list_scripts(
    chapter_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    await owned_chapter(db, chapter_id, user)
    rows = [
        await script_output(db, row)
        for row in (
            await db.scalars(
                select(ScriptVersion)
                .where(ScriptVersion.chapter_id == chapter_id)
                .order_by(ScriptVersion.created_at.desc())
            )
        ).all()
    ]
    response.headers["Cache-Control"] = "no-store"
    return rows


@router.get("/scripts/{script_id}")
async def get_script(
    script_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await owned_script(db, script_id, user)
    value = await script_output(db, script)
    value["segments"] = [
        output(row)
        for row in (
            await db.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_version_id == script.id)
                .order_by(ScriptSegment.sequence)
            )
        ).all()
    ]
    value["evidence"] = [
        output(row)
        for row in (
            await db.scalars(
                select(ScriptEvidence).where(ScriptEvidence.script_version_id == script.id)
            )
        ).all()
    ]
    for segment in value["segments"]:
        segment["evidence"] = [ev for ev in value["evidence"] if ev["segment_id"] == segment["id"]]
        segment["speaker_ref"] = script.data["segments"][segment["sequence"] - 1].get("speaker_ref")
    return script_response(value, script, response)


@router.get("/scripts/{script_id}/status")
async def script_status(
    script_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await owned_script(db, script_id, user)
    job = await db.scalar(
        select(ScriptGenerationJob)
        .where(ScriptGenerationJob.script_version_id == script.id)
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    return script_response(
        {"script": await script_output(db, script), "job": output(job) if job else None},
        script,
        response,
    )


@router.get("/script-jobs/{job_id}/status")
async def job_status(
    job_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    job = await db.get(ScriptGenerationJob, job_id)
    if not job:
        raise HTTPException(404, "Script job not found")
    await owned_chapter(db, job.chapter_id, user)
    chapter_id = job.chapter_id
    await db.rollback()
    from app.script_service import recover_script_jobs

    await run_in_threadpool(recover_script_jobs, chapter_id=chapter_id)
    await db.refresh(job)
    return output(job)


@router.get("/chapters/{chapter_id}/script-jobs")
async def list_script_jobs(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    await db.rollback()
    from app.script_service import recover_script_jobs

    await run_in_threadpool(recover_script_jobs, chapter_id=chapter_id)
    rows = await db.scalars(
        select(ScriptGenerationJob)
        .where(ScriptGenerationJob.chapter_id == chapter_id)
        .order_by(ScriptGenerationJob.created_at.desc(), ScriptGenerationJob.attempt.desc())
    )
    return [output(row) for row in rows]


@router.post("/script-jobs/{job_id}/retry", status_code=202)
async def retry_script(
    job_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    old = await db.get(ScriptGenerationJob, job_id)
    if not old:
        raise HTTPException(404, "Script job not found")
    await owned_chapter(db, old.chapter_id, user)
    if old.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled jobs can be retried")
    story_id, profile_hash = old.story_version_id, old.profile_fingerprint
    latest = await db.scalar(
        select(ScriptGenerationJob)
        .where(
            ScriptGenerationJob.story_version_id == story_id,
            ScriptGenerationJob.profile_fingerprint == profile_hash,
        )
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    if latest.id != old.id:
        return output(latest)
    job = ScriptGenerationJob(
        chapter_id=old.chapter_id,
        story_version_id=old.story_version_id,
        actor_id=user.id,
        profile_fingerprint=old.profile_fingerprint,
        profile=old.profile,
        provider=old.provider,
        model=old.model,
        attempt=old.attempt + 1,
    )
    db.add(job)
    try:
        await db.flush()
        from app.script_service import audit

        await db.run_sync(lambda session: audit(session, job, "script_generation_retry"))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        latest = await db.scalar(
            select(ScriptGenerationJob)
            .where(
                ScriptGenerationJob.story_version_id == story_id,
                ScriptGenerationJob.profile_fingerprint == profile_hash,
            )
            .order_by(ScriptGenerationJob.attempt.desc())
        )
        return output(latest)
    await db.refresh(job)
    await dispatch(db, job)
    await db.refresh(job)
    return output(job)


@router.patch("/script-segments/{segment_id}")
async def edit_segment(
    segment_id: UUID,
    payload: SegmentPatch,
    response: Response,
    if_match: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    segment = await db.get(ScriptSegment, segment_id)
    if not segment:
        raise HTTPException(404, "Script segment not found")
    script = await locked_script(db, segment.script_version_id, user, if_match)
    await db.refresh(segment)
    values = payload.model_dump(exclude_unset=True)
    if not values or any(
        value is None and key not in {"dialogue_text", "speaker_ref"}
        for key, value in values.items()
    ):
        raise HTTPException(422, "Nonempty edit with non-null required fields expected")
    import copy

    data = copy.deepcopy(script.data)
    before = data["segments"][segment.sequence - 1].copy()
    edited = data["segments"].pop(segment.sequence - 1)
    edited.update(values)
    position = values.get("sequence", segment.sequence)
    if position is None or not 1 <= position <= len(data["segments"]) + 1:
        raise HTTPException(422, "Invalid sequence")
    data["segments"].insert(position - 1, edited)
    for index, item in enumerate(data["segments"], 1):
        item["sequence"] = index
    # Front matter repeats a segment, so retain that linkage on narration edits.
    for field in ("hook", "intro", "outro"):
        if data[field] == before["narration_text"]:
            data[field] = edited["narration_text"]
    story = await db.get(StoryVersion, script.story_version_id)
    pages, panels, ocr = await source_records(db, script.chapter_id)
    try:
        result = ScriptStructuredOutput.model_validate(data)
        validate_script(
            result,
            StoryStructuredOutput.model_validate(story.data),
            pages,
            panels,
            ocr,
            ScriptProfile.model_validate(script.profile),
            generated=False,
        )
    except ValueError:
        raise HTTPException(422, "Edit failed script grounding validation") from None
    if result.model_dump(mode="json") == script.data:
        return script_response(output(segment), script, response)
    audit_state = await mutation_before(db, script)
    result.status = ReviewState.NEEDS_REVIEW
    for item in result.segments:
        item.status = ReviewState.NEEDS_REVIEW
    rows = (
        await db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.script_version_id == script.id)
            .order_by(ScriptSegment.sequence)
        )
    ).all()
    by_temp = {item["temp_id"]: row for item, row in zip(script.data["segments"], rows)}
    for row in rows:
        row.sequence += 1000
    await db.flush()
    for item in result.segments:
        row = by_temp[item.temp_id]
        for key, value in item.model_dump(
            mode="json", exclude={"temp_id", "evidence", "source_dialogue", "speaker_ref"}
        ).items():
            setattr(row, key, value)
    script.revision += 1
    script.approved_revision = None
    script.status = "needs_review"
    script.data = result.model_dump(mode="json")
    script.updated_at = func.now()
    db.add(
        StoryAudit(
            chapter_id=script.chapter_id,
            actor_id=user.id,
            action="script_segment_edited",
            entity_type="script_segment",
            entity_id=segment.id,
            version_id=script.story_version_id,
            data={
                **await mutation_after(db, script, audit_state, "script_edited"),
                "affected_segment_ids": [str(row.id) for row in rows],
                "content_before": audit_state["content_before"],
                "content_after": script.data,
                "fields": sorted(values),
                "before": before,
                "after": edited,
                "script_version_id": str(script.id),
                "story_version_id": str(script.story_version_id),
            },
        )
    )
    await db.commit()
    await db.refresh(segment)
    return script_response(output(segment), script, response)


@router.patch("/scripts/{script_id}/review")
async def review_script(
    script_id: UUID,
    payload: ScriptStatusPatch,
    response: Response,
    if_match: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await locked_script(db, script_id, user, if_match)
    before = script.status.value if hasattr(script.status, "value") else script.status
    if payload.status == "confirmed":
        try:
            await validated_candidate(db, script, script.data)
        except ValueError:
            raise HTTPException(422, "Confirmation failed script grounding validation") from None
    adopted = before == "confirmed" and script.approved_revision is None
    if before == payload.status and not (adopted and payload.status == "confirmed"):
        return script_response(await script_output(db, script), script, response)
    audit_state = await mutation_before(db, script)
    script.revision += 1
    script.approved_revision = script.revision if payload.status == "confirmed" else None
    script.status = payload.status
    data = deepcopy(script.data)
    data["status"] = payload.status
    for segment in data["segments"]:
        segment["status"] = payload.status
    script.data = data
    await db.execute(
        update(ScriptSegment)
        .where(ScriptSegment.script_version_id == script.id)
        .values(status=payload.status)
    )
    db.add(
        StoryAudit(
            chapter_id=script.chapter_id,
            action="review",
            actor_id=user.id,
            entity_type="script_version",
            entity_id=script.id,
            version_id=script.story_version_id,
            data={
                **await mutation_after(db, script, audit_state, "review_changed"),
                "approval_adopted": adopted and payload.status == "confirmed",
                "before": before,
                "after": payload.status,
                "script_version_id": str(script.id),
                "story_version_id": str(script.story_version_id),
            },
        )
    )
    await db.commit()
    await db.refresh(script)
    return script_response(await script_output(db, script), script, response)


@router.patch("/scripts/{script_id}/metadata")
async def edit_metadata(
    script_id: UUID,
    payload: MetadataPatch,
    response: Response,
    if_match: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await locked_script(db, script_id, user, if_match)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(422, "At least one metadata field is required")
    before = {field: script.data[field] for field in values}
    data = deepcopy(script.data)
    data.update(values)
    story = await db.get(StoryVersion, script.story_version_id)
    pages, panels, ocr = await source_records(db, script.chapter_id)
    try:
        result = ScriptStructuredOutput.model_validate(data)
        validate_script(
            result,
            StoryStructuredOutput.model_validate(story.data),
            pages,
            panels,
            ocr,
            ScriptProfile.model_validate(script.profile),
            generated=False,
        )
    except ValueError:
        raise HTTPException(422, "Edit failed script grounding validation") from None
    if result.model_dump(mode="json") == script.data:
        return script_response(await script_output(db, script), script, response)
    audit_state = await mutation_before(db, script)
    result.status = ReviewState.NEEDS_REVIEW
    for item in result.segments:
        item.status = ReviewState.NEEDS_REVIEW
    script.revision += 1
    script.approved_revision = None
    script.status = "needs_review"
    script.data = result.model_dump(mode="json")
    script.title = result.title
    script.updated_at = func.now()
    await db.execute(
        update(ScriptSegment)
        .where(ScriptSegment.script_version_id == script.id)
        .values(status="needs_review")
    )
    db.add(
        StoryAudit(
            chapter_id=script.chapter_id,
            actor_id=user.id,
            action="script_metadata_edited",
            entity_type="script_version",
            entity_id=script.id,
            version_id=script.story_version_id,
            data={
                **await mutation_after(db, script, audit_state, "metadata_edited"),
                "fields": sorted(values),
                "before": before,
                "after": values,
                "script_version_id": str(script.id),
                "story_version_id": str(script.story_version_id),
            },
        )
    )
    await db.commit()
    await db.refresh(script)
    return script_response(await script_output(db, script), script, response)
