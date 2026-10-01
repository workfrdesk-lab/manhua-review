"""Partial graph/audit regression, NOT the requested final lifecycle gate.

Uses seeded Story rows. Reanalysis, retries, concurrency, ownership isolation,
merge/split, and relationship mutations are not exercised here. Phase 4A must
not be closed on the strength of this test.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_story_integrity import seed

from app.models import (
    Character,
    Event,
    EventCharacter,
    Scene,
    SceneCharacter,
    StoryAudit,
    StoryRelationship,
)


def test_seeded_graph_integrity_and_character_audit_contract(client, database):
    route, chapter, character, target, scene, event = seed(client, database)
    assert client.get(route + "/characters").status_code == 200
    assert client.get(route + "/scenes").status_code == 200
    assert client.get(route + "/relationships").status_code == 200
    assert client.get(route + "/story/versions").status_code == 200
    assert client.get(route + "/story/summary").status_code == 200

    edited = client.patch(
        f"/api/v1/characters/{character}",
        json={"name": "Human-reviewed John", "status": "confirmed"},
        headers=csrf(client),
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "confirmed"

    repeated = client.patch(
        f"/api/v1/characters/{character}",
        json={"name": "Human-reviewed John", "status": "confirmed"},
        headers=csrf(client),
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == str(character)

    with Session(database[0]) as db:
        rows = db.scalars(select(StoryAudit).where(StoryAudit.entity_id == character)).all()
        assert rows
        assert all(row.actor_id for row in rows)
        assert all(row.entity_type == "character" for row in rows)
        assert all("before" in row.data and "after" in row.data for row in rows)

        characters = db.scalars(select(Character).where(Character.chapter_id == chapter)).all()
        scenes = db.scalars(select(Scene).where(Scene.chapter_id == chapter)).all()
        events = db.scalars(select(Event).join(Scene).where(Scene.chapter_id == chapter)).all()
        relationships = db.scalars(
            select(StoryRelationship).where(StoryRelationship.chapter_id == chapter)
        ).all()
        assert len({row.id for row in characters}) == len(characters)
        assert len({row.id for row in scenes}) == len(scenes)
        assert len({row.id for row in events}) == len(events)
        assert len({row.id for row in relationships}) == len(relationships)
        assert all(row.chapter_id == chapter for row in characters + scenes)
        assert all(row.scene_id in {scene.id for scene in scenes} for row in events)
        assert all(
            row.character_id in {character.id for character in characters}
            for row in db.scalars(select(SceneCharacter)).all()
        )
        assert all(
            row.character_id in {character.id for character in characters}
            for row in db.scalars(select(EventCharacter)).all()
        )
        assert all(
            row.source_character_id in {character.id for character in characters}
            and row.target_character_id in {character.id for character in characters}
            for row in relationships
        )
        assert db.get(Character, target).chapter_id == chapter
