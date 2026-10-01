from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, setup_chapter
from test_projects import register

from app import story
from app.models import (
    Character,
    Event,
    EventCharacter,
    Scene,
    SceneCharacter,
    StoryAnalysisJob,
    StoryAudit,
    StoryRelationship,
    StoryVersion,
)
from app.story_resolution import resolve_story
from app.story_schemas import CharacterData, StoryStructuredOutput
from app.story_service import fingerprint, persist


def test_concurrent_analyze_requests(client, database, monkeypatch):
    route = setup_chapter(client)
    submitted = []

    class Queue:
        def submit(self, job_id):
            submitted.append(job_id)

    monkeypatch.setattr(story, "get_job_queue", Queue)
    barrier = Barrier(2)
    headers = csrf(client)

    def send():
        barrier.wait(timeout=10)
        return client.post(route + "/story/analyze", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["id"] == results[1].json()["id"]
    assert len(submitted) == 1
    with Session(database[0]) as db:
        assert len(db.scalars(select(StoryAnalysisJob)).all()) == 1


def seed(client, database):
    route = setup_chapter(client)
    chapter_id = UUID(route.rsplit("/", 1)[1])
    with Session(database[0]) as db:
        a = Character(chapter_id=chapter_id, name="John", display_name="John", evidence=[])
        b = Character(chapter_id=chapter_id, name="Jin", display_name="Jin", evidence=[])
        scene = Scene(
            chapter_id=chapter_id,
            scene_index=1,
            title="Gate",
            summary="Wait",
            start_page=1,
            end_page=1,
        )
        db.add_all([a, b, scene])
        db.flush()
        event = Event(scene_id=scene.id, event_index=1, description="Wait")
        db.add(event)
        db.flush()
        db.add_all(
            [
                SceneCharacter(scene_id=scene.id, character_id=a.id),
                EventCharacter(event_id=event.id, character_id=a.id),
            ]
        )
        db.commit()
        return route, chapter_id, a.id, b.id, scene.id, event.id


def test_manual_edit_preserved_and_proposal_conflict(client, database):
    route, chapter_id, a, _, _, _ = seed(client, database)
    from uuid import uuid4

    evidence = {"page_id": str(uuid4()), "page_number": 1, "reason": "fixture"}
    with Session(database[0]) as db:
        db.get(Character, a).evidence = [evidence]
        db.commit()
    response = client.patch(
        f"/api/v1/characters/{a}", json={"name": "Jin Woo"}, headers=csrf(client)
    )
    assert response.status_code == 200
    output, _ = resolve_story(
        [
            StoryStructuredOutput(
                characters=[
                    CharacterData(
                        temp_id="a",
                        name="John",
                        importance=0.5,
                        confidence=0.8,
                        evidence=[evidence],
                    )
                ]
            )
        ]
    )
    with Session(database[0]) as db:
        version = StoryVersion(
            chapter_id=chapter_id,
            fingerprint="a" * 64,
            data=output.model_dump(mode="json"),
            decisions=[],
        )
        db.add(version)
        db.flush()
        persist(db, chapter_id, output, version.id)
        db.commit()
        assert db.get(Character, a).name == "Jin Woo"
        assert db.get(Character, a).review_conflicts
    assert len(client.get(route + "/story/versions").json()) == 1


def test_merge_split_preserve_references_and_audit(client, database):
    _, _, a, b, scene, event = seed(client, database)
    response = client.post(
        f"/api/v1/characters/{a}/merge",
        json={"target_character_id": str(b), "reason": "same person"},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    with Session(database[0]) as db:
        assert db.get(SceneCharacter, (scene, b))
        assert db.get(EventCharacter, (event, b))
        assert db.get(Character, a).status == "rejected"
    response = client.post(
        f"/api/v1/characters/{b}/split",
        headers=csrf(client),
        json={
            "retained_name": "Jin",
            "new_name": "John",
            "scene_ids": [str(scene)],
            "event_ids": [str(event)],
            "reason": "two people",
        },
    )
    assert response.status_code == 200, response.text
    created = UUID(response.json()["created"]["id"])
    with Session(database[0]) as db:
        assert db.get(SceneCharacter, (scene, created))
        assert db.get(EventCharacter, (event, created))
        assert db.get(SceneCharacter, (scene, b)) is None
        assert len(db.scalars(select(StoryAudit)).all()) == 2


def test_story_user_isolation(client, database):
    route, _, a, b, scene, event = seed(client, database)
    client.post("/api/v1/auth/logout", headers=csrf(client))
    register(client, "other-story@example.com")
    for suffix in ("/characters", "/scenes", "/relationships", "/story/versions", "/story/summary"):
        assert client.get(route + suffix).status_code == 404
    assert client.get(f"/api/v1/scenes/{scene}/events").status_code == 404
    assert (
        client.patch(
            f"/api/v1/characters/{a}", headers=csrf(client), json={"name": "stolen"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/characters/{a}/merge",
            headers=csrf(client),
            json={"target_character_id": str(b)},
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/events/{event}", headers=csrf(client)).status_code == 404


def test_fingerprint_configuration_and_migration_roundtrip(client, database, monkeypatch):
    from app.config import get_settings

    route = setup_chapter(client)
    chapter_id = UUID(route.rsplit("/", 1)[1])
    with Session(database[0]) as db:
        original = fingerprint(db, chapter_id)[0]
        assert original == fingerprint(db, chapter_id)[0]
        monkeypatch.setenv("LLM_MODEL", "different-fixture-model")
        get_settings.cache_clear()
        assert original != fingerprint(db, chapter_id)[0]
    command.downgrade(database[1], "0006_story_review")
    assert "story_versions" not in inspect(database[0]).get_table_names()
    command.upgrade(database[1], "head")
    assert "story_versions" in inspect(database[0]).get_table_names()


def test_cross_chapter_relationship_is_rejected_by_database(client, database):
    first_route = setup_chapter(client)
    project = client.post(
        "/api/v1/projects", json={"name": "Other project"}, headers=csrf(client)
    ).json()
    second = client.post(
        f"/api/v1/projects/{project['id']}/chapters",
        json={"name": "Other chapter"},
        headers=csrf(client),
    ).json()
    second_route = f"/api/v1/chapters/{second['id']}"
    first_id = UUID(first_route.rsplit("/", 1)[1])
    second_id = UUID(second_route.rsplit("/", 1)[1])
    with Session(database[0]) as db:
        if database[0].dialect.name == "sqlite":
            db.execute(text("PRAGMA foreign_keys=ON"))
        first = Character(chapter_id=first_id, name="First", display_name="First")
        second = Character(chapter_id=second_id, name="Second", display_name="Second")
        db.add_all([first, second])
        db.flush()
        db.add(
            StoryRelationship(
                chapter_id=first_id,
                source_character_id=first.id,
                target_character_id=second.id,
                relationship_type="cross-chapter",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
