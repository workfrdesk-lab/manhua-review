import hashlib
import io

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_analysis_api import upload_page
from test_ingestion_local import csrf

from app import story_service
from app.models import Page, StoryVersion
from app.storage import get_storage, streaming_sha256
from app.story_providers import LLMResponse
from app.story_resolution import deduplicate_aliases
from app.story_schemas import (
    AliasData,
    CharacterData,
    EventData,
    RelationshipData,
    SceneData,
    StoryStructuredOutput,
    SummaryClaim,
)


@pytest.mark.parametrize(
    "model,fields",
    [
        (CharacterData, {"name": "Jin", "importance": 0.8}),
        (AliasData, {"alias": "Jin"}),
        (EventData, {"description": "Arrives", "importance": 0.8}),
        (
            SceneData,
            {
                "title": "Gate",
                "summary": "Arrival",
                "start_page": 1,
                "end_page": 1,
                "importance": 0.8,
            },
        ),
        (RelationshipData, {"source": "Jin", "target": "Mira", "relationship_type": "ally"}),
        (SummaryClaim, {"description": "Arrives", "event_refs": ["event"]}),
    ],
)
@pytest.mark.parametrize("evidence", [None, []])
def test_factual_schema_requires_evidence(model, fields, evidence):
    payload = {"temp_id": "claim", "confidence": 0.9, "status": "confirmed", **fields}
    if evidence is not None:
        payload["evidence"] = evidence
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert any(item["loc"] == ("evidence",) for item in error.value.errors())


def test_streaming_hash_bounds_reads():
    data = b"x" * (3 * 1024 * 1024 + 7)

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    assert streaming_sha256(BoundedStream(data)) == hashlib.sha256(data).hexdigest()


def test_alias_unicode_case_whitespace_deduplication():
    aliases = [
        AliasData(
            temp_id=str(i),
            alias=name,
            confidence=0.7 + i / 10,
            evidence=[{"page_number": i + 1, "reason": "named"}],
        )
        for i, name in enumerate(["  Jin   Woo ", "jin woo", "Ｊｉｎ Ｗｏｏ"])
    ]
    merged = deduplicate_aliases(aliases)
    assert len(merged) == 1
    assert len(merged[0].evidence) == 3
    assert merged[0].confidence == pytest.approx(0.9)
    assert len(deduplicate_aliases([aliases[0]])) == 1
    assert len(deduplicate_aliases([aliases[1]])) == 1


def test_in_place_page_bytes_create_new_story_version(client, database, monkeypatch):
    upload_page(client)
    with Session(database[0]) as db:
        page = db.scalar(select(Page))
        chapter_id, page_id, key = page.chapter_id, page.id, page.storage_key

    class Provider:
        name = "test-only"
        model = "fixture"

        def generate_structured(self, context):
            return LLMResponse(StoryStructuredOutput(characters=[]), 1, 1)

    monkeypatch.setattr(story_service, "configured_provider", Provider)
    route = f"/api/v1/chapters/{chapter_id}/story/analyze"
    first = client.post(route, headers=csrf(client))
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    original = first.json()["source_fingerprint"]
    assert client.post(route, headers=csrf(client)).json()["source_fingerprint"] == original
    storage = get_storage()
    storage.put(key, storage.get(key) + b"changed source bytes", "image/jpeg")
    second = client.post(route, headers=csrf(client))
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert second.json()["source_fingerprint"] != original
    with Session(database[0]) as db:
        assert db.scalar(select(Page.id)) == page_id
        assert len(db.scalars(select(StoryVersion)).all()) == 2
