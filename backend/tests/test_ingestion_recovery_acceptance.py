"""Deterministic ingestion-recovery acceptance coverage.

These tests deliberately exercise the service boundary instead of using sleeps.
For PostgreSQL concurrency evidence, run with a disposable TEST_DATABASE_URL and
ALLOW_TEST_DB_RESET=1; the normal fixture remains useful for deterministic fencing
and cleanup checks.
"""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, make_png, setup_chapter

from app import ingestion
from app.ingestion_service import (
    ACTIVE,
    Fenced,
    attempt_prefix,
    cleanup_attempt,
    finalize,
    owned,
    recover_attempt,
    transition,
    utcnow,
)
from app.jobs import LocalJobQueue, job_engine
from app.models import Chapter, ChapterFile, IngestionAttempt, Job, Page, Project
from app.storage import get_storage


@pytest.fixture(scope="module", autouse=True)
def compatible_event_loop():
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


def upload(client):
    route = setup_chapter(client)
    response = client.post(
        route + "/upload",
        files={"file": ("source.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    return route, UUID(response.json()["job_id"])


def stale_and_replace(database, job_id):
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        old = db.get(IngestionAttempt, job.current_attempt_id)
        old.status = "processing_pages"
        old.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        old_id = old.id
    new_id = recover_attempt(job_id, old_id)
    assert new_id != old_id
    return old_id, new_id


def state(database, job_id):
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        chapter = db.get(Chapter, job.chapter_id)
        attempts = db.scalars(
            select(IngestionAttempt).where(IngestionAttempt.chapter_id == chapter.id)
        ).all()
        pages = db.scalars(select(Page).where(Page.chapter_id == chapter.id)).all()
        return {
            "job": job.status,
            "chapter": chapter.status,
            "current": job.current_attempt_id,
            "active": [a.id for a in attempts if a.status in ACTIVE],
            "pages": [(p.id, p.attempt_id, p.page_number, p.status) for p in pages],
        }


def test_stale_attempt_owned_boundary_and_cleanup(client, database):
    """Boundary probes; not independent coverage of every production call site."""
    _, job_id = upload(client)
    old_id, new_id = stale_and_replace(database, job_id)

    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        chapter = db.get(Chapter, job.chapter_id)
        project = db.get(Project, chapter.project_id)
        prefix = (
            f"users/{project.user_id}/projects/{project.id}/chapters/{chapter.id}"
        )
        db.add(
            Page(
                id=uuid4(),
                chapter_id=chapter.id,
                attempt_id=new_id,
                page_number=1,
                storage_key=f"{prefix}/attempts/{new_id}/pages/b.jpg",
                thumbnail_key=f"{prefix}/attempts/{new_id}/thumbnails/b.jpg",
                width=32,
                height=48,
                format="JPEG",
                file_size=1,
                status="ready",
            )
        )
        db.commit()
        b_snapshot = state(database, job_id)

    storage = get_storage()
    b_keys = [
        f"{prefix}/attempts/{new_id}/pages/b.jpg",
        f"{prefix}/attempts/{new_id}/thumbnails/b.jpg",
    ]
    for key in b_keys:
        storage.put(key, b"b", "image/jpeg")

    def rejected(label, operation):
        with pytest.raises(Fenced):
            operation()
        after = state(database, job_id)
        assert after == b_snapshot, label
        assert all(storage.exists(key) for key in b_keys), label

    engine = job_engine()
    try:
        rejected("page insert", lambda: _owned_mutation(engine, job_id, old_id, "page"))
        rejected("page metadata", lambda: _owned_mutation(engine, job_id, old_id, "metadata"))
        rejected("chapter status", lambda: _owned_mutation(engine, job_id, old_id, "chapter"))
        rejected("job status", lambda: _owned_mutation(engine, job_id, old_id, "job"))
        rejected("job completion", lambda: _owned_mutation(engine, job_id, old_id, "complete"))
        rejected("finalize", lambda: finalize(engine, job_id, old_id, [], storage))
        rejected("current attempt", lambda: _owned_mutation(engine, job_id, old_id, "current"))
        rejected(
            "successful chapter publication",
            lambda: _owned_mutation(engine, job_id, old_id, "publish"),
        )
    finally:
        engine.dispose()

    cleanup_attempt(old_id)
    assert all(storage.exists(key) for key in b_keys)
    assert state(database, job_id) == b_snapshot


def _owned_mutation(engine, job_id, attempt_id, kind):
    with owned(engine, job_id, attempt_id) as (db, job, attempt):
        if kind == "page":
            db.add(Page(
                id=uuid4(), chapter_id=job.chapter_id, attempt_id=attempt.id,
                page_number=99, storage_key="invalid/pages/a", thumbnail_key="invalid/thumb/a",
                width=1, height=1, format="JPEG", file_size=1, status="ready",
            ))
        elif kind == "metadata":
            page = db.scalar(select(Page).where(Page.attempt_id == job.current_attempt_id))
            assert page is not None
            page.status = "corrupt"
        elif kind == "chapter":
            db.get(Chapter, job.chapter_id).status = "ready"
        elif kind == "job":
            job.status = "completed"
        elif kind == "current":
            job.current_attempt_id = attempt.id
        elif kind in {"complete", "publish"}:
            transition(db, job, attempt, "completed")


def test_interrupted_cleanup_is_resumable_and_exactly_scoped(client, database, monkeypatch):
    _, job_id = upload(client)
    old_id, new_id = stale_and_replace(database, job_id)
    storage = get_storage()
    with Session(database[0]) as db:
        attempt = db.get(IngestionAttempt, old_id)
        attempt.status = "abandoned"
        attempt.cleanup_status = "pending"
        db.commit()
        prefix_a = attempt_prefix(db, attempt)
        prefix_b = attempt_prefix(db, db.get(IngestionAttempt, new_id))
        source = db.get(ChapterFile, db.get(Job, job_id).file_id).storage_key
    old_keys = [f"{prefix_a}pages/{n}.jpg" for n in range(3)]
    b_key = f"{prefix_b}pages/current.jpg"
    for key in old_keys + [b_key]:
        storage.put(key, b"x", "image/jpeg")
    original = storage.content_hash(source)
    real = storage.delete_prefix
    first = {"deleted": 0}

    def interrupted(prefix):
        assert prefix == prefix_a
        keys = storage.list_prefix(prefix)
        for key in keys[:1]:
            storage.delete(key)
            first["deleted"] += 1
        raise RuntimeError("deterministic cleanup interruption")

    monkeypatch.setattr("app.ingestion_service.get_storage", lambda: storage)
    monkeypatch.setattr(storage, "delete_prefix", interrupted)
    cleanup_attempt(old_id)
    assert first["deleted"] == 1
    assert len(storage.list_prefix(prefix_a)) == 2
    with Session(database[0]) as db:
        assert db.get(IngestionAttempt, old_id).cleanup_status == "pending"

    monkeypatch.setattr(storage, "delete_prefix", real)
    cleanup_attempt(old_id)
    cleanup_attempt(old_id)
    assert storage.list_prefix(prefix_a) == []
    assert storage.list_prefix(prefix_b) == [b_key]
    assert storage.content_hash(source) == original


def test_concurrent_recovery_has_one_database_winner(client, database):
    _, job_id = upload(client)
    old_id, _ = stale_and_replace(database, job_id)
    # Make a fresh stale attempt for this race: the previous replacement is current.
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        current = db.get(IngestionAttempt, job.current_attempt_id)
        current.lease_expires_at = utcnow() - timedelta(seconds=1)
        current.status = "processing_pages"
        db.commit()
        old_id = current.id
    barrier = Barrier(2)

    def compete():
        barrier.wait(timeout=20)
        return recover_attempt(job_id, old_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0] == results[1]
    final = state(database, job_id)
    assert len(final["active"]) == 1
    assert final["current"] == results[0]


def test_broker_submission_failure_leaves_truthful_recoverable_db(client, database, monkeypatch):
    route = setup_chapter(client)

    class BrokenQueue:
        def submit(self, *_args):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(ingestion, "get_job_queue", lambda: BrokenQueue())
    response = client.post(
        route + "/upload",
        files={"file": ("source.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert response.status_code == 503
    chapter_id = UUID(route.rsplit("/", 1)[1])
    with Session(database[0]) as db:
        chapter = db.get(Chapter, chapter_id)
        job = db.scalar(select(Job).where(Job.chapter_id == chapter_id))
        assert chapter.status == "uploaded"
        assert job.status == "queued"
        assert db.get(IngestionAttempt, job.current_attempt_id).status == "queued"

    monkeypatch.setattr(ingestion, "get_job_queue", lambda: LocalJobQueue())
    LocalJobQueue().submit(str(job.id), str(job.current_attempt_id))
    with Session(database[0]) as db:
        assert db.get(Job, job.id).status == "completed"
        assert db.get(Chapter, chapter_id).status == "ready"


@pytest.mark.parametrize("competitor", ["manual", "recovery"])
def test_manual_retry_races(client, database, monkeypatch, competitor):
    """Both operations reach recovery with the same old identity before locking."""
    from app import ingestion_service

    route = setup_chapter(client)
    submissions = []

    class DeferredQueue:
        def submit(self, *args):
            submissions.append(args)

    monkeypatch.setattr(ingestion, "get_job_queue", lambda: DeferredQueue())
    response = client.post(
        route + "/upload",
        files={"file": ("source.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert response.status_code == 200
    job_id = UUID(response.json()["job_id"])
    with Session(database[0]) as db:
        job = db.get(Job, job_id)
        old = db.get(IngestionAttempt, job.current_attempt_id)
        old.status = "failed" if competitor == "manual" else "processing_pages"
        old.lease_expires_at = utcnow() - timedelta(seconds=1)
        old_id = old.id
        old_prefix = attempt_prefix(db, old)
        source = db.get(ChapterFile, job.file_id).storage_key
        db.commit()
    storage = get_storage()
    original_hash = storage.content_hash(source)
    storage.put(old_prefix + "pages/orphan.jpg", b"old", "image/jpeg")
    barrier = Barrier(2)
    real_recover = ingestion_service.recover_attempt

    def synchronized_recovery(identifier, attempt):
        assert identifier == job_id and attempt == old_id
        barrier.wait(timeout=20)
        return real_recover(identifier, attempt)

    monkeypatch.setattr(ingestion_service, "recover_attempt", synchronized_recovery)
    headers = csrf(client)

    def manual():
        result = client.post(route + "/processing-retry", headers=headers)
        assert result.status_code == 200, result.text
        return UUID(result.json()["attempt_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manual)
        second = pool.submit(
            manual if competitor == "manual"
            else lambda: synchronized_recovery(job_id, old_id)
        )
        winners = [first.result(timeout=30), second.result(timeout=30)]
    assert winners[0] == winners[1] != old_id
    assert state(database, job_id)["active"] == [winners[0]]
    LocalJobQueue().submit(str(job_id), str(winners[0]))
    before = state(database, job_id)
    cleanup_attempt(old_id)
    assert real_recover(job_id, old_id) == winners[0]
    assert state(database, job_id) == before
    assert before["job"] == "completed" and before["chapter"] == "ready"
    assert before["active"] == []
    assert len(before["pages"]) == 1
    assert before["pages"][0][1] == winners[0]
    assert storage.list_prefix(old_prefix) == []
    assert storage.content_hash(source) == original_hash
    with Session(database[0]) as db:
        attempts = db.scalars(select(IngestionAttempt).where(
            IngestionAttempt.job_id == job_id
        )).all()
        assert len(attempts) == 2
        assert sum(a.status == "completed" for a in attempts) == 1
        page = db.scalar(select(Page).where(Page.attempt_id == winners[0]))
        assert storage.exists(page.storage_key) and storage.exists(page.thumbnail_key)

