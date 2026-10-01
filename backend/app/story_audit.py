"""Chapter-scoped reconstruction snapshots; never serialize users, settings or requests."""

from copy import deepcopy

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.models import (
    Chapter,
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
)


def graph_snapshot(db, chapter_id):
    db.flush()
    characters = select(Character.id).where(Character.chapter_id == chapter_id)
    scenes = select(Scene.id).where(Scene.chapter_id == chapter_id)
    events = select(Event.id).where(Event.scene_id.in_(scenes))
    queries = {
        "characters": select(Character).where(Character.chapter_id == chapter_id),
        "aliases": select(CharacterAlias).where(CharacterAlias.character_id.in_(characters)),
        "relationships": select(StoryRelationship).where(
            StoryRelationship.chapter_id == chapter_id
        ),
        "scenes": select(Scene).where(Scene.chapter_id == chapter_id),
        "events": select(Event).where(Event.scene_id.in_(scenes)),
        "scene_participation": select(SceneCharacter).where(SceneCharacter.scene_id.in_(scenes)),
        "event_participation": select(EventCharacter).where(EventCharacter.event_id.in_(events)),
        "understandings": select(ChapterUnderstanding).where(
            ChapterUnderstanding.chapter_id == chapter_id
        ),
    }
    result = {}
    for name, query in queries.items():
        rows = [
            jsonable_encoder({c.name: getattr(row, c.name) for c in row.__table__.columns})
            for row in db.scalars(query)
        ]
        result[name] = sorted(rows, key=lambda row: str(sorted(row.items())))
    return deepcopy(result)


def complete_audit(db, audit, before, after, version_id=None, job_id=None):
    chapter = db.get(Chapter, audit.chapter_id)
    audit.version_id = version_id
    audit.data = {
        **audit.data,
        "actor_type": "user" if audit.actor_id else "system",
        "project_id": str(chapter.project_id),
        "chapter_id": str(chapter.id),
        "version_id": str(version_id) if version_id else None,
        "job_id": str(job_id) if job_id else None,
        "before_snapshot": before,
        "after_snapshot": after,
        "removal": "soft_rejection" if audit.action == "delete" else None,
    }


def audit_reconciliation(db, chapter_id, before, after, version_id, previous_ids):
    job = db.scalar(select(StoryAnalysisJob).where(StoryAnalysisJob.chapter_id == chapter_id))
    audits = [
        row
        for row in db.scalars(select(StoryAudit).where(StoryAudit.chapter_id == chapter_id))
        if row.id not in previous_ids
    ]
    if before != after:
        audit = StoryAudit(
            chapter_id=chapter_id,
            action="ai_reconcile",
            entity_type="chapter",
            entity_id=chapter_id,
            data={"reason": "Story analysis publication"},
        )
        db.add(audit)
        audits.append(audit)
    for audit in audits:
        if audit.entity_id is None and audit.data.get("entity"):
            from uuid import UUID

            audit.entity_id = UUID(audit.data["entity"])
            for collection, entity_type in (
                ("characters", "character"),
                ("scenes", "scene"),
                ("events", "event"),
                ("relationships", "relationship"),
                ("understandings", "chapter_understanding"),
            ):
                if any(row["id"] == str(audit.entity_id) for row in after[collection]):
                    audit.entity_type = entity_type
        complete_audit(db, audit, before, after, version_id, job.id if job else None)
