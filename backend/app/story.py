from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.db import get_db
from app.jobs import get_job_queue
from app.models import (
    ChapterUnderstanding,
    Character,
    CharacterAlias,
    Event,
    EventCharacter,
    Scene,
    SceneCharacter,
    StoryAnalysisJob,
    StoryAudit,
    StoryRelationship,
    StoryVersion,
    User,
)
from app.projects import owned_chapter, protect_write
from app.story_audit import complete_audit, graph_snapshot
from app.story_resolution import normalize, union
from app.story_review import validate_review_transition
from app.story_schemas import ReviewStatus
from app.story_service import fingerprint

router = APIRouter(prefix="/api/v1", dependencies=[Depends(protect_write)])


def output(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class ReviewPatch(BaseModel):
    @field_validator(
        "status",
        "importance",
        "name",
        "display_name",
        "title",
        "summary",
        "start_page",
        "end_page",
        "event_type",
        check_fields=False,
    )
    @classmethod
    def status_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("field cannot be null")
        return value


class CharacterPatch(ReviewPatch):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    gender: str | None = None
    age_group: str | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    status: ReviewStatus | None = None


class ScenePatch(ReviewPatch):
    model_config = ConfigDict(extra="forbid")
    title: str | None = None
    summary: str | None = None
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    status: ReviewStatus | None = None


class EventPatch(ReviewPatch):
    model_config = ConfigDict(extra="forbid")

    @field_validator("description")
    @classmethod
    def description_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("description cannot be null")
        return value

    description: str | None = None
    event_type: str | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    status: ReviewStatus | None = None


class MergeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_character_id: UUID
    reason: str = Field(default="User requested merge", min_length=1, max_length=1000)


class SplitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retained_name: str = Field(min_length=1, max_length=200)
    new_name: str = Field(min_length=1, max_length=200)
    scene_ids: list[UUID]
    event_ids: list[UUID]
    reason: str = Field(min_length=1, max_length=1000)


def mark_edited(record, fields):
    record.edited_fields = sorted(set(record.edited_fields or []) | set(fields))
    record.provenance = "user_confirmed" if record.status == "confirmed" else "user_edited"


def apply_patch(record, payload):
    values = payload.model_dump(exclude_unset=True)
    if "status" in values:
        try:
            validate_review_transition(record.status, values["status"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    for key, value in values.items():
        setattr(record, key, value)


async def finish_user_audit(db, entity_id, chapter_id, before):
    audit = next(
        row for row in db.new if isinstance(row, StoryAudit) and row.entity_id == entity_id
    )
    after = await db.run_sync(lambda session: graph_snapshot(session, chapter_id))
    await db.run_sync(lambda session: complete_audit(session, audit, before, after))


async def lock_story(db, chapter_id):
    # A database write lock serializes graph edits with publication on SQLite and PostgreSQL.
    await db.execute(
        update(StoryAnalysisJob)
        .where(StoryAnalysisJob.chapter_id == chapter_id)
        .values(updated_at=StoryAnalysisJob.updated_at)
    )


async def owned_character(db, character_id, user):
    character = await db.get(Character, character_id)
    if not character:
        raise HTTPException(404, "Character not found")
    await owned_chapter(db, character.chapter_id, user)
    return character


async def owned_scene(db, scene_id, user):
    scene = await db.get(Scene, scene_id)
    if not scene:
        raise HTTPException(404, "Scene not found")
    await owned_chapter(db, scene.chapter_id, user)
    return scene


async def owned_event(db, event_id, user):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    scene = await owned_scene(db, event.scene_id, user)
    return event, scene


@router.post("/chapters/{chapter_id}/story/analyze")
async def analyze_story(
    chapter_id: UUID,
    payload: AnalyzeInput = AnalyzeInput(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    chapter = await owned_chapter(db, chapter_id, user)
    current_fingerprint, _ = await db.run_sync(lambda session: fingerprint(session, chapter_id))
    job = await db.scalar(select(StoryAnalysisJob).where(StoryAnalysisJob.chapter_id == chapter_id))
    if job and job.status in {"pending", "analyzing"}:
        return output(job)
    if job and job.status == "completed" and job.source_fingerprint == current_fingerprint:
        return output(job)
    if job:
        previous_id = job.id
        result = await db.execute(
            update(StoryAnalysisJob)
            .where(
                StoryAnalysisJob.id == previous_id,
                StoryAnalysisJob.status.not_in(["pending", "analyzing"]),
            )
            .values(
                id=uuid4(),
                status="pending",
                error=None,
                source_fingerprint=current_fingerprint,
                pages_processed=0,
                chunks=0,
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0,
                duration=None,
                validation_errors=[],
                validation_warnings=[],
            )
        )
        await db.commit()
        job = await db.scalar(
            select(StoryAnalysisJob).where(StoryAnalysisJob.chapter_id == chapter_id)
        )
        if not result.rowcount:
            return output(job)
    else:
        job = StoryAnalysisJob(chapter_id=chapter.id, source_fingerprint=current_fingerprint)
        db.add(job)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            job = await db.scalar(
                select(StoryAnalysisJob).where(StoryAnalysisJob.chapter_id == chapter_id)
            )
            return output(job)
        await db.refresh(job)
    try:
        await __import__("asyncio").to_thread(get_job_queue().submit, "story:" + str(job.id))
    except Exception:
        job.status, job.error = "failed", "Could not submit story analysis job"
        await db.commit()
        raise HTTPException(503, job.error) from None
    await db.refresh(job)
    return output(job)


@router.get("/chapters/{chapter_id}/story/status")
async def story_status(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    job = await db.scalar(select(StoryAnalysisJob).where(StoryAnalysisJob.chapter_id == chapter_id))
    return output(job) if job else {"status": "not_started"}


@router.get("/chapters/{chapter_id}/characters")
async def characters(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    review_status: ReviewStatus | None = None,
):
    await owned_chapter(db, chapter_id, user)
    rows = await db.scalars(
        select(Character)
        .where(Character.chapter_id == chapter_id)
        .order_by(Character.importance.desc())
    )
    return [output(row) for row in rows if review_status is None or row.status == review_status]


@router.get("/chapters/{chapter_id}/scenes")
async def scenes(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    review_status: ReviewStatus | None = None,
):
    await owned_chapter(db, chapter_id, user)
    rows = await db.scalars(
        select(Scene).where(Scene.chapter_id == chapter_id).order_by(Scene.scene_index)
    )
    return [output(row) for row in rows if review_status is None or row.status == review_status]


@router.get("/scenes/{scene_id}")
async def scene(
    scene_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    return output(await owned_scene(db, scene_id, user))


@router.get("/scenes/{scene_id}/events")
async def scene_events(
    scene_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    review_status: ReviewStatus | None = None,
):
    await owned_scene(db, scene_id, user)
    rows = await db.scalars(
        select(Event).where(Event.scene_id == scene_id).order_by(Event.event_index)
    )
    return [output(row) for row in rows if review_status is None or row.status == review_status]


@router.get("/chapters/{chapter_id}/story/summary")
async def summary(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    row = await db.scalar(
        select(ChapterUnderstanding).where(ChapterUnderstanding.chapter_id == chapter_id)
    )
    return output(row) if row else {"status": "pending"}


@router.patch("/characters/{character_id}")
async def patch_character(
    character_id: UUID,
    payload: CharacterPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    record = await owned_character(db, character_id, user)
    await lock_story(db, record.chapter_id)
    if payload.model_fields_set == {"status"} and payload.status == record.status:
        return output(record)
    before = {key: getattr(record, key) for key in payload.model_fields_set}
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, record.chapter_id))
    apply_patch(record, payload)
    mark_edited(record, payload.model_fields_set)
    db.add(
        StoryAudit(
            chapter_id=record.chapter_id,
            action="user_edit",
            actor_id=user.id,
            entity_type="character",
            entity_id=record.id,
            data={
                "entity": str(record.id),
                "before": before,
                "after": {key: getattr(record, key) for key in payload.model_fields_set},
                "before_snapshot": before_graph,
                "after_snapshot": None,
            },
        )
    )
    await finish_user_audit(db, record.id, record.chapter_id, before_graph)
    await db.commit()
    await db.refresh(record)
    return output(record)


@router.patch("/scenes/{scene_id}")
async def patch_scene(
    scene_id: UUID,
    payload: ScenePatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    record = await owned_scene(db, scene_id, user)
    await lock_story(db, record.chapter_id)
    start = payload.start_page if payload.start_page is not None else record.start_page
    end = payload.end_page if payload.end_page is not None else record.end_page
    if end < start:
        raise HTTPException(422, "Invalid scene range")
    if payload.model_fields_set == {"status"} and payload.status == record.status:
        return output(record)
    before = {key: getattr(record, key) for key in payload.model_fields_set}
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, record.chapter_id))
    apply_patch(record, payload)
    mark_edited(record, payload.model_fields_set)
    db.add(
        StoryAudit(
            chapter_id=record.chapter_id,
            action="user_edit",
            actor_id=user.id,
            entity_type="scene",
            entity_id=record.id,
            data={
                "entity": str(record.id),
                "before": before,
                "after": {key: getattr(record, key) for key in payload.model_fields_set},
                "before_snapshot": before_graph,
                "after_snapshot": None,
            },
        )
    )
    await finish_user_audit(db, record.id, record.chapter_id, before_graph)
    await db.commit()
    await db.refresh(record)
    return output(record)


@router.patch("/events/{event_id}")
async def patch_event(
    event_id: UUID,
    payload: EventPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    record, owner = await owned_event(db, event_id, user)
    await lock_story(db, owner.chapter_id)
    if payload.model_fields_set == {"status"} and payload.status == record.status:
        return output(record)
    before = {key: getattr(record, key) for key in payload.model_fields_set}
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, owner.chapter_id))
    apply_patch(record, payload)
    mark_edited(record, payload.model_fields_set)
    db.add(
        StoryAudit(
            chapter_id=owner.chapter_id,
            action="user_edit",
            actor_id=user.id,
            entity_type="event",
            entity_id=record.id,
            data={
                "entity": str(record.id),
                "before": before,
                "after": {key: getattr(record, key) for key in payload.model_fields_set},
                "before_snapshot": before_graph,
                "after_snapshot": None,
            },
        )
    )
    await finish_user_audit(db, record.id, owner.chapter_id, before_graph)
    await db.commit()
    await db.refresh(record)
    return output(record)


@router.post("/characters/{character_id}/merge")
async def merge_character(
    character_id: UUID,
    payload: MergeInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Merge source into target without overwriting either character's values.

    Directed edges retain direction and type; collapsing self edges are removed.
    Same-type duplicate edges and normalized aliases union evidence and take the
    maximum confidence (omitted ORM confidence defaults to zero; NULL is invalid).
    Different edge types remain separate. Participation is a set union. Source
    user values remain on the rejected source; target user values remain intact.
    Repeating the same completed merge is a no-op, including audit/review state.
    """
    source = await owned_character(db, character_id, user)
    target = await owned_character(db, payload.target_character_id, user)
    if source.chapter_id != target.chapter_id or source.id == target.id:
        raise HTTPException(409, "Characters must belong to the same chapter")
    if source.status == "rejected" and any(
        conflict.get("source_id") == str(source.id) for conflict in target.review_conflicts or []
    ):
        return output(target)
    await lock_story(db, source.chapter_id)
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, source.chapter_id))
    target_before = output(target)
    audit = StoryAudit(
        chapter_id=source.chapter_id,
        action="merge",
        actor_id=user.id,
        entity_type="character",
        entity_id=source.id,
        data={
            "source": str(source.id),
            "target": str(target.id),
            "reason": payload.reason,
            "source_snapshot": {
                k: str(v) if isinstance(v, UUID) else v
                for k, v in output(source).items()
                if k not in {"created_at", "updated_at"}
            },
            "target_snapshot": {
                k: str(v) if isinstance(v, UUID) else v
                for k, v in target_before.items()
                if k not in {"created_at", "updated_at"}
            },
            "before_snapshot": before_graph,
            "after_snapshot": None,
        },
    )
    db.add(audit)
    target.evidence = target.evidence + [e for e in source.evidence if e not in target.evidence]
    target.review_conflicts = [
        *target.review_conflicts,
        {
            "source_id": str(source.id),
            "reason": "Merged source retained for review",
            "name": source.name,
            "edited_fields": source.edited_fields,
        },
    ]
    mark_edited(target, ["evidence", "aliases", "participation"])
    for model, key in ((SceneCharacter, "scene_id"), (EventCharacter, "event_id")):
        edges = list(await db.scalars(select(model).where(model.character_id == source.id)))
        for edge in edges:
            owner_id = getattr(edge, key)
            if await db.get(model, (owner_id, target.id)) is None:
                db.add(model(**{key: owner_id, "character_id": target.id}))
            await db.delete(edge)
    aliases = await db.scalars(
        select(CharacterAlias).where(CharacterAlias.character_id == source.id)
    )
    for alias in aliases:
        duplicate = await db.scalar(
            select(CharacterAlias).where(
                CharacterAlias.character_id == target.id,
                CharacterAlias.normalized_alias == normalize(alias.alias),
            )
        )
        if duplicate:
            duplicate.evidence = union(duplicate.evidence, alias.evidence)
            duplicate.confidence = max(duplicate.confidence, alias.confidence)
            await db.delete(alias)
        else:
            alias.normalized_alias = normalize(alias.alias)
            alias.character_id = target.id
    relationships = list(
        await db.scalars(
            select(StoryRelationship).where(
                (StoryRelationship.source_character_id == source.id)
                | (StoryRelationship.target_character_id == source.id)
            )
        )
    )
    for relationship in relationships:
        new_source = (
            target.id
            if relationship.source_character_id == source.id
            else relationship.source_character_id
        )
        new_target = (
            target.id
            if relationship.target_character_id == source.id
            else relationship.target_character_id
        )
        if new_source == new_target:
            await db.delete(relationship)
            continue
        duplicate = await db.scalar(
            select(StoryRelationship).where(
                StoryRelationship.id != relationship.id,
                StoryRelationship.chapter_id == relationship.chapter_id,
                StoryRelationship.source_character_id == new_source,
                StoryRelationship.target_character_id == new_target,
                StoryRelationship.relationship_type == relationship.relationship_type,
            )
        )
        if duplicate:
            duplicate.evidence = union(duplicate.evidence, relationship.evidence)
            duplicate.confidence = max(duplicate.confidence, relationship.confidence)
            await db.delete(relationship)
        else:
            relationship.source_character_id = new_source
            relationship.target_character_id = new_target
    # Retain the source and its user values; rejected records are not discarded.
    source.status = "rejected"
    mark_edited(source, ["status"])
    await db.flush()
    await db.refresh(target)
    audit.data = {
        **audit.data,
        "after": jsonable_encoder(output(target)),
        "after_snapshot": await db.run_sync(
            lambda session: graph_snapshot(session, source.chapter_id)
        ),
    }
    await db.run_sync(
        lambda session: complete_audit(session, audit, before_graph, audit.data["after_snapshot"])
    )
    await db.commit()
    await db.refresh(target)
    return output(target)


@router.post("/characters/{character_id}/split")
async def split_character(
    character_id: UUID,
    payload: SplitInput,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    source = await owned_character(db, character_id, user)
    await lock_story(db, source.chapter_id)
    # Serialize retries even when this chapter has no analysis job row to lock.
    await db.execute(
        update(Character).where(Character.id == source.id).values(updated_at=Character.updated_at)
    )
    await db.refresh(source)
    previous = await db.scalars(
        select(StoryAudit).where(
            StoryAudit.chapter_id == source.chapter_id,
            StoryAudit.entity_id == source.id,
            StoryAudit.action == "split",
            StoryAudit.actor_id == user.id,
        )
    )
    for audit in previous:
        data = audit.data
        after = data.get("after", {})
        if (
            data.get("reason") == payload.reason
            and after.get("retained", {}).get("name") == payload.retained_name
            and after.get("created", {}).get("name") == payload.new_name
            and set(data.get("scene_ids", [])) == {str(i) for i in payload.scene_ids}
            and set(data.get("event_ids", [])) == {str(i) for i in payload.event_ids}
        ):
            return after
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, source.chapter_id))
    source_before = jsonable_encoder(output(source))
    selected = []
    for model, key, identifiers in (
        (SceneCharacter, "scene_id", payload.scene_ids),
        (EventCharacter, "event_id", payload.event_ids),
    ):
        for identifier in set(identifiers):
            if model is SceneCharacter:
                owner = await owned_scene(db, identifier, user)
            else:
                _, owner = await owned_event(db, identifier, user)
            edge = await db.get(model, (identifier, source.id))
            if owner.chapter_id != source.chapter_id or edge is None:
                raise HTTPException(409, "Split references must belong to the source character")
            selected.append((model, key, identifier, edge))
    values = {
        c.name: getattr(source, c.name)
        for c in Character.__table__.columns
        if c.name not in {"id", "created_at", "updated_at"}
    }
    values.update(
        name=payload.new_name,
        display_name=payload.new_name,
        provenance="user_edited",
        edited_fields=["name", "participation"],
        status="needs_review",
        review_conflicts=[],
    )
    target = Character(**values)
    db.add(target)
    await db.flush()
    source.name = source.display_name = payload.retained_name
    mark_edited(source, ["name", "participation"])
    for model, key, identifier, edge in selected:
        db.add(model(**{key: identifier, "character_id": target.id}))
        await db.delete(edge)
    await db.flush()
    await db.refresh(source)
    await db.refresh(target)
    db.add(
        StoryAudit(
            chapter_id=source.chapter_id,
            action="split",
            actor_id=user.id,
            entity_type="character",
            entity_id=source.id,
            data={
                "source": str(source.id),
                "target": str(target.id),
                "reason": payload.reason,
                "scene_ids": [str(i) for i in payload.scene_ids],
                "event_ids": [str(i) for i in payload.event_ids],
                "before": source_before,
                "after": jsonable_encoder({"retained": output(source), "created": output(target)}),
                "before_snapshot": before_graph,
                "after_snapshot": None,
            },
        )
    )
    await finish_user_audit(db, source.id, source.chapter_id, before_graph)
    await db.commit()
    await db.refresh(source)
    await db.refresh(target)
    return {"retained": output(source), "created": output(target)}


@router.get("/chapters/{chapter_id}/story/versions")
async def story_versions(
    chapter_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    await owned_chapter(db, chapter_id, user)
    return [
        output(r)
        for r in await db.scalars(
            select(StoryVersion)
            .where(StoryVersion.chapter_id == chapter_id)
            .order_by(StoryVersion.created_at)
        )
    ]


@router.get("/chapters/{chapter_id}/relationships")
async def relationships(
    chapter_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
    review_status: ReviewStatus | None = None,
):
    await owned_chapter(db, chapter_id, user)
    rows = await db.scalars(
        select(StoryRelationship).where(StoryRelationship.chapter_id == chapter_id)
    )
    return [output(r) for r in rows if review_status is None or r.status == review_status]


@router.delete("/events/{event_id}")
async def delete_event(
    event_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_user)
):
    record, owner = await owned_event(db, event_id, user)
    before = jsonable_encoder(output(record))
    before_graph = await db.run_sync(lambda session: graph_snapshot(session, owner.chapter_id))
    if record.status == "rejected" and "status" in record.edited_fields:
        return {"success": True}
    record.status = "rejected"
    mark_edited(record, ["status"])
    db.add(
        StoryAudit(
            chapter_id=owner.chapter_id,
            action="delete",
            actor_id=user.id,
            entity_type="event",
            entity_id=record.id,
            data={
                "entity": str(record.id),
                "actor_type": "user",
                "actor_id": str(user.id),
                "after": {"status": "rejected"},
                "before": before,
                "before_snapshot": before_graph,
                "after_snapshot": None,
            },
        )
    )
    await finish_user_audit(db, record.id, owner.chapter_id, before_graph)
    await db.commit()
    return {"success": True}
