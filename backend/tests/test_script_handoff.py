"""Phase 7C contract, read-only and deterministic PostgreSQL snapshot gates."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_projects import register
from test_script_finalization import harness as harness  # noqa: F401
from test_script_revision import publish

from app import script_handoff
from app.config import get_settings
from app.main import app
from app.models import (
    Base,
    OCRResult,
    Page,
    Panel,
    ScriptEvidence,
    ScriptSegment,
    ScriptVersion,
    StoryVersion,
    User,
)


def prepare(h):
    route = publish(h)
    response = h.client.get(route)
    approved = h.client.patch(
        route + "/review",
        json={"status": "confirmed"},
        headers={**h.headers, "If-Match": response.headers["etag"]},
    )
    assert approved.status_code == 200
    script_id = UUID(route.rsplit("/", 1)[1])
    return script_id, h.route + f"/scripts/{script_id}/handoff"


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table.name: sorted(map(repr, connection.execute(select(table)).all()))
            for table in Base.metadata.tables.values()
        }


def persistent_state(h):
    root = Path(get_settings().local_storage_path)
    return (
        snapshot(h.engine),
        {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
        list(h.submitted),
    )


def source_fixture(h):
    """Three real, distinct OCR sources bound to the selected StoryVersion."""
    with Session(h.engine) as db:
        page = db.scalar(select(Page))
        sources = []
        for index, quote in enumerate(("Zulu", "Alpha", "Middle"), 1):
            panel = Panel(
                page_id=page.id, panel_index=index, reading_order=index,
                x=0, y=0, width=1, height=1, x_norm=0, y_norm=0,
                width_norm=1, height_norm=1, confidence=0.8,
            )
            db.add(panel)
            db.flush()
            ocr = OCRResult(panel_id=panel.id, text=quote, confidence=0.8)
            db.add(ocr)
            db.flush()
            sources.append({
                "page_id": str(page.id), "page_number": 1,
                "panel_id": str(panel.id), "ocr_result_id": str(ocr.id),
                "quote": quote, "reason": "Visible source",
            })
        story = db.get(StoryVersion, UUID(h.version_id))
        data = deepcopy(story.data)
        data["scenes"][0]["events"][0]["evidence"] = sources
        story.data = data
        db.commit()
    return sources


def forbid_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Handoff must not access a provider, queue, worker or storage")

    for target in (
        "app.script.get_job_queue", "app.jobs.get_job_queue", "app.jobs.process_job",
        "app.script.configured_script_provider", "app.script_service.process_script",
        "app.script_service.configured_script_provider",
        "app.story_providers._json_request", "app.storage.LocalStorageProvider.put",
        "app.storage.LocalStorageProvider.get", "app.storage.LocalStorageProvider.delete",
    ):
        monkeypatch.setattr(target, forbidden)


def test_contract_parity_and_no_writes(harness):
    h = harness
    script_id, route = prepare(h)
    before = snapshot(h.engine)
    response = h.client.get(route, headers={"If-None-Match": "*", "If-Match": "invalid"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    payload = response.json()
    assert set(payload) == {"schema", "dependency", "script"}
    assert payload["schema"] == "approved-script-handoff-v1"
    assert set(payload["dependency"]) == {
        "story_version_id",
        "script_version_id",
        "revision",
        "language",
        "profile_fingerprint",
        "eligible",
        "reasons",
    }
    assert set(payload["script"]) == {"title", "hook", "intro", "outro", "segments"}
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        assert payload["dependency"]["profile_fingerprint"] == row.profile_fingerprint
        assert payload["dependency"]["story_version_id"] == str(row.story_version_id)
        user = db.scalar(select(User))
        for actual, original in zip(
            payload["script"]["segments"], row.data["segments"], strict=True
        ):
            assert set(actual) == {
                "sequence", "segment_type", "narration_text", "dialogue_text",
                "source_dialogue", "speaker_ref", "scene_ref", "event_refs",
                "start_page", "end_page", "estimated_duration", "confidence",
                "status", "evidence",
            }
            assert 0 < actual["estimated_duration"] <= 3600
            assert actual["estimated_duration"] == original["estimated_duration"]
            assert actual["speaker_ref"] is None
            assert actual["dialogue_text"] is None
            for evidence in actual["evidence"]:
                assert set(evidence) == {
                    "scene_ref",
                    "event_ref",
                    "page_number",
                    "panel_id",
                    "ocr_result_id",
                    "quote",
                }
                assert evidence["page_number"] == 1
                assert evidence["panel_id"] is None
                assert evidence["ocr_result_id"] is None
                assert evidence["quote"] is None
        dto = h.client.portal.call(
            script_handoff.approved_script_handoff,
            app.state.db_session_factory,
            user,
            UUID(h.chapter["id"]),
            script_id,
        )
    assert json.loads(dto.serialize()) == payload
    assert snapshot(h.engine) == before


@pytest.mark.parametrize(
    "state,bound",
    [
        ("needs_review", False),
        ("rejected", False),
        ("confirmed", False),
        ("confirmed", True),
    ],
)
@pytest.mark.parametrize("language", ["en", "ar", "fr"])
@pytest.mark.parametrize("valid", [True, False])
def test_reason_combinations(harness, monkeypatch, state, bound, language, valid):
    h = harness
    script_id, route = prepare(h)
    reasons = []
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        row.status = state
        row.approved_revision = row.revision if bound else None
        row.profile = {**row.profile, "language": language}
        if not valid:
            data = deepcopy(row.data)
            data["segments"][0]["evidence"][0]["reason"] = ""
            row.data = data
            reasons.append("source_invalid")
        db.commit()
    if state == "needs_review":
        reasons.append("review_required")
    elif state == "rejected":
        reasons.append("rejected")
    elif not bound:
        reasons.append("approval_unbound")
    if language == "fr":
        reasons.append("unsupported_language")
    forbid_external_calls(monkeypatch)
    before = persistent_state(h)
    response = h.client.get(route)
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    if reasons:
        assert response.status_code == 409
        assert response.json() == {
            "success": False,
            "error": {
                "code": "DEPENDENCY_NOT_ELIGIBLE",
                "message": "Script is not eligible for downstream use",
                "reasons": sorted(reasons),
            },
        }
    else:
        assert response.status_code == 200
    assert persistent_state(h) == before


@pytest.mark.parametrize("boundary", ["approved_script_handoff", "validated_candidate"])
def test_unexpected_service_error_has_no_side_effects(harness, monkeypatch, boundary):
    h = harness
    _, route = prepare(h)
    forbid_external_calls(monkeypatch)

    async def broken(*args, **kwargs):
        raise RuntimeError("private SQL password /storage/source.png")

    monkeypatch.setattr(script_handoff, boundary, broken)
    before = persistent_state(h)
    response = h.client.get(route, headers={"If-None-Match": "*"})
    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"},
    }
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers
    assert persistent_state(h) == before


def test_errors_and_security(harness, monkeypatch):
    h = harness
    script_id, route = prepare(h)
    for target, expected in [
        (route.replace(str(script_id), str(uuid4())), 404),
        (route.replace(h.chapter["id"], str(uuid4())), 404),
        (route.replace(str(script_id), "not-a-uuid"), 422),
    ]:
        response = h.client.get(target)
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert "etag" not in response.headers
        assert "DEPENDENCY_NOT_ELIGIBLE" not in response.text

    async def broken(*args, **kwargs):
        raise RuntimeError("private-sql-credential-path")

    monkeypatch.setattr(script_handoff, "approved_script_handoff", broken)
    response = h.client.get(route)
    assert response.status_code == 500
    assert "private-sql-credential-path" not in response.text
    assert response.headers["cache-control"] == "no-store"
    h.client.cookies.clear()
    response = h.client.get(route)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
@pytest.mark.parametrize("mutation", ["metadata", "segments", "rejection", "page"])
def test_postgresql_snapshot(harness, monkeypatch, mutation):
    h = harness
    script_id, route = prepare(h)
    expected = h.client.get(route).json()
    loaded, committed = Event(), Event()
    original = script_handoff.validated_candidate

    async def paused(db, script, data, **kwargs):
        assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await db.scalar(text("SHOW transaction_read_only")) == "on"
        loaded.set()
        assert await asyncio.to_thread(committed.wait, 20)
        return await original(db, script, data, **kwargs)

    monkeypatch.setattr(script_handoff, "validated_candidate", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.client.get, route)
        try:
            assert loaded.wait(20)
            with Session(h.engine) as db:
                row = db.get(ScriptVersion, script_id)
                if mutation == "page":
                    db.scalar(select(Page)).page_number = 2
                elif mutation == "rejection":
                    row.status = "rejected"
                    row.approved_revision = None
                else:
                    data = deepcopy(row.data)
                    if mutation == "metadata":
                        data["title"] = "Concurrent title"
                    else:
                        old_text = data["segments"][0]["narration_text"]
                        data["segments"][0]["narration_text"] = "Concurrent narration"
                        for key in ("hook", "intro", "outro"):
                            if data[key] == old_text:
                                data[key] = "Concurrent narration"
                    row.data = data
                    row.revision += 1
                    row.approved_revision = row.revision
                db.commit()
        finally:
            committed.set()
        response = future.result(20)
    assert response.status_code == 200
    assert response.json() == expected
    after = h.client.get(route)
    if mutation in {"page", "rejection"}:
        assert after.status_code == 409
        assert after.json() == {
            "success": False,
            "error": {
                "code": "DEPENDENCY_NOT_ELIGIBLE",
                "message": "Script is not eligible for downstream use",
                "reasons": ["source_invalid" if mutation == "page" else "rejected"],
            },
        }
    else:
        assert after.status_code == 200
        current = deepcopy(expected)
        current["dependency"]["revision"] += 1
        if mutation == "metadata":
            current["script"]["title"] = "Concurrent title"
        else:
            old_text = current["script"]["segments"][0]["narration_text"]
            current["script"]["segments"][0]["narration_text"] = "Concurrent narration"
            for key in ("hook", "intro", "outro"):
                if current["script"][key] == old_text:
                    current["script"][key] = "Concurrent narration"
        assert after.json() == current
    assert after.headers["cache-control"] == "no-store"
    assert "etag" not in after.headers


def test_authorization_matrix(harness):
    h = harness
    script_id, route = prepare(h)
    other = h.client.post(
        f"/api/v1/projects/{h.project['id']}/chapters",
        json={"name": "Mismatch"}, headers=h.headers,
    )
    assert other.status_code == 201
    targets = [
        (route, 200),
        (route.replace(h.chapter["id"], other.json()["id"]), 404),
        (route.replace(h.chapter["id"], str(uuid4())), 404),
        (route.replace(str(script_id), str(uuid4())), 404),
    ]
    for target, code in targets:
        response = h.client.get(target)
        assert response.status_code == code
        assert response.headers["cache-control"] == "no-store"
        assert "etag" not in response.headers
        if code == 404:
            assert set(response.json()) == {"success", "error"}
            assert response.json()["success"] is False
            assert set(response.json()["error"]) == {"code", "message"}
            assert response.json()["error"]["code"] == "NOT_FOUND"
            assert response.json()["error"]["message"] in {
                "Script not found", "Chapter not found",
            }
            assert not any(key in response.text for key in (
                "eligible", "reasons", "revision", str(script_id), h.version_id,
            ))
    h.client.cookies.clear()
    for target, _ in targets:
        response = h.client.get(target)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    register(h.client, "handoff-outsider@example.com")
    responses = [h.client.get(target) for target, _ in targets]
    assert all(r.status_code == 404 for r in responses)
    assert all(r.headers["cache-control"] == "no-store" for r in responses)
    assert len({r.content for r in responses}) == 1
    assert all("etag" not in r.headers for r in responses)
    assert all(
        r.json() == {
            "success": False, "error": {"code": "NOT_FOUND", "message": "Chapter not found"},
        }
        for r in responses
    )


@pytest.mark.parametrize("reasons", [[], ["source_invalid", "rejected"]])
def test_http_adapter_delegates_only(harness, monkeypatch, reasons):
    h = harness
    script_id, route = prepare(h)
    calls = []
    content = b'{"canonical":"already materialized"}'

    async def service(factory, user, chapter_id, identity):
        calls.append((factory, user.id, chapter_id, identity))
        if reasons:
            raise script_handoff.DependencyNotEligible(reasons)
        return script_handoff.ApprovedScriptHandoff(content)

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP adapter must not evaluate identity or eligibility")

    monkeypatch.setattr(script_handoff, "approved_script_handoff", service)
    monkeypatch.setattr(script_handoff, "dependency", forbidden)
    monkeypatch.setattr(script_handoff, "validated_candidate", forbidden)
    response = h.client.get(route)
    assert len(calls) == 1
    assert calls[0][0] is app.state.db_session_factory
    assert calls[0][2:] == (UUID(h.chapter["id"]), script_id)
    assert response.headers["cache-control"] == "no-store"
    if reasons:
        assert response.status_code == 409
        assert response.json() == script_handoff.DependencyNotEligible(reasons).representation()
    else:
        assert response.status_code == 200
        assert response.content == content


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
@pytest.mark.parametrize("mutation", ["ocr_content", "ocr_delete", "panel_delete"])
def test_postgresql_ocr_mutation_and_page_delete_race(harness, monkeypatch, mutation):
    h = harness
    with Session(h.engine) as db:
        page = db.scalar(select(Page))
        panel = Panel(
            page_id=page.id, panel_index=1, reading_order=1,
            x=0, y=0, width=1, height=1, x_norm=0, y_norm=0,
            width_norm=1, height_norm=1, confidence=0.8,
        )
        db.add(panel)
        db.flush()
        ocr = OCRResult(panel_id=panel.id, text="source quote", confidence=0.8)
        db.add(ocr)
        db.flush()
        story = db.get(StoryVersion, UUID(h.version_id))
        data = deepcopy(story.data)
        for evidence in (data["scenes"][0]["evidence"][0],
                         data["scenes"][0]["events"][0]["evidence"][0]):
            evidence.update(panel_id=str(panel.id), ocr_result_id=str(ocr.id), quote="source quote")
        story.data = data
        db.commit()
    script_id, route = prepare(h)
    expected = h.client.get(route).json()
    entered, released = Event(), Event()
    original = script_handoff.validated_candidate

    async def paused(db, script, data, **kwargs):
        assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await db.scalar(text("SHOW transaction_read_only")) == "on"
        entered.set()
        assert await asyncio.to_thread(released.wait, 20)
        return await original(db, script, data, **kwargs)

    monkeypatch.setattr(script_handoff, "validated_candidate", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.client.get, route)
        assert entered.wait(20)
        with Session(h.engine) as db:
            ocr = db.scalar(select(OCRResult))
            if mutation == "ocr_content":
                ocr.text = "concurrent OCR mutation"
            else:
                # OCR deletion is allowed and clears normalized evidence's OCR FK.
                # Delete it first before a panel deletion; never disable constraints.
                panel_id = ocr.panel_id
                db.delete(ocr)
                db.flush()
                if mutation == "panel_delete":
                    db.delete(db.get(Panel, panel_id))
            db.commit()
        released.set()
        response = future.result(20)
    assert response.status_code == 200
    assert response.json() == expected

    entered.clear()
    released.clear()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.client.get, route)
        assert entered.wait(20)
        with Session(h.engine) as db:
            page = db.scalar(select(Page))
            db.delete(page)
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()
        released.set()
        response = future.result(20)
    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "error": {
            "code": "DEPENDENCY_NOT_ELIGIBLE",
            "message": "Script is not eligible for downstream use",
            "reasons": ["source_invalid"],
        },
    }


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
def test_postgresql_preloaded_session_cannot_mix_snapshot(harness, monkeypatch):
    h = harness
    script_id, route = prepare(h)
    expected = h.client.get(route).json()
    entered, released = Event(), Event()
    original = script_handoff.validated_candidate

    async def paused(db, script, data, **kwargs):
        # The service-created session is fresh; this query proves its snapshot state.
        assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await db.scalar(text("SHOW transaction_read_only")) == "on"
        entered.set()
        assert await asyncio.to_thread(released.wait, 20)
        return await original(db, script, data, **kwargs)

    monkeypatch.setattr(script_handoff, "validated_candidate", paused)
    with Session(h.engine) as preloaded:
        preloaded_script = preloaded.get(ScriptVersion, script_id)
        user = preloaded.scalar(select(User))
        assert preloaded_script is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(h.client.get, route)
            assert entered.wait(20)
            with Session(h.engine) as writer:
                row = writer.get(ScriptVersion, script_id)
                data = deepcopy(row.data)
                data["title"] = "committed after snapshot"
                row.data = data
                row.revision += 1
                row.approved_revision = row.revision
                writer.commit()
            released.set()
            response = future.result(20)
        assert preloaded_script.data["title"] == expected["script"]["title"]
        dto = h.client.portal.call(
            script_handoff.approved_script_handoff,
            app.state.db_session_factory, user, UUID(h.chapter["id"]), script_id,
        )
        current = deepcopy(expected)
        current["script"]["title"] = "committed after snapshot"
        current["dependency"]["revision"] += 1
        assert json.loads(dto.serialize()) == current
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
def test_postgresql_readers_on_opposite_sides_of_commit(harness, monkeypatch):
    h = harness
    sources = source_fixture(h)
    script_id, route = prepare(h)
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        data = deepcopy(row.data)
        template = data["segments"][0]
        data["segments"] = []
        for index, source in enumerate(sources, 1):
            segment = deepcopy(template)
            segment.update(
                temp_id=f"segment-{index}", sequence=index,
                narration_text=f"Distinct narration {index}",
                evidence=[{**source, "scene_ref": "scene", "event_ref": "event"}],
            )
            data["segments"].append(segment)
        for key in ("hook", "intro", "outro"):
            data[key] = ""
        row.data = data
        db.commit()
    response = h.client.get(route)
    assert response.status_code == 200, response.text
    before = response.json()
    assert len(before["script"]["segments"]) == 3
    assert [s["sequence"] for s in before["script"]["segments"]] == [1, 2, 3]
    assert [s["evidence"][0]["quote"] for s in before["script"]["segments"]] == [
        "Zulu", "Alpha", "Middle",
    ]
    first_reads = [Event() for _ in range(3)]
    second_reads = [Event() for _ in range(3)]
    allow_first = Event()
    original = script_handoff.validated_candidate
    calls = 0
    sessions = []

    async def coordinated(db, script, data, **kwargs):
        nonlocal calls
        index = calls
        calls += 1
        sessions.append(db)
        assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await db.scalar(text("SHOW transaction_read_only")) == "on"
        if index < 3:
            first_reads[index].set()
            assert await asyncio.to_thread(allow_first.wait, 20)
        else:
            second_reads[index - 3].set()
        return await original(db, script, data, **kwargs)

    monkeypatch.setattr(script_handoff, "validated_candidate", coordinated)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = [pool.submit(h.client.get, route) for _ in range(3)]
        try:
            assert all(ready.wait(20) for ready in first_reads)
            with Session(h.engine) as db:
                row = db.get(ScriptVersion, script_id)
                data = deepcopy(row.data)
                data["title"] = "after committed mutation"
                data["segments"].reverse()
                for index, segment in enumerate(data["segments"], 1):
                    segment["sequence"] = index
                row.data = data
                row.revision += 1
                row.approved_revision = row.revision
                db.commit()
            second = [pool.submit(h.client.get, route) for _ in range(3)]
            assert all(ready.wait(20) for ready in second_reads)
            second_responses = [future.result(20) for future in second]
        finally:
            allow_first.set()
        first_responses = [future.result(20) for future in first]
    assert len({id(db) for db in sessions}) == 6
    after = deepcopy(before)
    after["dependency"]["revision"] += 1
    after["script"]["title"] = "after committed mutation"
    after["script"]["segments"].reverse()
    for index, segment in enumerate(after["script"]["segments"], 1):
        segment["sequence"] = index
    for responses, expected in ((first_responses, before), (second_responses, after)):
        for response in responses:
            assert response.status_code == 200
            assert response.json() == expected
            assert response.json()["dependency"]["eligible"] is True
            assert response.json()["dependency"]["reasons"] == []


def test_handoff_never_changes_jobs_audits_or_storage(harness, monkeypatch):
    h = harness
    script_id, route = prepare(h)
    def forbidden(*args, **kwargs):
        pytest.fail("Handoff must not access a provider, queue, worker or storage")

    for target in (
        "app.script.get_job_queue", "app.jobs.get_job_queue", "app.jobs.process_job",
        "app.script.configured_script_provider", "app.script_service.process_script",
        "app.script_service.configured_script_provider",
        "app.story_providers._json_request", "app.storage.LocalStorageProvider.put",
        "app.storage.LocalStorageProvider.get", "app.storage.LocalStorageProvider.delete",
    ):
        monkeypatch.setattr(target, forbidden)
    root = Path(get_settings().local_storage_path)

    def persistent_state():
        return (
            snapshot(h.engine),
            {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
            list(h.submitted),
        )

    for target, code in (
        (route, 200), (route, 200),
        (route.replace(str(script_id), str(uuid4())), 404),
        (route.replace(str(script_id), "invalid"), 422),
    ):
        before = persistent_state()
        assert h.client.get(target).status_code == code
        assert persistent_state() == before
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        row.approved_revision = None
        db.commit()
    before = persistent_state()
    assert h.client.get(route).status_code == 409
    assert persistent_state() == before
    h.client.cookies.clear()
    before = persistent_state()
    assert h.client.get(route).status_code == 401
    assert persistent_state() == before
    register(h.client, "readonly-outsider@example.com")
    before = persistent_state()
    assert h.client.get(route).status_code == 404
    assert persistent_state() == before


def test_serialization_after_closed_transaction_has_no_reads(harness):
    h = harness
    script_id, route = prepare(h)
    expected = h.client.get(route).content
    factory = app.state.db_session_factory
    with Session(h.engine) as db:
        user = db.scalar(select(User))
        dto = h.client.portal.call(
            script_handoff.approved_script_handoff,
            factory, user, UUID(h.chapter["id"]), script_id,
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Detached DTO serialization attempted SQL")

    engine = factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", forbidden)
    try:
        assert dto.serialize() == expected
        assert dto.serialize() == expected
    finally:
        event.remove(engine, "before_cursor_execute", forbidden)


@pytest.mark.parametrize(
    "duration,code", [(0, 409), (-1, 409), (3600.1, 409), (0.001, 200), (3600, 200)]
)
def test_duration_bounds(harness, duration, code):
    h = harness
    script_id, route = prepare(h)
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        data = deepcopy(row.data)
        data["segments"][0]["estimated_duration"] = duration
        row.data = data
        db.commit()
    before = snapshot(h.engine)
    response = h.client.get(route)
    assert response.status_code == code
    if code == 200:
        assert response.json()["script"]["segments"][0]["estimated_duration"] == duration
    else:
        error = script_handoff.DependencyNotEligible(["source_invalid"])
        assert response.json() == error.representation()
    assert snapshot(h.engine) == before


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
def test_postgresql_segment_order_during_projection(harness, monkeypatch):
    h = harness
    script_id, route = prepare(h)
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        data = deepcopy(row.data)
        second = deepcopy(data["segments"][0])
        second.update(temp_id="second", sequence=2, narration_text="Second narration")
        data["segments"].append(second)
        row.data = data
        db.commit()
    before = h.client.get(route)
    assert before.status_code == 200
    expected = before.json()
    loaded, committed = Event(), Event()
    original = script_handoff.validated_candidate

    async def paused(db, script, data, **kwargs):
        candidate = await original(db, script, data, **kwargs)
        loaded.set()
        assert await asyncio.to_thread(committed.wait, 20)
        return candidate

    monkeypatch.setattr(script_handoff, "validated_candidate", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.client.get, route)
        try:
            assert loaded.wait(20)
            with Session(h.engine) as db:
                row = db.get(ScriptVersion, script_id)
                data = deepcopy(row.data)
                data["segments"].reverse()
                for index, segment in enumerate(data["segments"], 1):
                    segment["sequence"] = index
                row.data = data
                row.revision += 1
                row.approved_revision = row.revision
                db.commit()
        finally:
            committed.set()
        response = future.result(20)
    assert response.status_code == 200
    assert response.json() == expected
    after = deepcopy(expected)
    after["dependency"]["revision"] += 1
    after["script"]["segments"].reverse()
    for index, segment in enumerate(after["script"]["segments"], 1):
        segment["sequence"] = index
    assert h.client.get(route).json() == after


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
@pytest.mark.parametrize("access", ["cross_owner", "mismatch"])
def test_postgresql_unauthorized_during_mutation(harness, monkeypatch, access):
    h = harness
    script_id, route = prepare(h)
    if access == "cross_owner":
        h.client.cookies.clear()
        register(h.client, "concurrent-outsider@example.com")
    else:
        other = h.client.post(
            f"/api/v1/projects/{h.project['id']}/chapters",
            json={"name": "Other"}, headers=h.headers,
        )
        assert other.status_code == 201
        route = route.replace(h.chapter["id"], other.json()["id"])
    entered, committed = Event(), Event()
    original = script_handoff.owned_chapter

    async def paused(db, chapter_id, user):
        # Establish a real snapshot before releasing the independent writer.
        await db.scalar(select(User.id).where(User.id == user.id))
        entered.set()
        assert await asyncio.to_thread(committed.wait, 20)
        return await original(db, chapter_id, user)

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized request reached eligibility evaluation")

    monkeypatch.setattr(script_handoff, "owned_chapter", paused)
    monkeypatch.setattr(script_handoff, "validated_candidate", forbidden)
    monkeypatch.setattr(script_handoff, "dependency", forbidden)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(h.client.get, route)
        try:
            assert entered.wait(20)
            with Session(h.engine) as db:
                row = db.get(ScriptVersion, script_id)
                row.status = "rejected"
                row.approved_revision = None
                db.commit()
            persisted = snapshot(h.engine)
        finally:
            committed.set()
        response = future.result(20)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "DEPENDENCY_NOT_ELIGIBLE" not in response.text
    assert snapshot(h.engine) == persisted


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
def test_postgresql_independent_evidence_order(harness):
    h = harness
    script_id, route = prepare(h)
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        data = deepcopy(row.data)
        story = db.get(StoryVersion, row.story_version_id)
        story_data = deepcopy(story.data)
        page = db.scalar(select(Page))
        sources = []
        for index, quote in enumerate(("Zulu", "Alpha", "Middle"), 1):
            panel = Panel(
                page_id=page.id, panel_index=index, reading_order=index,
                x=0, y=0, width=1, height=1, x_norm=0, y_norm=0,
                width_norm=1, height_norm=1, confidence=0.8,
            )
            db.add(panel)
            db.flush()
            ocr = OCRResult(panel_id=panel.id, text=quote, confidence=0.8)
            db.add(ocr)
            db.flush()
            sources.append({
                "page_id": str(page.id), "page_number": 1,
                "panel_id": str(panel.id), "ocr_result_id": str(ocr.id),
                "quote": quote, "reason": "Visible source",
            })
        story_data["scenes"][0]["events"][0]["evidence"] = sources
        story.data = story_data
        segment = data["segments"][0]
        segment["evidence"] = [
            {**sources[i], "scene_ref": "scene", "event_ref": "event"}
            for i in (2, 0, 1)
        ]
        row.data = data
        expected = {
            "schema": "approved-script-handoff-v1",
            "dependency": {
                "story_version_id": str(row.story_version_id),
                "script_version_id": str(script_id), "revision": row.revision,
                "language": "en", "profile_fingerprint": row.profile_fingerprint,
                "eligible": True, "reasons": [],
            },
            "script": {key: deepcopy(data[key]) for key in ("title", "hook", "intro", "outro")},
        }
        projected = {key: value for key, value in segment.items() if key != "temp_id"}
        projected["evidence"] = [
            {
                "scene_ref": "scene", "event_ref": "event", "page_number": 1,
                "panel_id": sources[i]["panel_id"],
                "ocr_result_id": sources[i]["ocr_result_id"], "quote": sources[i]["quote"],
            }
            for i in (2, 0, 1)
        ]
        expected["script"]["segments"] = [projected]
        db.commit()
    contents = []
    for order in ((0, 1, 2), (1, 2, 0), (1, 0, 2)):
        # Reinsert normalized rows in deliberately different heap AND UUID order.
        # Neither is the validated JSON sequence (2, 0, 1) required by spec 6.2.
        with Session(h.engine) as db:
            segment_id = db.scalar(select(ScriptSegment.id).where(
                ScriptSegment.script_version_id == script_id,
            ))
            db.execute(delete(ScriptEvidence).where(
                ScriptEvidence.script_version_id == script_id,
            ))
            for rank, index in enumerate(order, 1):
                source = sources[index]
                db.add(ScriptEvidence(
                    id=UUID(int=rank), script_version_id=script_id, segment_id=segment_id,
                    chapter_id=UUID(h.chapter["id"]), scene_ref="scene", event_ref="event",
                    page_id=UUID(source["page_id"]), page_number=1,
                    panel_id=UUID(source["panel_id"]),
                    ocr_result_id=UUID(source["ocr_result_id"]),
                    quote=source["quote"], reason=source["reason"],
                ))
                db.flush()
            db.commit()
            for ordering in (ScriptEvidence.id, text("ctid")):
                actual = list(db.scalars(select(ScriptEvidence.quote).order_by(ordering)))
                assert actual == [sources[i]["quote"] for i in order]
                assert actual != ["Middle", "Zulu", "Alpha"]
        before = persistent_state(h)
        response = h.client.get(route)
        assert response.status_code == 200, response.text
        assert response.json() == expected
        contents.append(response.content)
        assert [e["quote"] for e in response.json()["script"]["segments"][0]["evidence"]] == [
            "Middle", "Zulu", "Alpha",
        ]
        assert persistent_state(h) == before
    assert len(set(contents)) == 1


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="requires disposable real PostgreSQL",
)
def test_postgresql_active_async_caller_identity_map_is_not_reused(harness, monkeypatch):
    h = harness
    source_fixture(h)
    script_id, route = prepare(h)
    expected = h.client.get(route).json()
    factory = app.state.db_session_factory
    original = script_handoff.validated_candidate

    async def scenario():
        async with factory() as caller:
            stale = await caller.get(ScriptVersion, script_id)
            story = await caller.get(StoryVersion, stale.story_version_id)
            pages = list((await caller.scalars(select(Page))).all())
            panels = list((await caller.scalars(select(Panel))).all())
            ocr_results = list((await caller.scalars(select(OCRResult))).all())
            user = await caller.scalar(select(User))
            assert caller.in_transaction()
            with Session(h.engine) as writer:
                row = writer.get(ScriptVersion, script_id)
                row.data = {**row.data, "title": "fresh committed title"}
                row.revision += 1
                row.approved_revision = row.revision
                writer.get(OCRResult, ocr_results[0].id).text = "changed source"
                writer.commit()

            async def checked(db, script, data, **kwargs):
                assert db is not caller
                assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                assert await db.scalar(text("SHOW transaction_read_only")) == "on"
                assert script is not stale
                assert await db.get(StoryVersion, script.story_version_id) is not story
                assert await db.get(Page, pages[0].id) is not pages[0]
                assert await db.get(Panel, panels[0].id) is not panels[0]
                assert await db.get(OCRResult, ocr_results[0].id) is not ocr_results[0]
                return await original(db, script, data, **kwargs)

            monkeypatch.setattr(script_handoff, "validated_candidate", checked)
            for _ in range(2):
                with pytest.raises(script_handoff.DependencyNotEligible) as error:
                    await script_handoff.approved_script_handoff(
                        factory, user, UUID(h.chapter["id"]), script_id,
                    )
                assert error.value.reasons == ("source_invalid",)
            assert ocr_results[0].text == "Zulu"
            assert stale.data["title"] == expected["script"]["title"]
            assert stale.revision == expected["dependency"]["revision"]
            assert caller.in_transaction()

    h.client.portal.call(scenario)
