"""A4: actual directed-edge merge rules, without changing merge semantics."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_story_integrity import seed

from app.models import (
    Character,
    CharacterAlias,
    EventCharacter,
    SceneCharacter,
    StoryAudit,
    StoryRelationship,
)

CASES = {
    "A_to_B": [("a", "b", "ally")],
    "B_to_A": [("b", "a", "ally")],
    "bidirectional": [("a", "b", "ally"), ("b", "a", "ally")],
    "outgoing_duplicates": [("a", "c", "ally"), ("b", "c", "ally")],
    "incoming_duplicates": [("c", "a", "ally"), ("c", "b", "ally")],
    "different_types": [("a", "c", "ally"), ("b", "c", "enemy")],
}


@pytest.mark.parametrize("edges", CASES.values(), ids=CASES)
@pytest.mark.parametrize("evidence_side", ["a", "b", "both"])
@pytest.mark.parametrize("confidence", [(0.2, 0.9), (0.9, 0.2), (0.0, 0.1), (None, None)])
def test_a4_merge_graph_matrix(client, database, edges, evidence_side, confidence):
    _, chapter, a, b, scene, event = seed(client, database)
    evidence = {
        key: [{"page_number": number, "reason": key}] if evidence_side in {key, "both"} else []
        for key, number in (("a", 1), ("b", 2))
    }
    expected = {}
    with Session(database[0]) as db:
        c = Character(chapter_id=chapter, name="Third", display_name="Third")
        db.add(c)
        db.flush()
        ids = {"a": a, "b": b, "c": c.id}
        for key in ("a", "b"):
            record = db.get(Character, ids[key])
            record.description = f"Human {key}"
            record.edited_fields = ["description"]
            record.provenance = "user_edited"
            record.evidence = evidence[key]
            db.add(
                CharacterAlias(
                    character_id=ids[key],
                    alias="Shared",
                    normalized_alias="shared",
                    confidence=0.4 if key == "a" else 0.8,
                    evidence=evidence[key],
                )
            )
        # Both sides already participate: rewiring must not duplicate either edge.
        db.add_all(
            [
                SceneCharacter(scene_id=scene, character_id=b),
                EventCharacter(event_id=event, character_id=b),
            ]
        )
        for index, (source, target, kind) in enumerate(edges):
            side = "a" if "a" in (source, target) else "b"
            values = {} if confidence[index % 2] is None else {"confidence": confidence[index % 2]}
            db.add(
                StoryRelationship(
                    chapter_id=chapter,
                    source_character_id=ids[source],
                    target_character_id=ids[target],
                    relationship_type=kind,
                    evidence=evidence[side],
                    **values,
                )
            )
            new_source, new_target = (
                (b if ids[source] == a else ids[source]),
                (b if ids[target] == a else ids[target]),
            )
            if new_source != new_target:
                key = (new_source, new_target, kind)
                item = expected.setdefault(key, {"evidence": [], "confidence": 0.0})
                item["evidence"] += [e for e in evidence[side] if e not in item["evidence"]]
                item["confidence"] = max(item["confidence"], confidence[index % 2] or 0.0)
        db.commit()

    route = f"/api/v1/characters/{a}/merge"
    payload = {"target_character_id": str(b)}
    response = client.post(route, json=payload, headers=csrf(client))
    assert response.status_code == 200, response.text
    with Session(database[0]) as db:
        relations = db.scalars(select(StoryRelationship)).all()
        assert len(relations) == len(expected)
        for row in relations:
            key = (row.source_character_id, row.target_character_id, row.relationship_type)
            assert row.source_character_id != row.target_character_id
            assert row.confidence == expected[key]["confidence"]
            assert sorted(row.evidence, key=str) == sorted(expected[key]["evidence"], key=str)
        assert db.get(Character, a).description == "Human a"
        assert db.get(Character, b).description == "Human b"
        assert db.get(Character, a).status == "rejected"
        assert sorted(db.get(Character, b).evidence, key=str) == sorted(
            evidence["a"] + evidence["b"], key=str
        )
        aliases = db.scalars(select(CharacterAlias)).all()
        assert len(aliases) == 1 and aliases[0].confidence == 0.8
        assert sorted(aliases[0].evidence, key=str) == sorted(
            evidence["a"] + evidence["b"], key=str
        )
        assert [(r.scene_id, r.character_id) for r in db.scalars(select(SceneCharacter))] == [
            (scene, b)
        ]
        assert [(r.event_id, r.character_id) for r in db.scalars(select(EventCharacter))] == [
            (event, b)
        ]
    # Full operation idempotence (including review state), not just edge counts.
    repeated = client.post(route, json=payload, headers=csrf(client))
    assert repeated.status_code == 200
    assert repeated.json() == response.json()
    with Session(database[0]) as db:
        assert len(db.scalars(select(StoryAudit).where(StoryAudit.action == "merge")).all()) == 1
