"""Authenticated, chapter-owned script generation and review endpoints."""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
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
            else output(await db.get(ScriptVersion, active.script_version_id)),
        }
    previous = await db.scalar(
        select(ScriptGenerationJob)
        .where(
            ScriptGenerationJob.story_version_id == story.id,
            ScriptGenerationJob.profile_fingerprint == profile_hash,
        )
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    if previous:
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
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    return [
        output(row)
        for row in (
            await db.scalars(
                select(ScriptVersion)
                .where(ScriptVersion.chapter_id == chapter_id)
                .order_by(ScriptVersion.created_at.desc())
            )
        ).all()
    ]


@router.get("/scripts/{script_id}")
async def get_script(
    script_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    script = await owned_script(db, script_id, user)
    value = output(script)
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
    return value


@router.get("/scripts/{script_id}/status")
async def script_status(
    script_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    script = await owned_script(db, script_id, user)
    job = await db.scalar(
        select(ScriptGenerationJob)
        .where(ScriptGenerationJob.script_version_id == script.id)
        .order_by(ScriptGenerationJob.attempt.desc())
    )
    return {"script": output(script), "job": output(job) if job else None}


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

        await db.run_sync(lambda session: audit(session, job, "script_retry_requested"))
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
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    segment = await db.get(ScriptSegment, segment_id)
    if not segment:
        raise HTTPException(404, "Script segment not found")
    script = await owned_script(db, segment.script_version_id, user)
    await db.execute(
        update(ScriptVersion)
        .where(ScriptVersion.id == script.id)
        .values(updated_at=ScriptVersion.updated_at)
    )
    await db.refresh(script)
    await db.refresh(segment)
    values = payload.model_dump(exclude_unset=True)
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
    # Canonical Story edits preserve explicit human review state.
    script.data = result.model_dump(mode="json")
    script.updated_at = func.now()
    db.add(
        StoryAudit(
            chapter_id=script.chapter_id,
            actor_id=user.id,
            action="segment_edited",
            entity_type="script_segment",
            entity_id=segment.id,
            version_id=script.story_version_id,
            data={
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
    return output(segment)


@router.patch("/scripts/{script_id}/review")
async def review_script(
    script_id: UUID,
    payload: ScriptStatusPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await owned_script(db, script_id, user)
    await db.execute(
        update(ScriptVersion)
        .where(ScriptVersion.id == script.id)
        .values(updated_at=ScriptVersion.updated_at)
    )
    await db.refresh(script)
    before = script.status.value if hasattr(script.status, "value") else script.status
    if before == payload.status:
        return output(script)
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
                "before": before,
                "after": payload.status,
                "script_version_id": str(script.id),
                "story_version_id": str(script.story_version_id),
            },
        )
    )
    await db.commit()
    await db.refresh(script)
    return output(script)


@router.patch("/scripts/{script_id}/metadata")
async def edit_metadata(
    script_id: UUID,
    payload: MetadataPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    script = await owned_script(db, script_id, user)
    await db.execute(
        update(ScriptVersion)
        .where(ScriptVersion.id == script.id)
        .values(updated_at=ScriptVersion.updated_at)
    )
    await db.refresh(script)
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
    script.data = result.model_dump(mode="json")
    script.title = result.title
    script.updated_at = func.now()
    db.add(
        StoryAudit(
            chapter_id=script.chapter_id,
            actor_id=user.id,
            action="script_metadata_edited",
            entity_type="script_version",
            entity_id=script.id,
            version_id=script.story_version_id,
            data={
                "before": before,
                "after": values,
                "script_version_id": str(script.id),
                "story_version_id": str(script.story_version_id),
            },
        )
    )
    await db.commit()
    await db.refresh(script)
    return output(script)
