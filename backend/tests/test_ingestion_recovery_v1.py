from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, make_png, setup_chapter

from app.ingestion_service import (
    Fenced,
    cleanup_attempt,
    finalize,
    process_ingestion,
    recover_attempt,
    utcnow,
)
from app.models import Chapter, IngestionAttempt, Job, Page, Project
from app.storage import get_storage


def upload(client):
    route = setup_chapter(client)
    response = client.post(
        route + "/upload",
        files={"file": ("source.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    return route, UUID(response.json()["job_id"])


def test_attempt_has_lease_and_pages_are_attempt_scoped(client, database):
    route, job_id = upload(client)
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        attempt = db.get(IngestionAttempt, job.current_attempt_id)
        pages = db.scalars(select(Page).where(Page.attempt_id == attempt.id)).all()
        assert attempt.generation == 1
        assert attempt.status == "completed"
        assert attempt.lease_expires_at is not None
        assert pages and all(f"/attempts/{attempt.id}/" in page.storage_key for page in pages)
        assert all("/original/" not in page.storage_key for page in pages)


def test_expired_attempt_recovery_is_fenced_and_cleanup_isolated(client, database):
    route, job_id = upload(client)
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        attempt_a = db.get(IngestionAttempt, job.current_attempt_id)
        attempt_a.status = "processing_pages"
        attempt_a.lease_expires_at = utcnow() - timedelta(seconds=1)
        attempt_a.last_heartbeat_at = utcnow() - timedelta(minutes=20)
        db.commit()
        old_id = attempt_a.id

    new_id = recover_attempt(job_id, old_id)
    assert new_id != old_id
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        chapter = db.get(Chapter, job.chapter_id)
        project = db.get(Project, chapter.project_id)
        old = db.get(IngestionAttempt, old_id)
        new = db.get(IngestionAttempt, new_id)
        assert job.current_attempt_id == new_id
        assert old.status == "abandoned"
        assert new.status == "queued" and new.generation == old.generation + 1
        prefix = f"users/{project.user_id}/projects/{project.id}/chapters/{chapter.id}"

    # A stale worker has no claim path; cleanup can only sweep A's exact prefix.
    process_key = f"{prefix}/attempts/{old_id}/pages/a.jpg"
    newer_key = f"{prefix}/attempts/{new_id}/pages/b.jpg"
    storage = get_storage()
    storage.put(process_key, b"a", "image/jpeg")
    storage.put(newer_key, b"b", "image/jpeg")
    cleanup_attempt(old_id)
    process_ingestion(job_id, old_id)
    with pytest.raises(Fenced):
        finalize(database[0], job_id, old_id, [], storage)
    assert not storage.exists(process_key)
    assert storage.exists(newer_key)
    assert client.get(route + "/processing-status").json()["status"] != "ready"


def test_retry_endpoint_does_not_create_second_active_attempt(client, database, monkeypatch):
    route, job_id = upload(client)
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        attempt = db.get(IngestionAttempt, job.current_attempt_id)
        attempt.status = "failed"
        db.commit()
    from app.jobs import LocalJobQueue

    monkeypatch.setattr(LocalJobQueue, "submit", lambda *args: None)
    first = client.post(route + "/processing-retry", headers=csrf(client))
    assert first.status_code == 200, first.text
    second = client.post(route + "/processing-retry", headers=csrf(client))
    assert second.status_code == 200
    assert first.json() == second.json()
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        active = db.scalars(
            select(IngestionAttempt).where(
                IngestionAttempt.chapter_id == job.chapter_id,
                IngestionAttempt.status.in_(
                    ("queued", "extracting", "processing_pages", "finalizing")
                ),
            )
        ).all()
        assert len(active) == 1
