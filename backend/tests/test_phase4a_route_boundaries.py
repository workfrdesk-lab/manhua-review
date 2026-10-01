"""Executable boundary checks for every registered Story route.

This suite deliberately does not claim to cover the complete success/state matrix.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_phase4a_route_inventory import STORY_ROUTES
from test_projects import register
from test_story_integrity import seed

from app.models import Character, Event, Scene, StoryAudit
from app.story_review import VALID_REVIEW_STATES, validate_review_transition


def request_case(client, method, template, ids, **kwargs):
    payload = None
    if method == "PATCH":
        payload = {"status": "confirmed"}
    elif template.endswith("/merge"):
        payload = {"target_character_id": str(ids["target_id"])}
    elif template.endswith("/split"):
        payload = {
            "retained_name": "John",
            "new_name": "Other",
            "scene_ids": [],
            "event_ids": [],
            "reason": "Acceptance boundary check",
        }
    elif method == "POST":
        payload = {}
    return client.request(method, template.format(**ids), json=payload, **kwargs)


@pytest.mark.parametrize("method,template", sorted(STORY_ROUTES))
def test_every_story_route_rejects_invalid_missing_and_foreign_resources(
    client, database, method, template
):
    _, chapter, character, target, scene, event = seed(client, database)
    ids = dict(
        chapter_id=chapter,
        character_id=character,
        target_id=target,
        scene_id=scene,
        event_id=event,
    )
    for replacement, expected in (("not-a-uuid", 422), (str(uuid4()), 404)):
        bad = {key: replacement if key != "target_id" else value for key, value in ids.items()}
        response = request_case(client, method, template, bad, headers=csrf(client))
        assert response.status_code == expected, response.text
        assert isinstance(response.json(), dict)

    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    register(client, "route-intruder@example.com")
    response = request_case(client, method, template, ids, headers=csrf(client))
    assert response.status_code == 404, response.text
    assert isinstance(response.json(), dict)
    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    response = request_case(client, method, template, ids)
    assert response.status_code in (401, 403), response.text
    assert isinstance(response.json(), dict)
    with Session(database[0]) as db:
        assert db.get(Character, character).name == "John"
        assert db.get(Character, character).status == "needs_review"
        assert db.get(Scene, scene).status == "needs_review"
        assert db.get(Event, event).status == "needs_review"
        assert db.scalars(select(StoryAudit)).all() == []


@pytest.mark.parametrize("current", sorted(VALID_REVIEW_STATES))
@pytest.mark.parametrize("requested", sorted(VALID_REVIEW_STATES))
@pytest.mark.parametrize(
    "kind,index,model",
    [("characters", 2, Character), ("scenes", 4, Scene), ("events", 5, Event)],
)
def test_explicit_human_review_transitions(
    client, database, current, requested, kind, index, model
):
    identifier = seed(client, database)[index]
    with Session(database[0]) as db:
        db.get(model, identifier).status = current
        db.commit()
    for _ in range(2):
        response = client.patch(
            f"/api/v1/{kind}/{identifier}",
            json={"status": requested},
            headers=csrf(client),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == requested
        assert response.json()["id"] == str(identifier)
        with Session(database[0]) as db:
            assert db.get(model, identifier).status == requested


@pytest.mark.parametrize("invalid", [None, "deleted", "approved", "", 1])
@pytest.mark.parametrize("valid", sorted(VALID_REVIEW_STATES))
def test_state_machine_rejects_unknown_source_and_destination(invalid, valid):
    with pytest.raises(ValueError):
        validate_review_transition(invalid, valid)
    with pytest.raises(ValueError):
        validate_review_transition(valid, invalid)


@pytest.mark.parametrize(
    "kind,index,model", [("characters", 2, Character), ("scenes", 4, Scene), ("events", 5, Event)]
)
@pytest.mark.parametrize("invalid", [None, "deleted", "approved", "", 1])
def test_review_patch_rejects_invalid_status_without_mutation(
    client, database, kind, index, model, invalid
):
    identifier = seed(client, database)[index]
    response = client.patch(
        f"/api/v1/{kind}/{identifier}", json={"status": invalid}, headers=csrf(client)
    )
    assert response.status_code == 422, response.text
    with Session(database[0]) as db:
        assert db.get(model, identifier).status == "needs_review"
        assert db.scalars(select(StoryAudit)).all() == []
