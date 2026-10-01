import pytest
from test_ingestion_local import csrf, setup_chapter

from app import story_service
from app.story_context import ContextChunker
from app.story_providers import LLMResponse, ProviderNotConfigured
from app.story_schemas import StoryStructuredOutput


class FakeLLMProvider:
    """Test-only provider. Never registered in application provider selection."""

    name = "test-only"
    model = "fixture"

    def generate_structured(self, context):
        return LLMResponse(StoryStructuredOutput(summary="Test fixture"), 10, 5)


def test_missing_provider(client, monkeypatch):
    route = setup_chapter(client)

    def missing():
        raise ProviderNotConfigured("LLM provider not configured")

    monkeypatch.setattr(story_service, "configured_provider", missing)
    assert client.get(route + "/story/status").json()["status"] == "not_started"
    result = client.post(route + "/story/analyze", headers=csrf(client))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "provider_not_configured"
    assert client.get(route + "/characters").json() == []
    assert client.post(route + "/story/analyze").status_code == 403


@pytest.mark.parametrize("error", [TimeoutError, RuntimeError])
def test_provider_failure(client, monkeypatch, error):
    route = setup_chapter(client)

    def failed():
        raise error("test provider failed")

    monkeypatch.setattr(story_service, "configured_provider", failed)
    result = client.post(route + "/story/analyze", headers=csrf(client))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed"


def test_chunking_page_overlap():
    context = "\n\n".join(f"Page {i}\n\nPanel 1\nOCR: {'x' * 80}" for i in range(1, 6))
    chunks = ContextChunker(max_chars=260).chunk(context)
    assert len(chunks) > 1
    assert chunks[0].pages[-1] == chunks[1].pages[0]


def test_invalid_structured_json():
    with pytest.raises(ValueError):
        StoryStructuredOutput.model_validate_json("not json")
    with pytest.raises(ValueError):
        StoryStructuredOutput.model_validate({"characters": [{"name": "Jin"}]})
