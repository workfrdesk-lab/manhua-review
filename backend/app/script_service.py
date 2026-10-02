"""Script processing on the shared queue. Publication and completion are atomic."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.jobs import job_engine
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
)
from app.script_providers import configured_script_provider
from app.script_schemas import ScriptProfile, ScriptStructuredOutput
from app.script_validation import validate_script
from app.story_providers import script_prompt
from app.story_schemas import StoryStructuredOutput


def audit(db, job, action):
    db.add(
        StoryAudit(
            chapter_id=job.chapter_id,
            actor_id=job.actor_id,
            action=action,
            entity_type="script_generation_job",
            entity_id=job.id,
            version_id=job.story_version_id,
            data={
                "attempt": job.attempt,
                "fingerprint": job.profile_fingerprint,
                "job_id": str(job.id),
                "story_version_id": str(job.story_version_id),
                "script_version_id": str(job.script_version_id) if job.script_version_id else None,
            },
        )
    )


def script_queue_status(job_id):
    engine = job_engine()
    try:
        with Session(engine) as db:
            job = db.get(ScriptGenerationJob, UUID(job_id))
            if not job:
                raise ValueError("Script job not found")
            return job.status
    finally:
        engine.dispose()


def cancel_script(job_id):
    engine = job_engine()
    try:
        with Session(engine) as db:
            result = db.execute(
                update(ScriptGenerationJob)
                .where(
                    ScriptGenerationJob.id == UUID(job_id), ScriptGenerationJob.status == "queued"
                )
                .values(status="cancelled", finished_at=func.now())
            )
            db.commit()
            return bool(result.rowcount)
    finally:
        engine.dispose()


def recover_script_jobs(*, now=None, chapter_id=None):
    """Expire abandoned attempts; explicit retry preserves their history.

    The conditional update serializes with the publication fence. A provider taking
    longer than fifteen minutes also expires and cannot subsequently publish.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=15)
    engine = job_engine()
    recovered = []
    try:
        with Session(engine) as db:
            query = select(ScriptGenerationJob.id).where(
                ScriptGenerationJob.status == "running",
                ScriptGenerationJob.started_at < cutoff,
            )
            if chapter_id is not None:
                query = query.where(ScriptGenerationJob.chapter_id == chapter_id)
            candidates = db.scalars(query).all()
            for identity in candidates:
                changed = db.execute(
                    update(ScriptGenerationJob)
                    .where(
                        ScriptGenerationJob.id == identity,
                        ScriptGenerationJob.status == "running",
                        ScriptGenerationJob.started_at < cutoff,
                    )
                    .values(
                        status="failed",
                        error="Script generation interrupted or timed out; retry available",
                        finished_at=func.now(),
                    )
                )
                if changed.rowcount:
                    audit(db, db.get(ScriptGenerationJob, identity), "script_generation_failed")
                    recovered.append(str(identity))
            db.commit()
    finally:
        engine.dispose()
    return recovered


def process_script(job_id):
    engine = job_engine()
    identity = UUID(job_id)
    try:
        with Session(engine) as db:
            claimed = db.execute(
                update(ScriptGenerationJob)
                .where(ScriptGenerationJob.id == identity, ScriptGenerationJob.status == "queued")
                .values(status="running", started_at=func.now())
            )
            if not claimed.rowcount:
                db.rollback()
                return
            audit(db, db.get(ScriptGenerationJob, identity), "script_generation_started")
            db.commit()
            try:
                job = db.get(ScriptGenerationJob, identity)
                provider = configured_script_provider()
                if (provider.name, provider.model) != (job.provider, job.model):
                    raise ValueError("Provider configuration changed")
                snapshot = db.get(StoryVersion, job.story_version_id)
                story = StoryStructuredOutput.model_validate(snapshot.data)
                profile = ScriptProfile.model_validate(job.profile)
                script_prompt(
                    story, profile
                )  # Enforce the same bounded context for every provider.
                raw = provider.generate_script(story, profile)
                # End the read transaction before competing with recovery. The
                # write lock below is held through publication and completion.
                db.rollback()
                fenced = db.execute(
                    update(ScriptGenerationJob)
                    .where(
                        ScriptGenerationJob.id == identity,
                        ScriptGenerationJob.status == "running",
                    )
                    .values(status="running")
                )
                if not fenced.rowcount:
                    db.rollback()
                    return
                job = db.get(ScriptGenerationJob, identity)
                result = ScriptStructuredOutput.model_validate(
                    raw.model_dump() if isinstance(raw, ScriptStructuredOutput) else raw
                )
                pages = db.scalars(select(Page).where(Page.chapter_id == job.chapter_id)).all()
                panels = db.scalars(
                    select(Panel).join(Page).where(Page.chapter_id == job.chapter_id)
                ).all()
                ocr = db.scalars(
                    select(OCRResult).where(OCRResult.panel_id.in_([p.id for p in panels]))
                ).all()
                validate_script(result, story, pages, panels, ocr, profile)
                data = result.model_dump(mode="json")
                record = ScriptVersion(
                    chapter_id=job.chapter_id,
                    story_version_id=job.story_version_id,
                    profile_fingerprint=job.profile_fingerprint,
                    profile=job.profile,
                    fingerprint=sha256(
                        json.dumps(
                            [str(job.story_version_id), job.profile_fingerprint, data],
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    title=result.title,
                    data=data,
                    revision=1,
                    approved_revision=None,
                )
                db.add(record)
                db.flush()
                numbers = {p.page_number: p.id for p in pages}
                for segment in result.segments:
                    values = segment.model_dump(
                        mode="json",
                        exclude={"temp_id", "evidence", "speaker_ref", "source_dialogue"},
                    )
                    row = ScriptSegment(
                        script_version_id=record.id, chapter_id=job.chapter_id, **values
                    )
                    db.add(row)
                    db.flush()
                    for evidence in segment.evidence:
                        values = evidence.model_dump()
                        values["page_id"] = evidence.page_id or numbers[evidence.page_number]
                        db.add(
                            ScriptEvidence(
                                script_version_id=record.id,
                                segment_id=row.id,
                                chapter_id=job.chapter_id,
                                **values,
                            )
                        )
                job.script_version_id = record.id
                job.status = "completed"
                job.finished_at = func.now()
                audit(db, job, "script_generation_completed")
                db.commit()
            except Exception:
                db.rollback()
                changed = db.execute(
                    update(ScriptGenerationJob)
                    .where(
                        ScriptGenerationJob.id == identity,
                        ScriptGenerationJob.status == "running",
                    )
                    .values(status="failed")
                )
                if not changed.rowcount:
                    db.rollback()
                    return
                job = db.get(ScriptGenerationJob, identity)
                job.status = "failed"
                job.error = "Script generation failed; provider or source validation unavailable"
                job.finished_at = func.now()
                audit(db, job, "script_generation_failed")
                db.commit()
    finally:
        engine.dispose()
