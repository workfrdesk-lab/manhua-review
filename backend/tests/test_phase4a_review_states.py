"""A9 canonical review-state acceptance contract.

Architecture boundary: characters, scenes, and events expose explicit review
PATCH endpoints. Relationships and participation edges do not. Participation
has no independent status column; its parents supply review context. Claims use
parent-level provenance and ``edited_fields`` as the source of truth because
Chapter Understanding has no claim row or per-claim provenance column. Once
protected, the entire claims list is preserved, not independently reviewed
claims. An AI-only confirmed/rejected claim is not proof of human review.

Official A9 API contract: global-ID PATCH authorization is USER OWNERSHIP.
Authenticated owners may mutate their entities, including sibling projects
and chapters. Non-owners, unauthenticated users and nonexistent IDs are denied.
There is intentionally no selected-project/chapter authorization parameter.
Embedded JSON claims use normalization, not an independent claim table,
database constraint or provenance field. These are accepted design decisions.
"""

from copy import deepcopy
from itertools import product
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import String, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_ingestion_local import csrf, setup_chapter
from test_phase4a_reconciliation_edges import payload, snapshot
from test_projects import register
from test_story_integrity import seed

from app.models import (
    ChapterUnderstanding,
    Character,
    Event,
    EventCharacter,
    Scene,
    SceneCharacter,
    StoryAudit,
    StoryRelationship,
    StoryVersion,
)
from app.story_review import ReviewState, validate_review_transition
from app.story_schemas import ReviewStatus, StoryStructuredOutput
from app.story_service import persist

REVIEW_MODELS = (Character, Scene, Event, StoryRelationship, ChapterUnderstanding, StoryVersion)


@pytest.mark.parametrize("model", REVIEW_MODELS, ids=lambda model: model.__tablename__)
@pytest.mark.parametrize(
    "original", [None, "legacy", "unknown", "needs_review", "confirmed", "rejected"]
)
def test_all_entity_migration_backfills_and_enforces_constraints(client, database, model, original):
    """Exercise persisted rows and actual Alembic upgrade, not source inspection.

    Historical migrations already made status NOT NULL. Relax that one property
    on the pre-0011 schema to exercise defensive NULL backfill explicitly.
    """
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    engine, config = database
    with Session(engine) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(payload()))
        db.add(StoryVersion(chapter_id=chapter, fingerprint="a" * 64, data={}, decisions=[]))
        db.commit()
    table = model.__tablename__
    command.downgrade(config, "0010_story_audit_context")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table(table) as batch:
            batch.alter_column("status", existing_type=String(30), nullable=True)
        connection.execute(text(f"UPDATE {table} SET status=:state"), {"state": original})
        seeded = connection.execute(text(f"SELECT status FROM {table}")).scalars().all()
        assert seeded and all(value == original for value in seeded)
    expected = original if original in {state.value for state in ReviewState} else "needs_review"
    for _ in range(2):
        command.upgrade(config, "head")
        with engine.connect() as connection:
            values = connection.execute(text(f"SELECT status FROM {table}")).scalars().all()
            assert values == [expected] * len(seeded)
            assert set(values) <= {state.value for state in ReviewState}
            assert None not in values
        for invalid in (None, "unknown", "legacy"):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(f"UPDATE {table} SET status=:state"), {"state": invalid})
        command.downgrade(config, "0010_story_audit_context")
    command.upgrade(config, "head")


@pytest.mark.parametrize(
    "original", [None, "legacy", "unknown", "needs_review", "confirmed", "rejected"]
)
def test_persisted_json_claim_migration_backfill(client, database, original):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    engine, config = database
    with Session(engine) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(payload()))
        db.commit()
    command.downgrade(config, "0010_story_audit_context")
    with Session(engine) as db:
        record = db.scalar(select(ChapterUnderstanding))
        claims = deepcopy(record.claims)
        claims[0]["status"] = original
        record.claims = claims
        db.commit()
        db.expire_all()
        assert record.claims[0]["status"] == original
    expected = original if original in {state.value for state in ReviewState} else "needs_review"
    command.upgrade(config, "head")
    with Session(engine) as db:
        persisted = db.scalar(select(ChapterUnderstanding)).claims
        assert persisted[0]["status"] == expected
        claims = persisted
    command.downgrade(config, "0010_story_audit_context")
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert db.scalar(select(ChapterUnderstanding)).claims == claims


def test_relationship_and_participation_use_reconciliation_contract(client, database):
    """No relationship/participation PATCH exists; reconciliation is the API."""
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    original = StoryStructuredOutput.model_validate(payload())
    with Session(database[0]) as db:
        persist(db, chapter, original)
        db.commit()
        relationship = db.scalar(select(StoryRelationship))
        participation = db.scalar(select(SceneCharacter))
        event_participation = db.scalar(select(EventCharacter))
        for parent_model in (Scene, Event):
            for parent in db.scalars(select(parent_model)):
                parent.status = "confirmed"
                parent.provenance = "user_confirmed"
        relationship.status = "confirmed"
        relationship.provenance = "user_edited"
        relationship.description = "Human relationship"
        db.commit()
        changed = deepcopy(original.model_dump(mode="json"))
        changed["relationships"] = []
        changed["scenes"][0]["character_refs"] = []
        changed["scenes"][0]["events"][0]["character_refs"] = []
        persist(db, chapter, StoryStructuredOutput.model_validate(changed))
        db.commit()
        db.expire_all()
        current = db.get(StoryRelationship, relationship.id)
        assert current.status == ReviewState.CONFIRMED
        assert current.description == "Human relationship"
        assert db.get(SceneCharacter, (participation.scene_id, participation.character_id))
        assert db.get(
            EventCharacter, (event_participation.event_id, event_participation.character_id)
        )
        for edge_model in (SceneCharacter, EventCharacter):
            assert "status" not in edge_model.__table__.c
        for parent_model in (Scene, Event):
            assert all(row.status == "confirmed" for row in db.scalars(select(parent_model)))
        assert all(
            row.status in {ReviewState.NEEDS_REVIEW, ReviewState.CONFIRMED, ReviewState.REJECTED}
            for row in db.scalars(select(StoryRelationship)).all()
        )
        before = snapshot(db)
        persist(db, chapter, StoryStructuredOutput.model_validate(changed))
        db.commit()
        assert snapshot(db) == before
    assert (
        client.patch(
            f"/api/v1/relationships/{relationship.id}",
            json={"status": "rejected"},
            headers=csrf(client),
        ).status_code
        == 404
    )


def test_canonical_enum():
    assert ReviewStatus is ReviewState
    assert {state.value for state in ReviewState} == {"needs_review", "confirmed", "rejected"}
    for model in (
        Character,
        Scene,
        Event,
        StoryRelationship,
        ChapterUnderstanding,
        StoryVersion,
    ):
        assert model.__table__.c.status.type.enum_class is ReviewState
        assert not model.__table__.c.status.nullable


@pytest.mark.parametrize(
    "kind,index,model", [("characters", 2, Character), ("scenes", 4, Scene), ("events", 5, Event)]
)
@pytest.mark.parametrize(
    "case",
    [
        "invalid",
        "null",
        "object",
        "unauthenticated",
        "wrong_user",
        "nonexistent",
        "identifier",
        "payload",
    ],
)
def test_review_patch_rejection_preserves_database(client, database, kind, index, model, case):
    identifier = seed(client, database)[index]
    body = {"status": "confirmed"}
    expected = 422
    headers = csrf(client)
    if case in {"invalid", "null", "object"}:
        body["status"] = {"invalid": "approved", "null": None, "object": {}}[case]
    elif case == "unauthenticated":
        client.post("/api/v1/auth/logout", headers=headers)
        client.cookies.clear()
        client.cookies.set("recap_csrf", headers["x-csrf-token"])
        expected = 401
    elif case == "wrong_user":
        client.post("/api/v1/auth/logout", headers=headers)
        register(client, "a9-other@example.com")
        headers = csrf(client)
        expected = 404
    elif case == "nonexistent":
        identifier = uuid4()
        expected = 404
    elif case == "identifier":
        identifier = "not-a-uuid"
    elif case == "payload":
        body = [body]
    with Session(database[0]) as db:
        before = snapshot(db)
    response = client.patch(f"/api/v1/{kind}/{identifier}", json=body, headers=headers)
    assert response.status_code == expected, response.text
    with Session(database[0]) as db:
        assert snapshot(db) == before


@pytest.mark.parametrize(
    "model,field",
    [
        (Character, "description"),
        (Scene, "summary"),
        (Event, "description"),
        (StoryRelationship, "description"),
        (ChapterUnderstanding, "summary"),
    ],
)
@pytest.mark.parametrize("state", ["confirmed", "rejected"])
@pytest.mark.parametrize("omitted", [False, True])
def test_reviewed_reanalysis_preserves_values(client, database, model, field, state, omitted):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    original = payload()
    with Session(database[0]) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(original))
        db.commit()
        record = db.scalar(select(model))
        identifier = record.id
        record.status = state
        record.provenance = "user_edited"
        setattr(record, field, "Human decision")
        db.commit()
        changed = StoryStructuredOutput.model_validate({"characters": []} if omitted else original)
        persist(db, chapter, changed)
        db.commit()
        assert db.get(model, identifier).status == state
        assert getattr(db.get(model, identifier), field) == "Human decision"
        if not omitted:
            assert any(c.get("field") == field for c in db.get(model, identifier).review_conflicts)
        before = snapshot(db)
        persist(db, chapter, changed)
        db.commit()
        assert snapshot(db) == before


@pytest.mark.parametrize("state", ["confirmed", "rejected"])
@pytest.mark.parametrize("protection", ["provenance", "edited_fields"])
def test_parent_protected_claim_survives_conflicting_analysis(client, database, state, protection):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    original = payload()
    with Session(database[0]) as db:
        persist(db, chapter, StoryStructuredOutput.model_validate(original))
        db.commit()
        record = db.scalar(select(ChapterUnderstanding))
        claims = deepcopy(record.claims)
        claims[0]["status"] = state
        record.claims = claims
        record.provenance = "user_edited"
        if protection == "edited_fields":
            record.edited_fields = ["claims"]
        db.commit()
        changed = deepcopy(original)
        changed["chapter_summary"]["claims"][0]["description"] = "Conflicting proposal"
        persist(db, chapter, StoryStructuredOutput.model_validate(changed))
        db.commit()
        db.expire_all()
        assert record.claims == claims
        assert record.provenance == "user_edited"
        conflicts = [c for c in record.review_conflicts if c.get("field") == "claims"]
        assert len(conflicts) == 1
        assert conflicts[0]["current"] == claims
        assert conflicts[0]["proposed"][0]["description"] == "Conflicting proposal"
        audits = [
            audit
            for audit in db.scalars(select(StoryAudit).where(StoryAudit.action == "ai_conflict"))
            if audit.data.get("field") == "claims"
        ]
        assert len(audits) == 1
        assert audits[0].data["before"] == audits[0].data["after"] == {"claims": claims}
        assert audits[0].data["proposal"] == {"claims": conflicts[0]["proposed"]}
        before = snapshot(db)
        for _ in range(3):
            persist(db, chapter, StoryStructuredOutput.model_validate(changed))
            db.commit()
            db.expire_all()
            assert snapshot(db) == before


@pytest.mark.parametrize(
    "original", [None, "legacy", "unknown", "needs_review", "confirmed", "rejected"]
)
@pytest.mark.parametrize("protected", [False, True])
def test_embedded_claim_reconciliation_normalizes_persisted_states(
    client, database, original, protected
):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    output = StoryStructuredOutput.model_validate(payload())
    with Session(database[0]) as db:
        persist(db, chapter, output)
        db.commit()
        record = db.scalar(select(ChapterUnderstanding))
        claims = deepcopy(record.claims)
        claims[0]["status"] = original
        record.claims = claims
        record.provenance = "user_edited" if protected else "ai_generated"
        record.edited_fields = ["claims"] if protected else []
        db.commit()
        expected = deepcopy(output.chapter_summary.model_dump(mode="json")["claims"])
        if protected:
            expected = deepcopy(claims)
            expected[0]["status"] = original if original in set(ReviewState) else "needs_review"
        persist(db, chapter, output)
        db.commit()
        db.expire_all()
        assert record.claims == expected
        assert all(claim["status"] in set(ReviewState) for claim in record.claims)
        before = snapshot(db)
        persist(db, chapter, output)
        db.commit()
        assert snapshot(db) == before


def test_deleted_event_is_not_resurrected(client, database):
    chapter = UUID(setup_chapter(client).rsplit("/", 1)[1])
    original = StoryStructuredOutput.model_validate(payload())
    with Session(database[0]) as db:
        persist(db, chapter, original)
        db.commit()
        identifier = db.scalar(select(Event.id))
    response = client.delete(f"/api/v1/events/{identifier}", headers=csrf(client))
    assert response.status_code == 200
    with Session(database[0]) as db:
        assert db.get(Event, identifier).status == "rejected"
        assert db.scalar(select(StoryAudit).where(StoryAudit.action == "delete")) is not None
        persist(db, chapter, original)
        db.commit()
        assert db.get(Event, identifier).status == "rejected"
        before = snapshot(db)
        persist(db, chapter, original)
        db.commit()
        assert snapshot(db) == before


@pytest.mark.parametrize("current,requested", list(product(ReviewState, repeat=2)))
def test_transition_contract(current, requested):
    assert validate_review_transition(current, requested) == requested


@pytest.mark.parametrize("invalid", [None, "", "unknown", "approved", 1, [], {}])
def test_invalid_transition(invalid):
    for state in ReviewState:
        with pytest.raises(ValueError):
            validate_review_transition(state, invalid)
        with pytest.raises(ValueError):
            validate_review_transition(invalid, state)


@pytest.mark.parametrize(
    "kind,index,model", [("characters", 2, Character), ("scenes", 4, Scene), ("events", 5, Event)]
)
def test_api_transition_matrix(client, database, kind, index, model):
    identifier = seed(client, database)[index]
    for current, requested in product(ReviewState, repeat=2):
        with Session(database[0]) as db:
            db.get(model, identifier).status = current
            db.commit()
        response = client.patch(
            f"/api/v1/{kind}/{identifier}",
            json={"status": requested.value},
            headers=csrf(client),
        )
        assert response.status_code == 200, response.text
        with Session(database[0]) as db:
            assert db.get(model, identifier).status is requested
            before = len(db.scalars(select(StoryAudit)).all())
            provenance = db.get(model, identifier).provenance
            fields = db.get(model, identifier).edited_fields
        repeated = client.patch(
            f"/api/v1/{kind}/{identifier}",
            json={"status": requested.value},
            headers=csrf(client),
        )
        assert repeated.status_code == 200
        with Session(database[0]) as db:
            assert len(db.scalars(select(StoryAudit)).all()) == before
            assert db.get(model, identifier).status is requested
            assert db.get(model, identifier).provenance == provenance
            assert db.get(model, identifier).edited_fields == fields


@pytest.mark.parametrize("table,index", [("characters", 2), ("scenes", 4), ("events", 5)])
@pytest.mark.parametrize("invalid", [None, "unknown"])
def test_database_rejects_invalid_state(client, database, table, index, invalid):
    identifier = seed(client, database)[index]
    with database[0].begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(f"UPDATE {table} SET status=:status WHERE id=:id"),
                {"status": invalid, "id": identifier.hex},
            )


@pytest.mark.parametrize(
    "kind,index,model", [("characters", 2, Character), ("scenes", 4, Scene), ("events", 5, Event)]
)
@pytest.mark.parametrize("scope", ["wrong_project", "wrong_chapter", "different_user"])
def test_real_cross_scope_review_patch_uses_persisted_ownership(
    client, database, kind, index, model, scope
):
    """Owned sibling scopes are authorized; another owner's scope is private.

    There is no current-project/current-chapter parameter on these global routes.
    An owned sibling cannot truthfully be asserted to reject a valid PATCH.
    """
    route, chapter_id, character_id, _, scene_id, event_id = seed(client, database)
    identifier_by_model = {Character: character_id, Scene: scene_id, Event: event_id}
    with Session(database[0]) as db:
        original = db.get(model, identifier_by_model[model])
        original_id = original.id

    if scope == "wrong_project":
        project = client.post(
            "/api/v1/projects", json={"name": "Other project"}, headers=csrf(client)
        ).json()
        chapter = client.post(
            f"/api/v1/projects/{project['id']}/chapters",
            json={"name": "Other chapter"},
            headers=csrf(client),
        ).json()
        foreign_chapter = UUID(chapter["id"])
    elif scope == "wrong_chapter":
        project_id = client.get("/api/v1/projects").json()[0]["id"]
        chapter = client.post(
            f"/api/v1/projects/{project_id}/chapters",
            json={"name": "Sibling chapter"},
            headers=csrf(client),
        ).json()
        foreign_chapter = UUID(chapter["id"])
    else:
        client.post("/api/v1/auth/logout", headers=csrf(client))
        register(client, "a9-scope-other@example.com")
        foreign_chapter = chapter_id

    if scope != "different_user":
        with Session(database[0]) as db:
            if model is Character:
                foreign = Character(
                    chapter_id=foreign_chapter, name="Foreign", display_name="Foreign", evidence=[]
                )
            elif model is Scene:
                foreign = Scene(
                    chapter_id=foreign_chapter,
                    scene_index=99,
                    title="Foreign",
                    summary="Foreign",
                    start_page=1,
                    end_page=1,
                )
            else:
                scene = Scene(
                    chapter_id=foreign_chapter,
                    scene_index=99,
                    title="Foreign",
                    summary="Foreign",
                    start_page=1,
                    end_page=1,
                )
                db.add(scene)
                db.flush()
                foreign = Event(scene_id=scene.id, event_index=99, description="Foreign")
            db.add(foreign)
            db.commit()
            target_id = foreign.id
    else:
        target_id = original_id

    with Session(database[0]) as db:
        before = snapshot(db)
        before_audits = len(db.scalars(select(StoryAudit)).all())
    response = client.patch(
        f"/api/v1/{kind}/{target_id}", json={"status": "confirmed"}, headers=csrf(client)
    )
    expected = 404 if scope == "different_user" else 200
    assert response.status_code == expected, response.text
    with Session(database[0]) as db:
        after = snapshot(db)
        if scope == "different_user":
            assert after == before
            assert str(target_id) not in response.text
            assert "Foreign" not in response.text
            assert len(db.scalars(select(StoryAudit)).all()) == before_audits
        else:
            assert db.get(model, target_id).status == "confirmed"
            assert db.get(model, original_id).status == "needs_review"
            assert len(db.scalars(select(StoryAudit)).all()) == before_audits + 1
            for entity_model, rows in before.items():
                for key, row in rows.items():
                    if entity_model is model and row.get("id") == target_id:
                        continue
                    assert after[entity_model][key] == row


def test_migration_backfills_deterministically_and_preserves_decisions(database):
    engine, config = database
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'migration@example.com', 'x')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO projects (id, user_id, name) VALUES "
                "('00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000001', 'migration')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO chapters (id, project_id, name) VALUES "
                "('00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000001', 'chapter')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO characters "
                "(id, chapter_id, name, display_name, status, evidence) VALUES "
                "('00000000-0000-0000-0000-000000000001', "
                "'00000000-0000-0000-0000-000000000001', 'A', 'A', 'confirmed', '[]')"
            )
        )
    for original in ("legacy", "confirmed", "rejected", "needs_review"):
        command.downgrade(config, "0010_story_audit_context")
        with engine.begin() as connection:
            connection.execute(text("UPDATE characters SET status = :status"), {"status": original})
        for _ in range(2):
            command.upgrade(config, "head")
            with engine.connect() as connection:
                rows = connection.execute(text("SELECT status FROM characters")).scalars().all()
            assert rows == ["needs_review" if original == "legacy" else original]
            command.downgrade(config, "0010_story_audit_context")
    command.upgrade(config, "head")
