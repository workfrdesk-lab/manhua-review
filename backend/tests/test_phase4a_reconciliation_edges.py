"""A3 persisted reconciliation matrix; synthetic data only."""

from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, setup_chapter

from app.models import (
    ChapterUnderstanding,
    Character,
    CharacterAlias,
    Event,
    EventCharacter,
    Scene,
    SceneCharacter,
    StoryAudit,
    StoryRelationship,
)
from app.story_resolution import normalize
from app.story_schemas import StoryStructuredOutput
from app.story_service import persist

MODELS = (
    Character,
    CharacterAlias,
    Scene,
    Event,
    SceneCharacter,
    EventCharacter,
    StoryRelationship,
    ChapterUnderstanding,
    StoryAudit,
)
CASES = (
    "new_character",
    "omitted_character",
    "new_participation",
    "omitted_participation",
    "relationship_type",
    "new_edge",
    "claim_change",
    "reviewed_disappearance",
)


def evidence(page):
    return [{"page_number": page, "reason": f"Synthetic source {page}"}]


def character(name, page):
    return dict(
        temp_id=name,
        name=name,
        importance=0.5,
        confidence=0.8,
        evidence=evidence(page),
        aliases=[
            dict(
                temp_id=f"alias-{name}",
                alias=f" {name.upper()}  alias ",
                confidence=0.7,
                evidence=evidence(page),
            )
        ],
    )


def relationship(source, target):
    return dict(
        temp_id=f"{source}-{target}",
        source=source,
        target=target,
        source_ref=source,
        target_ref=target,
        relationship_type="ally",
        description="Synthetic alliance",
        confidence=0.8,
        evidence=evidence(1),
    )


def payload():
    scenes = []
    for page, name in enumerate(("Ada", "Cora"), 1):
        scenes.append(
            dict(
                temp_id=f"s{page}",
                title=f"Scene {page}",
                summary="Waiting",
                start_page=page,
                end_page=page,
                importance=0.5,
                confidence=0.8,
                evidence=evidence(page),
                character_refs=[name],
                events=[
                    dict(
                        temp_id=f"e{page}",
                        description=f"Wait {page}",
                        importance=0.5,
                        confidence=0.8,
                        evidence=evidence(page),
                        character_refs=[name],
                    )
                ],
            )
        )
    return dict(
        characters=[character(n, i) for i, n in enumerate(("Ada", "Ben", "Cora"), 1)],
        scenes=scenes,
        relationships=[relationship("Ada", "Ben"), relationship("Ben", "Cora")],
        logline="Waiting",
        summary="Waiting",
        chapter_summary=dict(
            confidence=0.8,
            evidence=evidence(1),
            claims=[
                dict(
                    temp_id="claim",
                    description="Wait 1",
                    event_refs=["e1"],
                    confidence=0.8,
                    evidence=evidence(1),
                )
            ],
        ),
    )


def snapshot(db):
    return {
        model: {
            inspect(row).identity: deepcopy(
                {c.name: getattr(row, c.name) for c in model.__table__.columns}
            )
            for row in db.scalars(select(model))
        }
        for model in MODELS
    }


def integrity(db):
    chars = {r.id: r for r in db.scalars(select(Character))}
    scenes = {r.id: r for r in db.scalars(select(Scene))}
    events = {r.id: r for r in db.scalars(select(Event))}
    assert len({(r.chapter_id, normalize(r.name)) for r in chars.values()}) == len(chars)
    edges = db.scalars(select(StoryRelationship)).all()
    keys = [(r.chapter_id, r.source_character_id, r.target_character_id) for r in edges]
    assert len(keys) == len(set(keys))
    for row in edges:
        assert row.source_character_id != row.target_character_id
        assert chars[row.source_character_id].chapter_id == row.chapter_id
        assert chars[row.target_character_id].chapter_id == row.chapter_id
    for row in events.values():
        assert row.scene_id in scenes
    for row in db.scalars(select(SceneCharacter)):
        assert scenes[row.scene_id].chapter_id == chars[row.character_id].chapter_id
    for row in db.scalars(select(EventCharacter)):
        assert (
            scenes[events[row.event_id].scene_id].chapter_id == chars[row.character_id].chapter_id
        )
    aliases = db.scalars(select(CharacterAlias)).all()
    assert len(aliases) == len({(r.character_id, r.normalized_alias) for r in aliases})
    for row in aliases:
        assert row.character_id in chars and row.normalized_alias == normalize(row.alias)


def exercise(client, database, cases):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    initial = payload()
    with Session(database[0]) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(initial))
        db.commit()
        ada = db.scalar(select(Character).where(Character.name == "Ada"))
        ada_id = ada.id
        scenes = db.scalars(select(Scene).order_by(Scene.scene_index)).all()
        scene_id = scenes[0].id
        events = db.scalars(select(Event).where(Event.scene_id == scene_id)).all()
        event_id = events[0].id
    response = client.patch(
        f"/api/v1/characters/{ada_id}",
        headers=csrf(client),
        json={"description": "Human description", "status": "confirmed"},
    )
    assert response.status_code == 200
    with Session(database[0]) as db:
        for model in (Scene, Event, StoryRelationship, ChapterUnderstanding):
            for index, row in enumerate(db.scalars(select(model))):
                row.status = "confirmed" if index == 0 else "rejected"
                row.provenance = "user_confirmed" if index == 0 else "user_edited"
        cora = db.scalar(select(Character).where(Character.name == "Cora"))
        cora.status, cora.provenance = "rejected", "user_edited"
        db.commit()
        before = snapshot(db)
        changed = deepcopy(initial)
        if "new_character" in cases:
            new = character("Dina", 4)
            new["aliases"].append(
                {**new["aliases"][0], "temp_id": "duplicate", "alias": "dina alias"}
            )
            changed["characters"].append(new)
        if "omitted_character" in cases or "reviewed_disappearance" in cases:
            changed["characters"] = [c for c in changed["characters"] if c["name"] != "Cora"]
            changed["relationships"] = changed["relationships"][:1]
            changed["scenes"] = changed["scenes"][:1]
        if "new_participation" in cases:
            changed["scenes"][0]["character_refs"].append("Ben")
            changed["scenes"][0]["events"][0]["character_refs"].append("Ben")
        if "omitted_participation" in cases:
            changed["scenes"][0]["character_refs"].remove("Ada")
            changed["scenes"][0]["events"][0]["character_refs"].remove("Ada")
        if "relationship_type" in cases:
            changed["relationships"][0]["relationship_type"] = "enemy"
        if "new_edge" in cases:
            changed["relationships"].append(relationship("Ben", "Ada"))
        if "claim_change" in cases:
            changed["logline"] = changed["summary"] = "Escape"
            changed["chapter_summary"]["claims"][0]["description"] = "Escape"
        # Every case challenges a manual edit as well as its specific change.
        changed["characters"][0]["description"] = "AI replacement"
        value = StoryStructuredOutput.model_validate(changed)
        persist(db, chapter, value)
        db.commit()
        db.expire_all()
        after = snapshot(db)
        integrity(db)
        # All original identities, evidence, confidence, aliases, edits, states and audits survive.
        for model, rows in before.items():
            assert rows.keys() <= after[model].keys()
            for key, original in rows.items():
                current = after[model][key]
                for field in ("confidence", "status", "provenance", "edited_fields"):
                    if field in original:
                        assert current[field] == original[field]
                if "evidence" in original:
                    assert all(e in current["evidence"] for e in original["evidence"])
                for field in original.get("edited_fields", []):
                    assert current[field] == original[field]
                if model in (CharacterAlias, StoryAudit, SceneCharacter, EventCharacter):
                    assert current == original
        assert db.get(Character, ada_id).description == "Human description"
        if "new_character" in cases:
            assert len(after[Character]) == len(before[Character]) + 1
            dina = db.scalar(select(Character).where(Character.name == "Dina"))
            assert dina.confidence == 0.8
            assert dina.evidence == [
                e.model_dump(mode="json") for e in value.characters[-1].evidence
            ]
            assert (
                len(
                    db.scalars(
                        select(CharacterAlias).where(CharacterAlias.character_id == dina.id)
                    ).all()
                )
                == 1
            )
        else:
            assert len(after[Character]) == len(before[Character])
        ben = db.scalar(select(Character).where(Character.name == "Ben"))
        if "new_participation" in cases:
            assert db.get(SceneCharacter, (scene_id, ben.id))
            assert db.get(EventCharacter, (event_id, ben.id))
        assert db.get(SceneCharacter, (scene_id, ada_id))
        assert db.get(EventCharacter, (event_id, ada_id))
        if "relationship_type" in cases:
            relation = db.scalar(
                select(StoryRelationship).where(StoryRelationship.source_character_id == ada_id)
            )
            assert relation.relationship_type == "ally"
            assert any(c.get("proposed") == "enemy" for c in relation.review_conflicts)
        assert len(after[StoryRelationship]) == len(before[StoryRelationship]) + int(
            "new_edge" in cases
        )
        assert len(after[Scene]) == len(before[Scene])
        assert len(after[Event]) == len(before[Event])
        if "new_edge" in cases:
            added = after[StoryRelationship].keys() - before[StoryRelationship].keys()
            assert len(added) == 1
            row = after[StoryRelationship][added.pop()]
            assert row["confidence"] == 0.8
            assert row["evidence"] == [
                e.model_dump(mode="json") for e in value.relationships[-1].evidence
            ]
        if "claim_change" in cases:
            summary = db.scalar(select(ChapterUnderstanding))
            assert summary.summary == summary.logline == "Waiting"
            assert summary.claims[0]["description"] == "Wait 1"
            assert {c.get("field") for c in summary.review_conflicts} >= {
                "claims",
                "summary",
                "logline",
            }
        if "omitted_character" in cases:
            assert any(c.get("kind") == "ai_omission" for c in cora.review_conflicts)
        persist(db, chapter, value)
        db.commit()
        db.expire_all()
        assert snapshot(db) == after
        integrity(db)
    if "omitted_participation" in cases:
        # Explicit DELETE is a human soft rejection, unlike omission, and retains dependents.
        response = client.delete(f"/api/v1/events/{event_id}", headers=csrf(client))
        assert response.status_code == 200
        with Session(database[0]) as db:
            assert db.get(Event, event_id).status == "rejected"
            assert db.get(EventCharacter, (event_id, ada_id))
            assert db.scalar(select(StoryAudit).where(StoryAudit.action == "delete"))


@pytest.mark.parametrize("case", CASES, ids=CASES)
def test_a3_eight_case_matrix(client, database, case):
    exercise(client, database, {case})


def test_a3_combined_synthetic_lifecycle(client, database):
    exercise(client, database, set(CASES))


def test_a3_unreviewed_graph_is_additive_without_identity_rebuild(client, database):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    initial = payload()
    with Session(database[0]) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(initial))
        db.commit()
        before = snapshot(db)
        changed = deepcopy(initial)
        changed["characters"] = changed["characters"][:2]
        changed["relationships"] = changed["relationships"][:1]
        changed["relationships"][0]["relationship_type"] = "enemy"
        changed["scenes"] = changed["scenes"][:1]
        new_scene = deepcopy(initial["scenes"][0])
        new_scene.update(temp_id="new-scene", start_page=4, end_page=4, evidence=evidence(4))
        new_scene["events"][0].update(temp_id="new-event", evidence=evidence(4))
        changed["scenes"].append(new_scene)
        value = StoryStructuredOutput.model_validate(changed)
        persist(db, chapter, value)
        db.commit()
        after = snapshot(db)
        for model in MODELS:
            assert before[model].keys() <= after[model].keys()
        assert len(after[Scene]) == len(before[Scene]) + 1
        assert len(after[Event]) == len(before[Event]) + 1
        assert len(after[Character]) == len(before[Character])
        assert len(after[StoryRelationship]) == len(before[StoryRelationship])
        ada = db.scalar(select(Character).where(Character.name == "Ada"))
        relation = db.scalar(
            select(StoryRelationship).where(StoryRelationship.source_character_id == ada.id)
        )
        assert relation.relationship_type == "enemy"
        assert relation.confidence == 0.8
        assert relation.evidence == [
            e.model_dump(mode="json") for e in value.relationships[0].evidence
        ]
        persist(db, chapter, value)
        db.commit()
        assert snapshot(db) == after
        integrity(db)
