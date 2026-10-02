"""Revision and explicit approval binding API checks."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, select
from sqlalchemy.orm import Session
from test_script_finalization import enqueue, status
from test_script_finalization import harness as harness  # noqa: F401

from app import script_service
from app.models import ScriptVersion, StoryAudit


def publish(h):
    job = enqueue(h)
    script_service.process_script(job["id"])
    return f"/api/v1/scripts/{status(h, job)['script_version_id']}"


@pytest.mark.parametrize(
    "header,code", [(None, 428), ("*", 400), ('W/"x"', 400), ('"bad"', 400), ('"a", "b"', 400)]
)
def test_required_precondition(harness, header, code):  # noqa: F811
    h = harness
    route = publish(h)
    before = h.client.get(route).json()
    headers = dict(h.headers)
    if header is not None:
        headers["If-Match"] = header
    response = h.client.patch(route + "/review", json={"status": "confirmed"}, headers=headers)
    assert response.status_code == code
    assert h.client.get(route).json() == before


def test_revision_binding_reset_and_noop(harness):  # noqa: F811
    h = harness
    route = publish(h)
    response = h.client.get(route)
    original = response.json()
    headers = {**h.headers, "If-Match": response.headers["etag"]}
    assert original["revision"] == 1
    assert original["approved_revision"] is None
    assert original["dependency"]["reasons"] == ["review_required"]
    confirmed = h.client.patch(route + "/review", json={"status": "confirmed"}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["revision"] == confirmed.json()["approved_revision"] == 2
    assert confirmed.json()["dependency"]["eligible"]
    stale = h.client.patch(route + "/review", json={"status": "confirmed"}, headers=headers)
    assert stale.status_code == 412
    headers["If-Match"] = confirmed.headers["etag"]
    noop = h.client.patch(route + "/review", json={"status": "confirmed"}, headers=headers)
    assert noop.json() == confirmed.json()
    edited = h.client.patch(route + "/metadata", json={"title": "New title"}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 3
    assert edited.json()["approved_revision"] is None
    assert edited.json()["status"] == "needs_review"
    assert all(s["status"] == "needs_review" for s in h.client.get(route).json()["segments"])


@pytest.mark.parametrize("language", ["en", "fr"])
def test_legacy_adoption(harness, language):  # noqa: F811
    h = harness
    route = publish(h)
    script_id = UUID(route.rsplit("/", 1)[1])
    with Session(h.engine) as db:
        row = db.get(ScriptVersion, script_id)
        data = deepcopy(row.data)
        data["status"] = "confirmed"
        for item in data["segments"]:
            item["status"] = "confirmed"
        row.data = data
        row.status = "confirmed"
        row.profile = {**row.profile, "language": language}
        db.commit()
    response = h.client.get(route)
    before = response.json()
    assert "approval_unbound" in before["dependency"]["reasons"]
    headers = {**h.headers, "If-Match": response.headers["etag"]}
    result = h.client.patch(route + "/review", json={"status": "confirmed"}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["approved_revision"] == result.json()["revision"] == 2
    assert result.json()["profile"] == before["profile"]
    assert result.json()["data"] == before["data"]
    assert result.json()["dependency"]["eligible"] == (language == "en")
    with Session(h.engine) as db:
        audit = db.scalar(select(StoryAudit).where(StoryAudit.action == "review"))
        assert audit.data["approval_adopted"] is True
        assert audit.data["approved_revision_before"] is None
        assert audit.data["approval_token"]["revision"] == 2


def test_populated_revision_migration(harness, database):  # noqa: F811
    h = harness
    publish(h)
    engine, config = database
    tables = [
        "script_versions",
        "script_segments",
        "script_evidence",
        "script_generation_jobs",
        "story_audits",
    ]

    def snapshot():
        result = {}
        with engine.connect() as connection:
            for name in tables:
                table = Table(name, MetaData(), autoload_with=connection)
                columns = [c for c in table.c if c.name not in {"revision", "approved_revision"}]
                result[name] = [
                    dict(row)
                    for row in connection.execute(select(*columns).order_by(table.c.id)).mappings()
                ]
        return result

    before = snapshot()
    command.downgrade(config, "0015_script_evidence_scope")
    assert snapshot() == before
    command.upgrade(config, "head")
    assert snapshot() == before
    with Session(engine) as db:
        script = db.scalar(select(ScriptVersion))
        assert script.revision == 1 and script.approved_revision is None
    command.downgrade(config, "0015_script_evidence_scope")
    assert snapshot() == before
    command.upgrade(config, "head")
    assert snapshot() == before


def test_competing_edit_and_approval(harness):  # noqa: F811
    h = harness
    route = publish(h)
    headers = {**h.headers, "If-Match": h.client.get(route).headers["etag"]}

    def write(kind):
        payload = {"status": "confirmed"} if kind == "review" else {"title": "Concurrent"}
        return h.client.patch(route + "/" + kind, json=payload, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["review", "metadata"])) == [200, 412]
    assert h.client.get(route).json()["revision"] == 2
