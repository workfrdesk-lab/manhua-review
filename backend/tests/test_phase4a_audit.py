"""A10 acceptance: supported Story mutations are reconstructable and scoped.

Relationship CRUD is intentionally unsupported; reconciliation is the only
relationship mutation path and is audited by the persistence acceptance below.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_projects import register
from test_story_integrity import seed

from app.models import Chapter, StoryAudit
from app.story_audit import graph_snapshot

SECRET_WORDS = ("api_key", "password", "session", "cookie", "secret", "token")


def audits(database, *, chapter=None, entity=None):
    with Session(database[0]) as db:
        query = select(StoryAudit)
        if chapter:
            query = query.where(StoryAudit.chapter_id == chapter)
        if entity:
            query = query.where(StoryAudit.entity_id == entity)
        return list(db.scalars(query))


def assert_reconstructable(audit):
    assert audit.data["before_snapshot"] != audit.data["after_snapshot"]
    assert audit.created_at is not None
    assert audit.data["project_id"]
    assert audit.data["chapter_id"] == str(audit.chapter_id)
    assert audit.data["before_snapshot"]["characters"] is not None
    assert audit.data["after_snapshot"]["characters"] is not None
    assert {
        "characters",
        "aliases",
        "relationships",
        "scenes",
        "events",
        "scene_participation",
        "event_participation",
        "understandings",
    } <= audit.data["before_snapshot"].keys()
    assert not any(word in str(audit.data).lower() for word in SECRET_WORDS)


def test_character_edit_scene_edit_and_event_edit_audits_are_reconstructable(client, database):
    route, chapter, character, _, scene, event = seed(client, database)
    cases = (
        (f"/api/v1/characters/{character}", {"name": "Reviewed John"}, character),
        (f"/api/v1/scenes/{scene}", {"title": "Reviewed Gate"}, scene),
        (f"/api/v1/events/{event}", {"description": "Reviewed Wait"}, event),
    )
    for path, payload, entity in cases:
        response = client.patch(path, json=payload, headers=csrf(client))
        assert response.status_code == 200
        rows = audits(database, entity=entity)
        assert len(rows) == 1
        audit = rows[0]
        assert audit.actor_id is not None
        assert audit.chapter_id == chapter
        assert audit.action == "user_edit"
        assert audit.entity_id == entity
        assert audit.data["actor_type"] == "user"
        assert_reconstructable(audit)
        assert payload[next(iter(payload))] in str(audit.data["after_snapshot"])


def test_event_delete_merge_and_split_capture_graph_snapshots(client, database):
    _, chapter, source, target, scene, event = seed(client, database)
    response = client.post(
        f"/api/v1/characters/{source}/merge",
        json={"target_character_id": str(target), "reason": "same person"},
        headers=csrf(client),
    )
    assert response.status_code == 200
    merge = audits(database, entity=source)[-1]
    assert_reconstructable(merge)
    assert merge.action == "merge"
    with Session(database[0]) as db:
        assert merge.data["project_id"] == str(db.get(Chapter, chapter).project_id)
    assert {str(source), str(target)} <= {merge.data["source"], merge.data["target"]}
    assert any(row["id"] == str(target) for row in merge.data["after_snapshot"]["characters"])
    assert any(
        row["character_id"] == str(target)
        for row in merge.data["after_snapshot"]["scene_participation"]
    )

    response = client.post(
        f"/api/v1/characters/{target}/split",
        json={
            "retained_name": "Jin",
            "new_name": "John",
            "scene_ids": [str(scene)],
            "event_ids": [str(event)],
            "reason": "distinct",
        },
        headers=csrf(client),
    )
    assert response.status_code == 200
    split = audits(database, entity=target)[-1]
    assert_reconstructable(split)
    assert split.action == "split"
    assert split.data["project_id"] == merge.data["project_id"]
    assert str(scene) in split.data["scene_ids"]
    assert str(event) in split.data["event_ids"]
    assert any(
        row["id"] == split.data["target"] for row in split.data["after_snapshot"]["characters"]
    )

    response = client.delete(f"/api/v1/events/{event}", headers=csrf(client))
    assert response.status_code == 200
    deletion = audits(database, entity=event)[-1]
    assert_reconstructable(deletion)
    assert deletion.action == "delete"
    assert deletion.data["removal"] == "soft_rejection"
    count = len(audits(database, entity=event))
    response = client.delete(f"/api/v1/events/{event}", headers=csrf(client))
    assert response.status_code == 200
    assert len(audits(database, entity=event)) == count


def test_reconciliation_audit_has_system_version_context_and_is_idempotent(client, database):
    from app.models import StoryVersion
    from app.story_schemas import CharacterData, StoryStructuredOutput
    from app.story_service import persist

    _, chapter, *_ = seed(client, database)
    value = StoryStructuredOutput(
        characters=[
            CharacterData(
                temp_id="new",
                name="New arrival",
                importance=0.5,
                confidence=0.8,
                evidence=[{"page_number": 1, "reason": "test source"}],
            )
        ]
    )
    with Session(database[0]) as db:
        version = StoryVersion(
            chapter_id=chapter,
            fingerprint="a" * 64,
            data=value.model_dump(mode="json"),
            decisions=[],
        )
        db.add(version)
        db.flush()
        persist(db, chapter, value, version.id)
        db.commit()
        rows = list(db.scalars(select(StoryAudit)))
        assert rows
        for row in rows:
            assert row.actor_id is None
            assert row.data["actor_type"] == "system"
            assert row.version_id == version.id
            assert row.chapter_id == chapter
            assert_reconstructable(row)
        identifiers = {row.id for row in rows}
        persist(db, chapter, value, version.id)
        db.commit()
        assert set(db.scalars(select(StoryAudit.id))) == identifiers


def test_split_persists_context_and_final_snapshot(client, database):
    from app.story_audit import graph_snapshot

    _, chapter, source, _, scene, event = seed(client, database)
    with Session(database[0]) as db:
        before = graph_snapshot(db, chapter)
    response = client.post(
        f"/api/v1/characters/{source}/split",
        json={
            "retained_name": "John",
            "new_name": "Other",
            "scene_ids": [str(scene)],
            "event_ids": [str(event)],
            "reason": "distinct",
        },
        headers=csrf(client),
    )
    assert response.status_code == 200
    rows = audits(database, entity=source)
    assert len(rows) == 1
    audit = rows[0]
    with Session(database[0]) as db:
        assert audit.data["before_snapshot"] == before
        assert audit.data["after_snapshot"] == graph_snapshot(db, chapter)
        assert audit.data["project_id"] == str(db.get(Chapter, chapter).project_id)
    assert audit.actor_id is not None
    assert audit.action == "split"
    assert audit.entity_type == "character"
    assert_reconstructable(audit)


@pytest.mark.parametrize("with_participation", [False, True])
def test_split_repeated_delivery_does_not_duplicate_audits(client, database, with_participation):
    _, chapter, source, _, scene, event = seed(client, database)
    payload = {
        "retained_name": "John",
        "new_name": "Other",
        "scene_ids": [str(scene)] if with_participation else [],
        "event_ids": [str(event)] if with_participation else [],
        "reason": "distinct",
    }
    path = f"/api/v1/characters/{source}/split"
    first = client.post(path, json=payload, headers=csrf(client))
    assert first.status_code == 200
    with Session(database[0]) as db:
        after = graph_snapshot(db, chapter)
    identifiers = {row.id for row in audits(database)}
    response = client.post(path, json=payload, headers=csrf(client))
    assert response.status_code == 200
    assert response.json() == first.json()
    assert {row.id for row in audits(database)} == identifiers
    with Session(database[0]) as db:
        assert graph_snapshot(db, chapter) == after


@pytest.mark.parametrize("with_participation", [False, True])
def test_split_concurrent_duplicate_delivery(client, database, monkeypatch, with_participation):
    from app import story

    _, chapter, source, _, scene, event = seed(client, database)
    payload = {
        "retained_name": "John",
        "new_name": "Other",
        "scene_ids": [str(scene)] if with_participation else [],
        "event_ids": [str(event)] if with_participation else [],
        "reason": "distinct",
    }
    barrier = Barrier(2)
    original = story.lock_story

    async def synchronized_lock(db, chapter_id):
        import asyncio

        await asyncio.to_thread(barrier.wait, 10)
        await original(db, chapter_id)

    headers = csrf(client)
    with monkeypatch.context() as patch:
        patch.setattr(story, "lock_story", synchronized_lock)
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(
                    client.post,
                    f"/api/v1/characters/{source}/split",
                    json=payload,
                    headers=headers,
                )
                for _ in range(2)
            ]
            results = [future.result(30) for future in futures]
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json() == results[1].json()
    rows = audits(database, entity=source)
    assert len(rows) == 1
    with Session(database[0]) as db:
        assert graph_snapshot(db, chapter) == rows[0].data["after_snapshot"]
        assert len(graph_snapshot(db, chapter)["characters"]) == 3


@pytest.mark.parametrize(
    "changed_field", ["retained_name", "new_name", "reason", "scene_ids", "event_ids"]
)
def test_different_split_request_is_processed(client, database, changed_field):
    _, _, source, _, scene, event = seed(client, database)
    payload = {
        "retained_name": "John",
        "new_name": "Other",
        "scene_ids": [],
        "event_ids": [],
        "reason": "distinct",
    }
    path = f"/api/v1/characters/{source}/split"
    first = client.post(path, json=payload, headers=csrf(client))
    assert first.status_code == 200
    payload[changed_field] = (
        [str(scene)]
        if changed_field == "scene_ids"
        else [str(event)]
        if changed_field == "event_ids"
        else "Different"
    )
    second = client.post(path, json=payload, headers=csrf(client))
    assert second.status_code == 200
    assert second.json()["created"]["id"] != first.json()["created"]["id"]
    assert len(audits(database, entity=source)) == 2


def test_merge_split_retry_rejects_cross_user_access(client, database):
    _, chapter, source, target, _, _ = seed(client, database)
    payload = {
        "retained_name": "John",
        "new_name": "Other",
        "scene_ids": [],
        "event_ids": [],
        "reason": "distinct",
    }
    split_path = f"/api/v1/characters/{source}/split"
    assert client.post(split_path, json=payload, headers=csrf(client)).status_code == 200
    identifiers = {row.id for row in audits(database)}
    with Session(database[0]) as db:
        before = graph_snapshot(db, chapter)
    client.post("/api/v1/auth/logout", headers=csrf(client))
    register(client, "audit-other@example.com")
    assert client.post(split_path, json=payload, headers=csrf(client)).status_code == 404
    assert (
        client.post(
            f"/api/v1/characters/{source}/merge",
            json={"target_character_id": str(target)},
            headers=csrf(client),
        ).status_code
        == 404
    )
    assert {row.id for row in audits(database)} == identifiers
    with Session(database[0]) as db:
        assert graph_snapshot(db, chapter) == before


def test_merge_repeated_delivery_and_event_delete_are_idempotent(client, database):
    _, chapter, source, target, _, event = seed(client, database)
    path = f"/api/v1/characters/{source}/merge"
    payload = {"target_character_id": str(target), "reason": "same person"}
    assert client.post(path, json=payload, headers=csrf(client)).status_code == 200
    identifiers = {row.id for row in audits(database)}
    assert client.post(path, json=payload, headers=csrf(client)).status_code == 200
    assert {row.id for row in audits(database)} == identifiers
    path = f"/api/v1/events/{event}"
    assert client.delete(path, headers=csrf(client)).status_code == 200
    rows = audits(database, entity=event)
    assert len(rows) == 1
    assert rows[0].chapter_id == chapter
    assert_reconstructable(rows[0])
    assert client.delete(path, headers=csrf(client)).status_code == 200
    assert [row.id for row in audits(database, entity=event)] == [rows[0].id]
