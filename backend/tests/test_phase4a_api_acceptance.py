"""A11 route acceptance matrix (all 15 registered Story routes).

Every matrix row runs success/replay, body handling, auth/ownership, invalid and
missing IDs, response shape, secret checks and failure-state comparisons below.
GET/DELETE have no body contract: malformed/missing/null bodies are ignored.
PATCH fields are optional; merge/split fields are required; analyze defaults {}.
Empty collections/status/summary are tested separately; absent entity mutations
are 404 (there is no create-entity route). Global IDs resolve their actual owner,
not a caller's currently selected project. Merge/split references additionally
must share the source chapter. Review transitions are PATCH, reanalysis is POST
analyze, and audit CRUD/relationship mutations are not exposed.
Version publication/protected edits also run in the existing lifecycle and
review-state suites; this file tests dispatch/version-read contracts directly.
"""

import json
from uuid import UUID, uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_phase4a_route_inventory import STORY_ROUTES
from test_projects import register
from test_story_integrity import seed

from app import story
from app.main import app
from app.models import Chapter, Character, Scene, StoryAnalysisJob, StoryAudit, StoryVersion
from app.story_audit import graph_snapshot

MATRIX = sorted(STORY_ROUTES)
SECRET = "a11-private-api-key-canary"


@pytest.fixture
def world(client, database, monkeypatch):
    route, chapter, character, target, scene, event = seed(client, database)
    submitted = []

    class Queue:
        def submit(self, identifier):
            submitted.append(identifier)

    monkeypatch.setattr(story, "get_job_queue", Queue)
    return dict(
        chapter_id=chapter,
        character_id=character,
        target_id=target,
        scene_id=scene,
        event_id=event,
        route=route,
        submitted=submitted,
    )


def body(method, path, ids):
    if method == "PATCH":
        return {"status": "confirmed"}
    if path.endswith("/merge"):
        return {"target_character_id": str(ids["target_id"])}
    if path.endswith("/split"):
        return {
            "retained_name": "John",
            "new_name": "Other",
            "reason": "distinct",
            "scene_ids": [str(ids["scene_id"])],
            "event_ids": [str(ids["event_id"])],
        }
    return {}


def send(client, method, path, ids, **kwargs):
    if "headers" not in kwargs:
        kwargs["headers"] = csrf(client)
    if "content" not in kwargs:
        kwargs.setdefault("json", body(method, path, ids))
    return client.request(method, path.format(**ids), **kwargs)


def snapshot(database):
    # Include jobs, versions and audits as well as every graph; exclude auth sessions.
    with Session(database[0]) as db:
        result = {str(c): graph_snapshot(db, c) for c in db.scalars(select(Chapter.id))}
        for model in (StoryAnalysisJob, StoryVersion, StoryAudit):
            result[model.__tablename__] = sorted(
                (jsonable_encoder(story.output(row)) for row in db.scalars(select(model))),
                key=lambda row: row["id"],
            )
        return result


def check(response, expected, client):
    assert response.status_code == expected, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    for secret in (SECRET, *client.cookies.values()):
        assert secret not in response.text
    for key in ("api_key", "password_hash", "token_hash", "csrf_hash", "s3_secret_key"):
        assert f'"{key}"' not in response.text.lower()
    data = response.json()
    if expected >= 400:
        assert set(data) == {"success", "error"}
        assert data["success"] is False
        assert set(data["error"]) == {"code", "message"}
        assert all(isinstance(value, str) for value in data["error"].values())
    return data


def test_registered_inventory():
    actual = {(m, r.path) for r in story.router.routes for m in r.methods}
    registered = {
        (m.upper(), path) for path, operations in app.openapi()["paths"].items() for m in operations
    }
    assert actual == set(MATRIX)
    assert actual <= registered
    assert len(MATRIX) == 15


@pytest.mark.parametrize("method,path", MATRIX)
def test_route_success_and_replay(client, database, world, method, path):
    first = check(send(client, method, path, world), 200, client)
    if method == "DELETE":
        assert first == {"success": True}
    elif path.endswith("/split"):
        assert set(first) == {"retained", "created"}
        assert first["retained"]["id"] == str(world["character_id"])
        assert first["created"]["chapter_id"] == str(world["chapter_id"])
    elif isinstance(first, list):
        for row in first:
            UUID(row["id"])
            if "chapter_id" in row:
                assert row["chapter_id"] == str(world["chapter_id"])
            if "scene_id" in row:
                assert row["scene_id"] == str(world["scene_id"])
    elif "id" in first:
        UUID(first["id"])
        assert isinstance(first["status"], str)
    else:
        assert first in ({"status": "not_started"}, {"status": "pending"})
    after = snapshot(database)
    assert check(send(client, method, path, world), 200, client) == first
    assert snapshot(database) == after


@pytest.mark.parametrize("method,path", MATRIX)
def test_route_security_and_missing_ids(client, database, world, method, path):
    before = snapshot(database)
    for value, status in (("invalid", 422), (str(uuid4()), 404)):
        ids = {k: value if k.endswith("_id") and k != "target_id" else v for k, v in world.items()}
        # Keep body references valid while varying only the path resource.
        check(send(client, method, path, ids, json=body(method, path, world)), status, client)
        assert snapshot(database) == before
    if method != "GET":
        check(send(client, method, path, world, headers={}), 403, client)
        assert snapshot(database) == before
    check(
        send(
            client,
            method,
            path,
            world,
            headers={**csrf(client), "origin": "https://attacker.invalid"},
        ),
        200 if method == "GET" else 403,
        client,
    )
    client.post("/api/v1/auth/logout", headers=csrf(client))
    register(client, "a11-other@example.com")
    denied = check(send(client, method, path, world), 404, client)
    assert not any(str(world[k]) in json.dumps(denied) for k in world if k.endswith("_id"))
    assert snapshot(database) == before
    client.post("/api/v1/auth/logout", headers=csrf(client))
    check(send(client, method, path, world, headers={}), 401 if method == "GET" else 403, client)
    assert snapshot(database) == before


@pytest.mark.parametrize("method,path", MATRIX)
def test_route_body_contract(client, database, world, method, path):
    consumes = method in {"POST", "PATCH"}
    before = snapshot(database)
    for content in ('{"api_key":"' + SECRET + '",', "[]"):
        check(
            send(
                client,
                method,
                path,
                world,
                content=content,
                headers={**csrf(client), "content-type": "application/json"},
            ),
            422 if consumes else 200,
            client,
        )
        if consumes:
            assert snapshot(database) == before
    required = method == "PATCH" or path.endswith(("/merge", "/split"))
    for content in ("", "null"):
        check(
            send(
                client,
                method,
                path,
                world,
                content=content,
                headers={**csrf(client), "content-type": "application/json"},
            ),
            422 if required else 200,
            client,
        )
    if consumes:
        check(send(client, method, path, world, json={"api_key": SECRET}), 422, client)
    if path.endswith(("/merge", "/split")):
        valid = body(method, path, world)
        fields = ["target_character_id"] if path.endswith("/merge") else list(valid)
        for field in fields:
            check(
                send(
                    client, method, path, world, json={k: v for k, v in valid.items() if k != field}
                ),
                422,
                client,
            )
            check(send(client, method, path, world, json={**valid, field: None}), 422, client)
        assert snapshot(database) == before
    elif method == "PATCH":
        check(send(client, method, path, world, json={"status": None}), 422, client)
        assert snapshot(database) == before
        check(send(client, method, path, world, json={}), 200, client)
    elif path.endswith("/analyze"):
        check(send(client, method, path, world, json={"force": None}), 422, client)


@pytest.mark.parametrize(
    "kind,fields",
    [
        ("characters", ["name", "display_name", "importance"]),
        ("scenes", ["title", "summary", "start_page", "end_page", "importance"]),
        ("events", ["description", "event_type", "importance"]),
    ],
)
def test_prohibited_null_patch_fields(client, database, world, kind, fields):
    key = {"characters": "character_id", "scenes": "scene_id", "events": "event_id"}[kind]
    before = snapshot(database)
    for field in fields:
        check(
            client.patch(f"/api/v1/{kind}/{world[key]}", json={field: None}, headers=csrf(client)),
            422,
            client,
        )
        assert snapshot(database) == before


@pytest.mark.parametrize("method,path", MATRIX)
def test_same_owner_global_ids(client, database, world, method, path):
    # Moving the whole graph to a second owned project's chapter must not revoke
    # access through its global entity IDs or mix in the old chapter's state.
    project = client.post("/api/v1/projects", json={"name": "Second"}, headers=csrf(client)).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Second"}, headers=csrf(client)
    ).json()
    old = world["chapter_id"]
    with Session(database[0]) as db:
        for model in (Character, Scene):
            for row in db.scalars(select(model).where(model.chapter_id == old)):
                row.chapter_id = UUID(chapter["id"])
        db.commit()
    world["chapter_id"] = UUID(chapter["id"])
    check(send(client, method, path, world), 200, client)
    assert client.get(f"/api/v1/chapters/{old}/characters").json() == []
    assert client.get(f"/api/v1/chapters/{old}/scenes").json() == []


@pytest.mark.parametrize(
    "method,path", [row for row in MATRIX if row[0] != "GET" and not row[1].endswith("/analyze")]
)
def test_mutation_transaction_rollback(client, database, world, monkeypatch, method, path):
    before = snapshot(database)

    def fail(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(story, "complete_audit", fail)
    check(send(client, method, path, world), 500, client)
    assert snapshot(database) == before


def test_empty_chapter_and_empty_scene(client, database, world):
    project = client.get("/api/v1/projects").json()[0]
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Empty"}, headers=csrf(client)
    ).json()
    root = f"/api/v1/chapters/{chapter['id']}"
    for suffix, expected in (
        ("characters", []),
        ("scenes", []),
        ("relationships", []),
        ("story/versions", []),
        ("story/status", {"status": "not_started"}),
        ("story/summary", {"status": "pending"}),
    ):
        assert check(client.get(f"{root}/{suffix}"), 200, client) == expected
    with Session(database[0]) as db:
        scene = Scene(
            chapter_id=UUID(chapter["id"]),
            scene_index=1,
            title="Empty",
            summary="",
            start_page=1,
            end_page=1,
        )
        db.add(scene)
        db.commit()
        identifier = scene.id
    assert check(client.get(f"/api/v1/scenes/{identifier}/events"), 200, client) == []
    check(client.get(f"/api/v1/scenes/{identifier}"), 200, client)
    check(client.post(root + "/story/analyze", headers=csrf(client)), 200, client)


@pytest.mark.parametrize("different_project", [False, True])
def test_cross_chapter_merge_split_references(client, database, world, different_project):
    project = client.get("/api/v1/projects").json()[0]
    if different_project:
        project = client.post(
            "/api/v1/projects", json={"name": "Other"}, headers=csrf(client)
        ).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Other"}, headers=csrf(client)
    ).json()
    with Session(database[0]) as db:
        character = Character(chapter_id=UUID(chapter["id"]), name="Other", display_name="Other")
        scene = Scene(
            chapter_id=UUID(chapter["id"]),
            scene_index=1,
            title="Other",
            summary="Other",
            start_page=1,
            end_page=1,
        )
        db.add_all([character, scene])
        db.commit()
        target, reference = character.id, scene.id
    before = snapshot(database)
    merge = f"/api/v1/characters/{world['character_id']}/merge"
    split = f"/api/v1/characters/{world['character_id']}/split"
    for identifier, expected in ((target, 409), (world["character_id"], 409), (uuid4(), 404)):
        check(
            client.post(merge, json={"target_character_id": str(identifier)}, headers=csrf(client)),
            expected,
            client,
        )
        assert snapshot(database) == before
    for identifier, expected in ((reference, 409), (uuid4(), 404)):
        payload = body("POST", split, world)
        payload["scene_ids"] = [str(identifier)]
        check(client.post(split, json=payload, headers=csrf(client)), expected, client)
        assert snapshot(database) == before


@pytest.mark.parametrize("suffix", ["characters", "scenes", "relationships", "events"])
def test_review_filter_contract(client, database, world, suffix):
    root = f"/api/v1/scenes/{world['scene_id']}" if suffix == "events" else world["route"]
    before = snapshot(database)
    for invalid in ("null", "approved", "", SECRET):
        check(client.get(f"{root}/{suffix}", params={"review_status": invalid}), 422, client)
    assert (
        check(client.get(f"{root}/{suffix}", params={"review_status": "confirmed"}), 200, client)
        == []
    )
    rows = check(
        client.get(f"{root}/{suffix}", params={"review_status": "needs_review"}), 200, client
    )
    assert all(row["status"] == "needs_review" for row in rows)
    assert snapshot(database) == before


def test_analysis_retry_versions_and_submission_failure(client, database, world, monkeypatch):
    path = world["route"] + "/story/analyze"
    first = check(client.post(path, headers=csrf(client)), 200, client)
    with Session(database[0]) as db:
        job = db.get(StoryAnalysisJob, UUID(first["id"]))
        job.status = "completed"
        db.add(
            StoryVersion(
                chapter_id=world["chapter_id"],
                fingerprint=job.source_fingerprint,
                data={},
                decisions=[],
            )
        )
        db.commit()
    same = check(client.post(path, json={"force": True}, headers=csrf(client)), 200, client)
    assert same["id"] == first["id"]
    versions = check(client.get(world["route"] + "/story/versions"), 200, client)
    assert len(versions) == 1
    with Session(database[0]) as db:
        db.get(StoryAnalysisJob, UUID(first["id"])).source_fingerprint = "changed"
        db.commit()
    second = check(client.post(path, headers=csrf(client)), 200, client)
    assert second["id"] != first["id"]
    assert second["status"] == "pending"
    assert len(world["submitted"]) == 2
    assert client.get(world["route"] + "/story/versions").json() == versions
    with Session(database[0]) as db:
        db.get(StoryAnalysisJob, UUID(second["id"])).status = "failed"
        db.commit()

    class BrokenQueue:
        def submit(self, identifier):
            raise RuntimeError(SECRET)

    monkeypatch.setattr(story, "get_job_queue", BrokenQueue)
    check(client.post(path, headers=csrf(client)), 503, client)
    status = check(client.get(world["route"] + "/story/status"), 200, client)
    assert status["status"] == "failed"
    assert status["error"] == "Could not submit story analysis job"
    assert client.get(world["route"] + "/story/versions").json() == versions
