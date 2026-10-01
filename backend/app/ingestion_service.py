"""Application-owned ingestion leases. Celery acknowledgment policy is unchanged."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.extraction import extract
from app.jobs import job_engine
from app.models import Chapter, ChapterFile, IngestionAttempt, Job, Page
from app.storage import get_storage

LEASE_SECONDS = 600
HEARTBEAT_SECONDS = 30
ACTIVE = ("queued", "extracting", "processing_pages", "finalizing")
DISCARD = ("failed", "abandoned", "cancelled")


class Fenced(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def expired(value):
    return value is None or value.replace(tzinfo=timezone.utc) <= utcnow()


def new_attempt(job, generation):
    return IngestionAttempt(
        id=uuid4(),
        job_id=job.id,
        chapter_id=job.chapter_id,
        generation=generation,
        status="queued",
        lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS),
        last_heartbeat_at=utcnow(),
    )


def lock_job(db, job_id):
    # All mutations take the logical job write lock first, including SQLite.
    db.execute(update(Job).where(Job.id == job_id).values(status=Job.status))
    return db.get(Job, job_id, populate_existing=True)


def validate(db, job, attempt_id):
    attempt = db.get(IngestionAttempt, attempt_id, populate_existing=True)
    if (
        job is None
        or attempt is None
        or job.current_attempt_id != attempt_id
        or attempt.job_id != job.id
        or attempt.chapter_id != job.chapter_id
        or attempt.status not in ACTIVE
        or expired(attempt.lease_expires_at)
        or attempt.generation
        != db.scalar(
            select(func.max(IngestionAttempt.generation)).where(
                IngestionAttempt.chapter_id == job.chapter_id
            )
        )
    ):
        raise Fenced()
    return attempt


@contextmanager
def owned(db_engine, job_id, attempt_id):
    with Session(db_engine) as db, db.begin():
        job = lock_job(db, job_id)
        attempt = validate(db, job, attempt_id)
        yield db, job, attempt


def renew(db_engine, job_id, attempt_id):
    with owned(db_engine, job_id, attempt_id) as (_, _, attempt):
        attempt.last_heartbeat_at = utcnow()
        attempt.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)


@contextmanager
def heartbeats(db_engine, job_id, attempt_id):
    stop = Event()

    def run():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                renew(db_engine, job_id, attempt_id)
            except Exception:
                # No lease resurrection. The next foreground fence check fails closed.
                return

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def attempt_prefix(db, attempt):
    chapter = db.get(Chapter, attempt.chapter_id)
    from app.models import Project

    project = db.get(Project, chapter.project_id)
    return (
        f"users/{project.user_id}/projects/{project.id}/chapters/{chapter.id}"
        f"/attempts/{attempt.id}/"
    )


def cleanup_attempt(attempt_id):
    """Resweep terminal tombstones, even previously swept ones (late remote PUTs)."""
    db_engine = job_engine()
    try:
        with Session(db_engine) as db:
            attempt = db.get(IngestionAttempt, attempt_id)
            if not attempt or attempt.status not in DISCARD or attempt.generation == 0:
                return
            prefix = attempt_prefix(db, attempt)
            attempt.cleanup_status = "pending"
            db.commit()
            try:
                get_storage().delete_prefix(prefix)
            except Exception:
                return  # Durable pending status; operator can repeat reconciliation.
            db.execute(delete(Page).where(Page.attempt_id == attempt_id))
            attempt.cleanup_status = "swept"
            db.commit()
    finally:
        db_engine.dispose()


def transition(db, job, attempt, target):
    allowed = {
        "queued": "extracting",
        "extracting": "processing_pages",
        "processing_pages": "finalizing",
        "finalizing": "completed",
    }
    if allowed.get(attempt.status) != target:
        raise Fenced()
    attempt.status = job.status = target
    db.get(Chapter, job.chapter_id).status = "ready" if target == "completed" else target
    if target == "completed":
        attempt.finished_at = utcnow()


def finalize(db_engine, job_id, attempt_id, pages, storage):
    with owned(db_engine, job_id, attempt_id) as (db, job, attempt):
        if attempt.status != "finalizing" or not pages:
            raise Fenced()
        if [p.page_number for p in pages] != list(range(1, len(pages) + 1)):
            raise ValueError("Incomplete pages")
        if any(p.attempt_id != attempt_id or p.chapter_id != job.chapter_id for p in pages):
            raise Fenced()
        if (
            db.scalar(
                select(func.count())
                .select_from(IngestionAttempt)
                .where(
                    IngestionAttempt.chapter_id == job.chapter_id,
                    IngestionAttempt.status.in_(ACTIVE),
                )
            )
            != 1
        ):
            raise Fenced()
        for page in pages:
            if not storage.exists(page.storage_key) or not storage.exists(page.thumbnail_key):
                raise ValueError("Missing processing object")
        # Row lock prevents recovery between this check and atomic publication.
        if expired(attempt.lease_expires_at):
            raise Fenced()
        db.add_all(pages)
        transition(db, job, attempt, "completed")


def process_ingestion(job_id: UUID, attempt_id: UUID):
    db_engine = job_engine()
    store = get_storage()
    try:
        with owned(db_engine, job_id, attempt_id) as (db, job, attempt):
            transition(db, job, attempt, "extracting")
            attempt.started_at = utcnow()
            source = db.get(ChapterFile, job.file_id)
            if source.chapter_id != job.chapter_id:
                raise Fenced()
            source_key = source.storage_key
            prefix = attempt_prefix(db, attempt)
            chapter_id = job.chapter_id
        with heartbeats(db_engine, job_id, attempt_id):
            content = store.get(source_key)
            with owned(db_engine, job_id, attempt_id) as (db, job, attempt):
                transition(db, job, attempt, "processing_pages")
            pages = []
            total_bytes = 0
            for number, (data, thumb, width, height) in enumerate(
                extract(content, source_key.rsplit(".", 1)[-1]), 1
            ):
                total_bytes += len(data) + len(thumb)
                if total_bytes > get_settings().max_extracted_bytes:
                    raise ValueError("Extracted output size limit exceeded")
                page_id = uuid4()
                key = f"{prefix}pages/{page_id}.jpg"
                thumb_key = f"{prefix}thumbnails/{page_id}.jpg"
                for object_key, payload in ((key, data), (thumb_key, thumb)):
                    renew(db_engine, job_id, attempt_id)
                    store.put(object_key, payload, "image/jpeg")
                    # A remote PUT can finish after abandonment; clean its exact prefix.
                    renew(db_engine, job_id, attempt_id)
                pages.append(
                    Page(
                        id=page_id,
                        chapter_id=chapter_id,
                        attempt_id=attempt_id,
                        page_number=number,
                        storage_key=key,
                        thumbnail_key=thumb_key,
                        width=width,
                        height=height,
                        format="JPEG",
                        file_size=len(data),
                        status="ready",
                    )
                )
            with owned(db_engine, job_id, attempt_id) as (db, job, attempt):
                transition(db, job, attempt, "finalizing")
            finalize(db_engine, job_id, attempt_id, pages, store)
    except Fenced:
        pass
    except Exception:
        # Never log raw provider/database errors: they can contain credentials.
        try:
            with owned(db_engine, job_id, attempt_id) as (db, job, attempt):
                attempt.status = job.status = "failed"
                attempt.finished_at = utcnow()
                attempt.error = job.error = "File processing failed"
                attempt.cleanup_status = "pending"
                db.get(Chapter, job.chapter_id).status = "failed"
        except Fenced:
            pass
    finally:
        db_engine.dispose()
        cleanup_attempt(attempt_id)


def recover_attempt(job_id, attempt_id):
    db_engine = job_engine()
    try:
        with Session(db_engine) as db, db.begin():
            job = lock_job(db, job_id)
            if not job:
                raise Fenced()
            if job.current_attempt_id != attempt_id:
                return job.current_attempt_id
            attempt = db.get(IngestionAttempt, attempt_id)
            if not attempt or attempt.job_id != job.id:
                raise Fenced()
            if attempt.status == "completed":
                raise Fenced()
            if attempt.status in ACTIVE:
                if not expired(attempt.lease_expires_at):
                    return attempt.id
                attempt.status = "abandoned"
                attempt.finished_at = utcnow()
            attempt.cleanup_status = "pending" if attempt.generation else "legacy"
            db.flush()  # Release the active partial unique index before inserting.
            generation = (
                db.scalar(
                    select(func.max(IngestionAttempt.generation)).where(
                        IngestionAttempt.chapter_id == job.chapter_id
                    )
                )
                + 1
            )
            replacement = new_attempt(job, generation)
            db.add(replacement)
            db.flush()
            job.current_attempt_id = replacement.id
            job.status = "queued"
            job.error = None
            db.get(Chapter, job.chapter_id).status = "uploaded"
            result = replacement.id
        cleanup_attempt(attempt_id)
        return result
    finally:
        db_engine.dispose()


def cancel_ingestion(job_id):
    db_engine = job_engine()
    try:
        with Session(db_engine) as db, db.begin():
            job = lock_job(db, job_id)
            if not job or job.status != "queued":
                return False
            attempt = db.get(IngestionAttempt, job.current_attempt_id)
            if not attempt or attempt.status != "queued":
                return False
            attempt.status = job.status = "cancelled"
            attempt.finished_at = utcnow()
            attempt.cleanup_status = "pending"
            db.get(Chapter, job.chapter_id).status = "failed"
            return True
    finally:
        db_engine.dispose()
