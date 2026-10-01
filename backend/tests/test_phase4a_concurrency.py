"""A8: real API/worker claims on independent connections, without timing sleeps.

Default: file-backed SQLite (atomic UPDATE/unique constraints, not row locks).
Run the SAME six scenarios on a disposable PostgreSQL database with
TEST_DATABASE_URL=postgresql://... and ALLOW_TEST_DB_RESET=1. The fixture runs
real migrations and resets that database. Do not use a shared database or xdist.
Provider output and queue transport are test doubles; claims, validation,
versions, graph persistence and transactions are production code.
"""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from threading import Event as Signal
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, make_png, setup_chapter

from app import story, story_service
from app.models import (
    Character,
    Event,
    EventCharacter,
    Page,
    Scene,
    SceneCharacter,
    StoryAnalysisJob,
    StoryRelationship,
    StoryVersion,
)
from app.story_providers import LLMResponse
from app.story_schemas import StoryStructuredOutput


@pytest.fixture(scope="module", autouse=True)
def compatible_event_loop():
    """Psycopg async connections require a selector loop on Windows."""
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


class Harness:
    def __init__(self, client, engine, monkeypatch):
        self.client, self.engine, self.monkeypatch = client, engine, monkeypatch
        self.route = setup_chapter(client)
        self.chapter = UUID(self.route.rsplit("/", 1)[1])
        self.headers = csrf(client)
        response = client.post(
            self.route + "/upload",
            headers=self.headers,
            files={"file": ("source.png", make_png(), "image/png")},
        )
        assert response.status_code == 200, response.text
        self.submitted = []
        self.calls = 0
        self.entered, self.release = Signal(), Signal()
        self.release.set()
        self.fail = False
        self.name, self.model = "a8-test", "deterministic"
        monkeypatch.setattr(story, "get_job_queue", lambda: self)
        monkeypatch.setattr(story_service, "configured_provider", lambda: self)
        evidence = [{"page_number": 1, "reason": "A8 source"}]
        common = dict(confidence=0.9, importance=0.5, evidence=evidence)
        self.payload = StoryStructuredOutput.model_validate(
            {
                "characters": [
                    dict(temp_id=n, name=n, description="AI", **common) for n in ("Alpha", "Beta")
                ],
                "scenes": [
                    dict(
                        temp_id="scene",
                        title="Gate",
                        summary="Wait",
                        start_page=1,
                        end_page=1,
                        character_refs=["Alpha", "Beta"],
                        events=[
                            dict(
                                temp_id="event",
                                description="Wait",
                                character_refs=["Alpha"],
                                **common,
                            )
                        ],
                        **common,
                    )
                ],
                "relationships": [
                    dict(
                        temp_id="edge",
                        source="Alpha",
                        target="Beta",
                        source_ref="Alpha",
                        target_ref="Beta",
                        relationship_type="ally",
                        confidence=0.9,
                        evidence=evidence,
                    )
                ],
            }
        )
        # A pre-existing manually reviewed graph, but no analysis job/version.
        # This makes preservation assertions non-vacuous even on initial analysis.
        with Session(engine) as db:
            story_service.persist(db, self.chapter, self.payload.model_copy(deep=True))
            db.commit()
        for row in client.get(self.route + "/characters").json():
            response = client.patch(
                "/api/v1/characters/" + row["id"],
                headers=self.headers,
                json={
                    "description": "Human",
                    "status": "confirmed" if row["name"] == "Alpha" else "rejected",
                },
            )
            assert response.status_code == 200, response.text
        self.original = self.graph()

    def submit(self, job_id):
        self.submitted.append(job_id)

    def generate_structured(self, context):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(20), "provider release timed out"
        if self.fail:
            raise RuntimeError("A8 injected provider failure")
        return LLMResponse(self.payload.model_copy(deep=True), 10, 5)

    def request(self):
        response = self.client.post(self.route + "/story/analyze", headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def graph(self):
        with Session(self.engine) as db:
            rows = {
                model: db.scalars(select(model)).all()
                for model in (
                    Character,
                    Scene,
                    Event,
                    StoryRelationship,
                    SceneCharacter,
                    EventCharacter,
                )
            }
            chars, scenes, events, edges = [
                rows[m] for m in (Character, Scene, Event, StoryRelationship)
            ]
            assert [len(x) for x in (chars, scenes, events, edges)] == [2, 1, 1, 1]
            assert len({c.name for c in chars}) == 2
            assert len({(s.chapter_id, s.scene_index) for s in scenes}) == 1
            assert len({(e.scene_id, e.event_index) for e in events}) == 1
            assert (
                len(
                    {
                        (e.source_character_id, e.target_character_id, e.relationship_type)
                        for e in edges
                    }
                )
                == 1
            )
            cids, sids, eids = [{r.id for r in group} for group in (chars, scenes, events)]
            assert all(e.scene_id in sids for e in events)
            assert all(
                e.source_character_id in cids and e.target_character_id in cids for e in edges
            )
            assert all(r.character_id in cids and r.scene_id in sids for r in rows[SceneCharacter])
            assert all(r.character_id in cids and r.event_id in eids for r in rows[EventCharacter])
            assert len(rows[SceneCharacter]) == 2 and len(rows[EventCharacter]) == 1
            assert all(c.description == "Human" for c in chars)
            assert {c.name: c.status for c in chars} == {"Alpha": "confirmed", "Beta": "rejected"}
            return tuple(tuple(sorted(str(inspect(r).identity) for r in rows[m])) for m in rows)

    def check(self, status, versions):
        assert self.graph() == self.original
        with Session(self.engine) as db:
            jobs = db.scalars(select(StoryAnalysisJob)).all()
            assert len(jobs) == 1 and jobs[0].status == status
            rows = db.scalars(select(StoryVersion)).all()
            assert len(rows) == len({r.fingerprint for r in rows}) == versions
            if status == "completed":
                current = story_service.fingerprint(db, self.chapter)[0]
                assert jobs[0].source_fingerprint == current
                assert sum(r.fingerprint == current for r in rows) == 1
                assert all(len(r.data["characters"]) == 2 for r in rows)

    def changed_source(self):
        with Session(self.engine) as db:
            db.scalar(select(Page)).width += 1
            db.commit()

    def requests_together(self):
        # Pause AFTER both SELECTs execute, so both callers observed the same
        # old job (or absence) before either attempts its INSERT/CAS update.
        barrier = Barrier(2)
        original = AsyncSession.scalar

        async def scalar(session, statement, *args, **kwargs):
            result = await original(session, statement, *args, **kwargs)
            if (
                statement.is_select
                and "story_analysis_jobs" in str(statement)
                and not session.info.get("a8_read")
            ):
                session.info["a8_read"] = True
                await asyncio.to_thread(barrier.wait, 20)
            return result

        with self.monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "scalar", scalar)
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.request) for _ in range(2)]
                results = [f.result(30) for f in futures]
        assert results[0]["id"] == results[1]["id"]
        return results[0]["id"]

    def workers_together(self, job_id):
        barrier = Barrier(2)
        original = story_service.job_engine

        def engine():
            result = original()
            barrier.wait(20)
            return result

        with self.monkeypatch.context() as patch:
            patch.setattr(story_service, "job_engine", engine)
            with Session(self.engine) as blocker, ThreadPoolExecutor(2) as pool:
                lock_pid = None
                if self.engine.dialect.name == "postgresql":
                    pending = blocker.scalar(
                        select(StoryAnalysisJob)
                        .where(
                            StoryAnalysisJob.id == UUID(job_id),
                            StoryAnalysisJob.status == "pending",
                        )
                        .with_for_update()
                    )
                    if pending is not None:
                        lock_pid = blocker.scalar(text("SELECT pg_backend_pid()"))
                futures = [pool.submit(story_service.process_story, job_id) for _ in range(2)]
                try:
                    if lock_pid is not None:
                        # Observe actual database lock waits, not thread start timing.
                        deadline = monotonic() + 20
                        with self.engine.connect().execution_options(
                            isolation_level="AUTOCOMMIT"
                        ) as observer:
                            while True:
                                waiting = observer.scalar(
                                    text(
                                        "SELECT count(*) FROM pg_stat_activity "
                                        "WHERE datname = current_database() "
                                        "AND wait_event_type = 'Lock' "
                                        "AND query LIKE 'UPDATE story_analysis_jobs%' "
                                        "AND cardinality(pg_blocking_pids(pid)) > 0"
                                    )
                                )
                                if waiting == 2:
                                    break
                                assert monotonic() < deadline, "both claims must contend on row"
                finally:
                    blocker.rollback()
                for future in futures:
                    future.result(30)

    def finish(self, job_id, versions):
        before = self.calls
        self.workers_together(job_id)
        assert self.calls == before + 1  # one DB claim winner, not two providers
        self.check("completed", versions)
        # Both API retry and concurrent late duplicate delivery must be no-ops.
        submissions = len(self.submitted)
        assert self.requests_together() == job_id
        self.workers_together(job_id)
        assert len(self.submitted) == submissions and self.calls == before + 1
        self.check("completed", versions)


@pytest.fixture
def harness(client, database, monkeypatch):
    return Harness(client, database[0], monkeypatch)


def test_simultaneous_initial_analysis(harness):
    h = harness
    job = h.requests_together()
    assert len(h.submitted) == 1
    h.check("pending", 0)
    h.finish(job, 1)


def test_simultaneous_reanalysis(harness):
    h = harness
    old = h.request()["id"]
    h.finish(old, 1)
    h.changed_source()
    job = h.requests_together()
    assert job != old and len(h.submitted) == 2
    h.check("pending", 1)
    h.finish(job, 2)
    h.workers_together(old)
    h.check("completed", 2)


def test_analyze_while_previous_job_is_completing(harness):
    h = harness
    job = h.request()["id"]
    entered, release = Signal(), Signal()
    original = story_service.persist

    def persist(db, *args, **kwargs):
        original(db, *args, **kwargs)
        db.flush()  # graph and version are written but NOT committed
        entered.set()
        assert release.wait(20)

    with h.monkeypatch.context() as patch:
        patch.setattr(story_service, "persist", persist)
        with ThreadPoolExecutor(2) as pool:
            worker = pool.submit(story_service.process_story, job)
            try:
                assert entered.wait(20)
                h.check("analyzing", 0)  # separate connection cannot see partial publication
                assert pool.submit(h.request).result(10)["id"] == job
                assert len(h.submitted) == 1
            finally:
                release.set()
            worker.result(30)
    h.check("completed", 1)
    h.workers_together(job)
    assert h.requests_together() == job
    assert h.calls == 1 and len(h.submitted) == 1
    h.check("completed", 1)


def test_reanalyze_while_analysis_is_active(harness):
    h = harness
    h.finish(h.request()["id"], 1)
    h.changed_source()
    job = h.request()["id"]
    h.entered.clear()
    h.release.clear()
    with ThreadPoolExecutor(2) as pool:
        worker = pool.submit(story_service.process_story, job)
        try:
            assert h.entered.wait(20)
            h.check("analyzing", 1)
            assert pool.submit(h.request).result(10)["id"] == job
            assert len(h.submitted) == 2
        finally:
            h.release.set()
        worker.result(30)
    h.check("completed", 2)
    h.workers_together(job)
    assert h.requests_together() == job
    assert h.calls == 2 and len(h.submitted) == 2
    h.check("completed", 2)


def test_retry_after_failed_job(harness):
    h = harness
    old = h.request()["id"]
    h.fail = True
    h.workers_together(old)
    assert h.calls == 1
    h.check("failed", 0)
    h.fail = False
    job = h.requests_together()
    assert job != old and len(h.submitted) == 2
    h.check("pending", 0)
    h.finish(job, 1)
    h.workers_together(old)
    assert h.calls == 2
    h.check("completed", 1)


def test_duplicate_worker_delivery(harness):
    h = harness
    job = h.request()["id"]
    h.check("pending", 0)
    h.finish(job, 1)
    assert h.calls == 1 and len(h.submitted) == 1
