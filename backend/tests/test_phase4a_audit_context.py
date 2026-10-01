from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ingestion_local import csrf
from test_story_integrity import seed

from app.models import StoryAudit


def test_user_story_audit_is_reconstructible_and_has_no_secret_fields(client, database):
    _, _, character, _, _, _ = seed(client, database)
    response = client.patch(
        f"/api/v1/characters/{character}",
        json={"description": "Reviewed description", "status": "confirmed"},
        headers=csrf(client),
    )
    assert response.status_code == 200
    with Session(database[0]) as db:
        audit = db.scalar(select(StoryAudit).where(StoryAudit.entity_id == character))
        assert audit is not None
        assert audit.actor_id is not None
        assert audit.entity_type == "character"
        assert audit.entity_id == character
        assert audit.data["before"]["description"] is None
        assert audit.data["after"]["description"] == "Reviewed description"
        serialized = str(audit.data).lower()
        assert all(secret not in serialized for secret in ("api_key", "token", "password"))
