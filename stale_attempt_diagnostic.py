"""Read-only, sanitized stale-attempt diagnostic harness.

This harness deliberately does not import or call recovery, dispatch, cleanup,
retry, revoke, or task-submission functions. ORM values are copied to primitive
values while the session is open to avoid detached/expired attribute access.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from uuid import UUID

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs import job_engine
from app.models import ChapterFile, IngestionAttempt, Job, Page
from app.storage import get_storage
from app.worker import celery_app


JOB_ID = UUID("bd4b46e9-1c65-4ccc-a6b1-20ba202ba2e7")
OLD_ATTEMPT_ID = UUID("0873ae50-ab7c-4bd5-91a9-1271962a6dab")


def _utc(value):
    return value.isoformat() if value is not None else None


def database_probe() -> dict:
    engine = job_engine()
    try:
        # All values used after the session closes are copied to primitives here.
        with Session(engine, expire_on_commit=False) as db:
            job = db.get(Job, JOB_ID)
            old = db.get(IngestionAttempt, OLD_ATTEMPT_ID)
            if job is None or old is None:
                return {"database_found": False}
            pages = db.scalars(select(Page).where(Page.chapter_id == job.chapter_id)).all()
            files = db.scalars(
                select(ChapterFile).where(ChapterFile.chapter_id == job.chapter_id)
            ).all()
            attempts = db.scalars(
                select(IngestionAttempt).where(
                    IngestionAttempt.chapter_id == job.chapter_id
                )
            ).all()
            now = datetime.now(timezone.utc)
            result = {
                "database_found": True,
                "old_attempt_status": old.status,
                "old_attempt_started_at": _utc(old.started_at),
                "old_attempt_lease_expires_at": _utc(old.lease_expires_at),
                "old_attempt_lease_expired": bool(
                    old.lease_expires_at is not None
                    and old.lease_expires_at <= now
                ),
                "job_status": job.status,
                "job_current_attempt_id": str(job.current_attempt_id),
                "replacement_attempt_count": sum(
                    attempt.id != OLD_ATTEMPT_ID for attempt in attempts
                ),
                "nonterminal_attempt_count": sum(
                    attempt.status
                    in {"queued", "extracting", "processing_pages", "finalizing"}
                    for attempt in attempts
                ),
                "page_count": len(pages),
                "pages_attributed_to_old_attempt": sum(
                    page.attempt_id == OLD_ATTEMPT_ID for page in pages
                ),
                "pages_attributed_to_other_attempts": sum(
                    page.attempt_id != OLD_ATTEMPT_ID for page in pages
                ),
                "original_upload_metadata_count": len(files),
                "chapter_id": str(job.chapter_id),
            }
        return result
    finally:
        engine.dispose()


def _task_count(reply: dict, task_id: str) -> int:
    count = 0
    for tasks in reply.values():
        for task in tasks or []:
            request = task.get("request", {}) if isinstance(task, dict) else {}
            if task.get("id") == task_id or request.get("id") == task_id:
                count += 1
    return count


def celery_probe() -> dict:
    inspector = celery_app.control.inspect(timeout=5)
    active = inspector.active() or {}
    reserved = inspector.reserved() or {}
    scheduled = inspector.scheduled() or {}
    settings = get_settings()
    redis_url = settings.redis_url.get_secret_value()
    client = Redis.from_url(redis_url, decode_responses=False)
    queued_count = 0
    for queue in ("system", "celery"):
        for raw in client.lrange(queue, 0, -1):
            try:
                envelope = json.loads(raw)
                headers = envelope.get("headers", {})
                body = envelope.get("body")
                if isinstance(body, str):
                    body = json.loads(body)
                if headers.get("id") == str(OLD_ATTEMPT_ID):
                    queued_count += 1
                elif isinstance(body, dict) and body.get("id") == str(OLD_ATTEMPT_ID):
                    queued_count += 1
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return {
        "worker_online": bool(set(active) | set(reserved) | set(scheduled)),
        "active_count": _task_count(active, str(OLD_ATTEMPT_ID)),
        "reserved_count": _task_count(reserved, str(OLD_ATTEMPT_ID)),
        "scheduled_count": _task_count(scheduled, str(OLD_ATTEMPT_ID)),
        "queued_count": queued_count,
    }


def storage_probe(database: dict) -> dict:
    engine = job_engine()
    try:
        with Session(engine) as db:
            pages = db.scalars(
                select(Page).where(Page.chapter_id == UUID(database["chapter_id"]))
            ).all()
            files = db.scalars(
                select(ChapterFile).where(
                    ChapterFile.chapter_id == UUID(database["chapter_id"])
                )
            ).all()
            page_keys = [page.storage_key for page in pages if page.storage_key]
            thumbnail_keys = [page.thumbnail_key for page in pages if page.thumbnail_key]
            old_pages = [
                page for page in pages if page.attempt_id == OLD_ATTEMPT_ID
            ]
            old_keys = [
                key
                for page in old_pages
                for key in (page.storage_key, page.thumbnail_key)
                if key
            ]
            source_keys = [file.storage_key for file in files if file.storage_key]
        storage = get_storage()

        def exists(key):
            try:
                return bool(key) and bool(storage.exists(key))
            except Exception:
                return False

        return {
            "exact_old_attempt_object_count": sum(exists(key) for key in old_keys),
            "current_chapter_object_count": sum(
                exists(key) for key in page_keys + thumbnail_keys
            ),
            "original_upload_object_exists": any(exists(key) for key in source_keys),
            "page_object_count": sum(exists(page.storage_key) for page in old_pages),
            "thumbnail_object_count": sum(
                exists(page.thumbnail_key) for page in old_pages
            ),
        }
    finally:
        engine.dispose()


def main() -> None:
    database = database_probe()
    if not database.get("database_found"):
        print(json.dumps({"database_found": False}))
        return
    print(json.dumps({"database": database, "celery": celery_probe(), "storage": storage_probe(database)}))


if __name__ == "__main__":
    main()