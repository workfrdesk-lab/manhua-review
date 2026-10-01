"""Real HTTP, queue boundary, worker transactions and deterministic providers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ingestion_local import make_png
from test_projects import register, write_headers
from test_script import source

from app import script_service
from app.models import (
    ScriptEvidence,
    ScriptGenerationJob,
    ScriptSegment,
    ScriptVersion,
    StoryAudit,
    StoryVersion,
)
from app.script_providers import DeterministicScriptProvider
from app.script_schemas import ScriptProfile
from app.story_providers import GeminiProvider, OpenAIProvider


@pytest.fixture
def harness(client, database, monkeypatch):
    register(client, "final-script@example.com")
    headers = write_headers(client)
    project = client.post("/api/v1/projects", json={"name": "Final"}, headers=headers).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Chapter"}, headers=headers
    ).json()
    route = f"/api/v1/chapters/{chapter['id']}"
    assert (
        client.post(
            route + "/upload",
            files={"file": ("page.png", make_png(), "image/png")},
            headers=headers,
        ).status_code
        == 200
    )
    page = client.get(route + "/pages").json()[0]
    story = source()[0]
    for ev in (story.scenes[0].evidence[0], story.scenes[0].events[0].evidence[0]):
        ev.page_id = UUID(page["id"])
    with Session(database[0]) as db:
        version = StoryVersion(
            chapter_id=UUID(chapter["id"]),
            fingerprint="s" * 64,
            data=story.model_dump(mode="json"),
            decisions=[],
        )
        db.add(version)
        db.commit()
        version_id = str(version.id)
    submitted = []
    monkeypatch.setattr(
        "app.script.get_job_queue", lambda: SimpleNamespace(submit=submitted.append)
    )
    monkeypatch.setattr("app.script.configured_script_provider", DeterministicScriptProvider)
    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)
    return SimpleNamespace(
        client=client,
        engine=database[0],
        headers=headers,
        route=route,
        chapter=chapter,
        project=project,
        story=story,
        version_id=version_id,
        submitted=submitted,
    )


def enqueue(h):
    response = h.client.post(
        h.route + "/scripts", json={"story_version_id": h.version_id}, headers=h.headers
    )
    assert response.status_code == 201, response.text
    return response.json()["job"]


def status(h, job):
    response = h.client.get(f"/api/v1/script-jobs/{job['id']}/status")
    assert response.status_code == 200, response.text
    return response.json()


def counts(h):
    with Session(h.engine) as db:
        return [
            db.scalar(select(func.count()).select_from(model))
            for model in (ScriptVersion, ScriptSegment, ScriptEvidence)
        ]


@pytest.mark.parametrize("adapter", [OpenAIProvider, GeminiProvider])
@pytest.mark.parametrize("mode", ["valid", "malformed", "exception"])
def test_mocked_script_adapter_and_worker(harness, monkeypatch, adapter, mode):
    h = harness
    provider = adapter(model="fixture-model", api_key="fixture-key")
    expected = DeterministicScriptProvider().generate_script(h.story, ScriptProfile())
    calls = []

    def request(url, headers, payload):
        calls.append((url, payload))
        if mode == "exception":
            raise RuntimeError("private-provider-token")
        text = expected.model_dump_json() if mode == "valid" else "private-provider-token"
        if adapter is OpenAIProvider:
            assert headers["Authorization"] == "Bearer fixture-key"
            assert payload["response_format"] == {"type": "json_object"}
            return {"choices": [{"message": {"content": text}}]}
        assert headers["x-goog-api-key"] == "fixture-key"
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    monkeypatch.setattr("app.story_providers._json_request", request)
    if mode == "valid":
        assert provider.generate_script(h.story, ScriptProfile()) == expected
    else:
        with pytest.raises(ValueError if mode == "malformed" else RuntimeError):
            provider.generate_script(h.story, ScriptProfile())
    monkeypatch.setattr("app.script.configured_script_provider", lambda: provider)
    monkeypatch.setattr(script_service, "configured_script_provider", lambda: provider)
    job = enqueue(h)
    script_service.process_script(job["id"])
    result = status(h, job)
    assert len(calls) == 2
    assert result["status"] == ("completed" if mode == "valid" else "failed")
    assert counts(h) == ([1, 1, 1] if mode == "valid" else [0, 0, 0])
    assert "private-provider-token" not in str(result)


@pytest.mark.parametrize(
    "mode", ["valid", "malformed", "exception", "grounding", "config", "flush"]
)
def test_worker_provider_transaction_matrix(harness, monkeypatch, mode):
    h = harness
    job = enqueue(h)
    assert job["status"] == "queued"
    assert h.submitted == [f"script:{job['id']}"]

    class Provider(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            assert counts(h) == [0, 0, 0]
            with Session(h.engine) as db:
                assert db.get(ScriptGenerationJob, UUID(job["id"])).status == "running"
            if mode == "exception":
                raise RuntimeError("private token must not leak")
            if mode == "malformed":
                return {"private": "token"}
            result = super().generate_script(story, profile)
            if mode == "grounding":
                result.segments[0].evidence[0].event_ref = "invented"
            return result

    provider = Provider()
    if mode == "config":
        provider.model = "different-model"
    monkeypatch.setattr(script_service, "configured_script_provider", lambda: provider)
    original = script_service.audit

    def audit(db, record, action):
        if mode == "flush" and action == "script_generation_completed":
            db.flush()
            assert counts(h) == [0, 0, 0], "uncommitted publication became visible"
            raise RuntimeError("private commit failure")
        original(db, record, action)

    monkeypatch.setattr(script_service, "audit", audit)
    script_service.process_script(job["id"])
    current = status(h, job)
    assert current["status"] == ("completed" if mode == "valid" else "failed")
    assert counts(h) == ([1, 1, 1] if mode == "valid" else [0, 0, 0])
    if mode != "valid":
        assert (
            current["error"]
            == "Script generation failed; provider or source validation unavailable"
        )
    with Session(h.engine) as db:
        audits = db.scalars(select(StoryAudit).where(StoryAudit.entity_id == UUID(job["id"]))).all()
        assert {a.action for a in audits} == {
            "script_generation_requested",
            "script_generation_started",
            "script_generation_completed" if mode == "valid" else "script_generation_failed",
        }
        for audit_row in audits:
            assert audit_row.actor_id
            assert str(audit_row.chapter_id) == h.chapter["id"]
            assert str(audit_row.version_id) == h.version_id
            assert audit_row.data["job_id"] == job["id"]


def test_interruption_recovery_retry_and_duplicate_delivery(harness, monkeypatch):
    h = harness
    job = enqueue(h)

    class Interrupted(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise SystemExit("simulated process exit after persisted claim")

    monkeypatch.setattr(script_service, "configured_script_provider", Interrupted)
    with pytest.raises(SystemExit):
        script_service.process_script(job["id"])
    assert status(h, job)["status"] == "running"
    assert script_service.recover_script_jobs() == []
    future = datetime.now(UTC) + timedelta(minutes=16)
    assert script_service.recover_script_jobs(now=future) == [job["id"]]
    assert script_service.recover_script_jobs(now=future) == []
    assert status(h, job)["status"] == "failed"
    retry = h.client.post(f"/api/v1/script-jobs/{job['id']}/retry", headers=h.headers)
    assert retry.status_code == 202, retry.text
    next_job = retry.json()
    assert next_job["attempt"] == 2
    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)
    script_service.process_script(next_job["id"])
    assert status(h, next_job)["status"] == "completed"
    monkeypatch.setattr(script_service, "configured_script_provider", Interrupted)
    for identity in (job["id"], next_job["id"], next_job["id"]):
        script_service.process_script(identity)
    assert counts(h) == [1, 1, 1]
    assert status(h, job)["status"] == "failed"


def test_recovery_fences_a_worker_still_inside_provider(harness, monkeypatch):
    h = harness
    job = enqueue(h)
    entered, release = Event(), Event()

    class Paused(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            entered.set()
            assert release.wait(20)
            return super().generate_script(story, profile)

    monkeypatch.setattr(script_service, "configured_script_provider", Paused)
    with ThreadPoolExecutor() as pool:
        worker = pool.submit(script_service.process_script, job["id"])
        assert entered.wait(10)
        try:
            assert script_service.recover_script_jobs(
                now=datetime.now(UTC) + timedelta(minutes=16)
            ) == [job["id"]]
        finally:
            release.set()
        worker.result(timeout=20)
    assert counts(h) == [0, 0, 0]
    assert status(h, job)["status"] == "failed"


def test_concurrent_generation_and_retry(harness, monkeypatch):
    h = harness

    def concurrent(call):
        barrier = Barrier(4)

        def run(_):
            barrier.wait(timeout=10)
            return call()

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(run, range(4)))

    jobs = concurrent(lambda: enqueue(h))
    assert len({j["id"] for j in jobs}) == 1
    assert len(h.submitted) == 1

    class Failed(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise ValueError("failure")

    monkeypatch.setattr(script_service, "configured_script_provider", Failed)
    script_service.process_script(jobs[0]["id"])

    def retry():
        response = h.client.post(f"/api/v1/script-jobs/{jobs[0]['id']}/retry", headers=h.headers)
        assert response.status_code == 202, response.text
        return response.json()

    retries = concurrent(retry)
    assert len({j["id"] for j in retries}) == 1
    assert {j["attempt"] for j in retries} == {2}
    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)
    concurrent(lambda: script_service.process_script(retries[0]["id"]))
    assert counts(h) == [1, 1, 1]
    with Session(h.engine) as db:
        assert db.scalar(select(func.count()).select_from(ScriptGenerationJob)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(ScriptGenerationJob)
                .where(ScriptGenerationJob.status == "completed")
            )
            == 1
        )


def test_generate_after_failed_attempt_creates_next_attempt(harness, monkeypatch):
    h = harness
    first = enqueue(h)

    class Failed(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise ValueError("first attempt failed")

    monkeypatch.setattr(script_service, "configured_script_provider", Failed)
    script_service.process_script(first["id"])
    assert status(h, first)["status"] == "failed"

    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)
    second = enqueue(h)
    assert second["id"] != first["id"]
    assert second["attempt"] == 2
    assert second["status"] == "queued"

    with Session(h.engine) as db:
        history = db.scalars(
            select(ScriptGenerationJob).order_by(ScriptGenerationJob.attempt)
        ).all()
        assert [(job.attempt, job.status) for job in history] == [(1, "failed"), (2, "queued")]
    script_service.process_script(second["id"])
    completed = status(h, second)
    assert completed["status"] == "completed"
    assert enqueue(h)["id"] == second["id"]
    script_service.process_script(second["id"])
    assert status(h, second) == completed
    assert counts(h) == [1, 1, 1]


def test_concurrent_generate_after_failure_has_one_next_attempt(harness, monkeypatch):
    h = harness
    first = enqueue(h)

    class Failed(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise ValueError("first attempt failed")

    monkeypatch.setattr(script_service, "configured_script_provider", Failed)
    script_service.process_script(first["id"])
    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)

    barrier = Barrier(4)

    def generate(_):
        barrier.wait(timeout=10)
        return enqueue(h)

    with ThreadPoolExecutor(max_workers=4) as pool:
        generated = list(pool.map(generate, range(4)))

    assert len({job["id"] for job in generated}) == 1
    assert {job["attempt"] for job in generated} == {2}
    with Session(h.engine) as db:
        history = db.scalars(
            select(ScriptGenerationJob).order_by(ScriptGenerationJob.attempt)
        ).all()
        assert len(history) == 2
        assert history[0].status == "failed"
        assert history[1].attempt == 2


def test_metadata_and_review_synchronization(harness):
    h = harness
    job = enqueue(h)
    script_service.process_script(job["id"])
    detail = f"/api/v1/scripts/{status(h, job)['script_version_id']}"
    original = h.client.get(detail).json()
    for state in ("confirmed", "needs_review", "rejected", "needs_review", "confirmed"):
        assert (
            h.client.patch(
                detail + "/review", json={"status": state}, headers=h.headers
            ).status_code
            == 200
        )
        value = h.client.get(detail).json()
        assert value["status"] == value["data"]["status"] == state
        assert {s["status"] for s in value["segments"]} == {state}
        assert {s["status"] for s in value["data"]["segments"]} == {state}
    patch = {
        "title": "Human title",
        "hook": "",
        "intro": "",
        "outro": original["segments"][0]["narration_text"],
    }
    assert h.client.patch(detail + "/metadata", json=patch, headers=h.headers).status_code == 200
    edited = h.client.get(detail).json()
    assert edited["status"] == "confirmed"
    assert edited["evidence"] == original["evidence"]
    for field, value in patch.items():
        assert edited["data"][field] == value
    for bad in ({"hook": "ungrounded new claim"}, {"title": None}, {"data": {}}, {}):
        assert h.client.patch(detail + "/metadata", json=bad, headers=h.headers).status_code == 422
    assert h.client.get(detail).json() == edited
    with Session(h.engine) as db:
        audit = db.scalar(select(StoryAudit).where(StoryAudit.action == "script_metadata_edited"))
        assert audit.actor_id and str(audit.version_id) == h.version_id
        assert audit.data["after"] == patch
        assert audit.data["before"]["title"] == original["title"]
        reviews = db.scalars(select(StoryAudit).where(StoryAudit.action == "review")).all()
        assert {(a.data["before"], a.data["after"]) for a in reviews} == {
            ("needs_review", "confirmed"),
            ("confirmed", "needs_review"),
            ("needs_review", "rejected"),
            ("rejected", "needs_review"),
        }
        for row in reviews:
            assert str(row.chapter_id) == h.chapter["id"]
            assert str(row.version_id) == h.version_id
            assert row.data["script_version_id"] == original["id"]


def test_phase7a_acceptance_27_steps(harness, monkeypatch):
    # 1-3: fixture creates project/chapter, uploads a page, obtains exact snapshot.
    h = harness
    job = enqueue(h)  # 4-5
    assert job["status"] == "queued" and h.submitted
    script_service.process_script(job["id"])  # 6
    job = status(h, job)
    detail = f"/api/v1/scripts/{job['script_version_id']}"
    script = h.client.get(detail).json()
    assert script["status"] == "needs_review"  # 7-8
    assert len(script["segments"]) == len(script["evidence"]) == 1  # 9-10
    assert script["story_version_id"] == h.version_id
    assert script["evidence"][0]["event_ref"] == h.story.scenes[0].events[0].temp_id  # 11
    segment_route = f"/api/v1/script-segments/{script['segments'][0]['id']}"
    assert (
        h.client.patch(
            segment_route, json={"evidence": [{"page_id": str(uuid4())}]}, headers=h.headers
        ).status_code
        == 422
    )  # 12: immutable evidence cannot be substituted.
    assert (
        h.client.patch(
            segment_route, json={"narration_text": "The traveler arrives."}, headers=h.headers
        ).status_code
        == 200
    )  # 13
    assert (
        h.client.patch(
            detail + "/metadata", json={"title": "Reviewed arrival"}, headers=h.headers
        ).status_code
        == 200
    )  # 14
    with Session(h.engine) as db:
        actions = set(db.scalars(select(StoryAudit.action)).all())
        assert {"script_segment_edited", "script_metadata_edited"} <= actions  # 15
    assert (
        h.client.patch(
            detail + "/review", json={"status": "confirmed"}, headers=h.headers
        ).status_code
        == 200
    )  # 16
    confirmed = h.client.get(detail).json()
    assert confirmed["status"] == "confirmed"  # 17
    with Session(h.engine) as db:
        newer = StoryVersion(
            chapter_id=UUID(h.chapter["id"]),
            fingerprint="n" * 64,
            data=h.story.model_dump(mode="json"),
            decisions=[],
        )
        db.add(newer)
        db.commit()
        h.version_id = str(newer.id)  # 18
    new_job = enqueue(h)
    script_service.process_script(new_job["id"])  # 19
    assert h.client.get(detail).json() == confirmed  # 20

    class Failure(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise RuntimeError("private-provider-token")

    monkeypatch.setattr(script_service, "configured_script_provider", Failure)  # 21
    response = h.client.post(
        h.route + "/scripts",
        json={"story_version_id": h.version_id, "profile": {"language": "ar"}},
        headers=h.headers,
    )
    assert response.status_code == 201
    failed = response.json()["job"]
    script_service.process_script(failed["id"])
    failed = status(h, failed)
    assert failed["status"] == "failed"  # 22
    assert "private-provider-token" not in failed["error"]  # 23
    response = h.client.post(f"/api/v1/script-jobs/{failed['id']}/retry", headers=h.headers)
    assert response.status_code == 202
    retry = response.json()
    assert retry["attempt"] == failed["attempt"] + 1  # 24
    monkeypatch.setattr(script_service, "configured_script_provider", DeterministicScriptProvider)
    script_service.process_script(retry["id"])
    final = status(h, retry)
    assert final["status"] == "completed"  # 25
    assert h.client.get(detail).json() == confirmed
    jobs = h.client.get(h.route + "/script-jobs").json()
    assert next(j for j in jobs if j["id"] == retry["id"])["status"] == "completed"
    h.client.post("/api/v1/auth/logout", headers=h.headers)
    register(h.client, "acceptance-outsider@example.com")
    assert h.client.get(detail).status_code == 404  # 26
    assert h.client.get(f"/api/v1/script-jobs/{retry['id']}/status").status_code == 404
    outsider_headers = write_headers(h.client)
    assert (
        h.client.patch(
            segment_route, json={"confidence": 0.5}, headers=outsider_headers
        ).status_code
        == 404
    )
    assert (
        h.client.patch(
            detail + "/review", json={"status": "rejected"}, headers=outsider_headers
        ).status_code
        == 404
    )
    h.client.post("/api/v1/auth/logout", headers=outsider_headers)
    assert (
        h.client.post(
            "/api/v1/auth/login",
            json={"email": "final-script@example.com", "password": "correct horse battery staple"},
        ).status_code
        == 200
    )
    assert h.client.get(detail).json() == confirmed
    assert status(h, retry) == final
    assert final["script_version_id"] and final["error"] is None
    with Session(h.engine) as db:
        rows = db.scalars(select(StoryAudit)).all()
        assert {
            "script_generation_requested",
            "script_generation_started",
            "script_generation_completed",
            "script_generation_failed",
            "script_generation_retry_requested",
            "script_segment_edited",
            "script_metadata_edited",
        } <= {row.action for row in rows}
        for row in rows:
            assert str(row.chapter_id) == h.chapter["id"]
            assert row.actor_id and row.version_id
            if row.entity_type == "script_generation_job":
                attempt = db.get(ScriptGenerationJob, row.entity_id)
                assert row.data["job_id"] == str(attempt.id)
                assert row.data["attempt"] == attempt.attempt
                assert row.data["story_version_id"] == str(attempt.story_version_id)
                if row.action == "script_generation_completed":
                    assert row.data["script_version_id"] == str(attempt.script_version_id)
            else:
                assert row.data["script_version_id"] == script["id"]
                assert row.data["story_version_id"] == str(row.version_id)


@pytest.mark.parametrize("identity", ["owner", "outsider", "anonymous", "missing"])
def test_endpoint_authorization_matrix(harness, identity):
    h = harness
    job = enqueue(h)
    script_service.process_script(job["id"])
    script_id = status(h, job)["script_version_id"]
    segment_id = h.client.get(f"/api/v1/scripts/{script_id}").json()["segments"][0]["id"]
    chapter_route = h.route
    story_id, job_id = h.version_id, job["id"]
    if identity == "missing":
        chapter_route = f"/api/v1/chapters/{uuid4()}"
        story_id, script_id, segment_id, job_id = [str(uuid4()) for _ in range(4)]
    elif identity != "owner":
        h.client.post("/api/v1/auth/logout", headers=h.headers)
        if identity == "outsider":
            register(h.client, "matrix-outsider@example.com")
            h.headers = write_headers(h.client)
            # A separate project does not grant cross-project access.
            assert (
                h.client.post(
                    "/api/v1/projects", json={"name": "Other project"}, headers=h.headers
                ).status_code
                == 201
            )
    requests = [
        ("POST", chapter_route + "/scripts", {"story_version_id": story_id}),
        ("GET", chapter_route + "/scripts", None),
        ("GET", chapter_route + "/script-jobs", None),
        ("GET", f"/api/v1/scripts/{script_id}", None),
        ("GET", f"/api/v1/scripts/{script_id}/status", None),
        ("PATCH", f"/api/v1/scripts/{script_id}/review", {"status": "confirmed"}),
        ("PATCH", f"/api/v1/scripts/{script_id}/metadata", {"title": "Owner edit"}),
        ("GET", f"/api/v1/script-jobs/{job_id}/status", None),
        ("POST", f"/api/v1/script-jobs/{job_id}/retry", None),
        ("PATCH", f"/api/v1/script-segments/{segment_id}", {"confidence": 0.5}),
    ]
    for method, route, payload in requests:
        response = h.client.request(method, route, json=payload, headers=h.headers)
        if identity == "owner":
            assert response.status_code == (
                409 if route.endswith("/retry") else 201 if method == "POST" else 200
            ), response.text
        elif identity == "anonymous":
            assert response.status_code in (401, 403), (route, response.text)
        else:
            assert response.status_code == 404, (route, response.text)


def test_wrong_story_chapter_is_rejected(harness):
    h = harness
    other = h.client.post(
        f"/api/v1/projects/{h.project['id']}/chapters", json={"name": "Other"}, headers=h.headers
    ).json()
    for chapter, story in ((other["id"], h.version_id), (h.chapter["id"], str(uuid4()))):
        assert (
            h.client.post(
                f"/api/v1/chapters/{chapter}/scripts",
                json={"story_version_id": story},
                headers=h.headers,
            ).status_code
            == 404
        )
