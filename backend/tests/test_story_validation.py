from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from test_ingestion_local import setup_chapter

from app.jobs import LocalJobQueue
from app.models import StoryAnalysisJob
from app.story_context import ContextChunker
from app.story_schemas import EvidenceRef, StoryStructuredOutput
from app.story_validation import StoryValidationError, validate_story


@pytest.fixture
def source():
    page = SimpleNamespace(id=uuid4(), page_number=1)
    panel = SimpleNamespace(id=uuid4(), page_id=page.id)
    ocr = SimpleNamespace(id=uuid4(), panel_id=panel.id, text="Mira opens the gate.")
    evidence = dict(
        page_id=page.id,
        page_number=1,
        panel_id=panel.id,
        ocr_result_id=ocr.id,
        quote="Mira",
        reason="Named in dialogue",
    )
    output = StoryStructuredOutput.model_validate(
        {
            "characters": [
                {
                    "temp_id": "c1",
                    "name": "Mira",
                    "importance": 0.8,
                    "confidence": 0.9,
                    "evidence": [evidence],
                }
            ]
        }
    )
    return output, [page], [panel], [ocr]


def test_valid_evidence(source):
    assert validate_story(*source) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_id", uuid4()),
        ("panel_id", uuid4()),
        ("ocr_result_id", uuid4()),
        ("page_number", 2),
        ("quote", "invented dialogue"),
    ],
)
def test_reject_hallucinated_evidence(source, field, value):
    setattr(source[0].characters[0].evidence[0], field, value)
    with pytest.raises(StoryValidationError):
        validate_story(*source)


def test_cross_chapter_evidence(source):
    output, _, panels, ocr = source
    with pytest.raises(StoryValidationError):
        validate_story(output, [SimpleNamespace(id=uuid4(), page_number=1)], panels, ocr)


def test_empty_evidence_is_rejected(source):
    character = source[0].characters[0]
    character.evidence = []
    character.status = "confirmed"
    with pytest.raises(StoryValidationError, match="invalid source references"):
        validate_story(*source)


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"reason": "no source"},
        {
            "page_id": "not-a-uuid",
            "reason": "malformed",
        },
    ],
)
def test_malformed_evidence(evidence):
    with pytest.raises(ValueError):
        EvidenceRef.model_validate(evidence)


def test_invalid_relationship_character(source):
    output = source[0]
    payload = output.model_dump()
    payload["relationships"] = [
        {
            "temp_id": "r1",
            "source": "Mira",
            "target": "Unknown",
            "source_ref": "c1",
            "target_ref": "missing",
            "relationship_type": "ally",
            "confidence": 0.8,
            "evidence": [{"page_number": 1, "reason": "fixture"}],
        }
    ]
    with pytest.raises(StoryValidationError):
        validate_story(StoryStructuredOutput.model_validate(payload), *source[1:])


@pytest.mark.parametrize("start,end", [(2, 1), (1, 2)])
def test_invalid_scene_range(source, start, end):
    payload = source[0].model_dump()
    payload["scenes"] = [
        {
            "temp_id": "s1",
            "title": "Gate",
            "summary": "Gate opens",
            "start_page": start,
            "end_page": end,
            "importance": 0.5,
            "confidence": 0.8,
        }
    ]
    with pytest.raises(ValueError):
        validate_story(StoryStructuredOutput.model_validate(payload), *source[1:])


def test_empty_chunk_context():
    assert ContextChunker().chunk("  ") == []


def test_large_chapter_chunk_bounds():
    text = "\n\n".join(f"Page {i}\n{'x' * 100}" for i in range(1, 1001))
    chunks = ContextChunker(max_chars=400, max_pages=3, overlap_pages=1).chunk(text)
    assert all(len(c.text) <= 400 and len(c.pages) <= 3 for c in chunks)
    assert {p for c in chunks for p in c.pages} == set(range(1, 1001))


def test_zero_overlap():
    chunks = ContextChunker(max_chars=100, overlap_pages=0).chunk(
        "\n\n".join(f"Page {i}\n{'x' * 60}" for i in range(1, 5))
    )
    assert [c.pages for c in chunks] == [[1], [2], [3], [4]]


def test_oversized_page_fails_closed():
    with pytest.raises(ValueError, match="exceeds"):
        ContextChunker(max_chars=100).chunk("Page 1\n" + "x" * 200)


def test_story_queue_cancellation(client, database):
    from uuid import UUID

    chapter_id = UUID(setup_chapter(client).rsplit("/", 1)[-1])
    with Session(database[0]) as db:
        job = StoryAnalysisJob(chapter_id=chapter_id)
        db.add(job)
        db.commit()
        job_id = "story:" + str(job.id)
    queue = LocalJobQueue()
    assert queue.get_status(job_id) == "pending"
    assert queue.cancel(job_id)
    assert not queue.cancel(job_id)
    queue.submit(job_id)
    assert queue.get_status(job_id) == "cancelled"


def test_story_migration_roundtrip(database):
    from alembic import command
    from sqlalchemy import inspect

    engine, config = database
    command.downgrade(config, "0005_story")
    assert "evidence" not in {c["name"] for c in inspect(engine).get_columns("characters")}
    command.upgrade(config, "head")
    assert "evidence" in {c["name"] for c in inspect(engine).get_columns("characters")}


def test_successful_extraction_persists_evidence(client, database, monkeypatch):
    from sqlalchemy import select
    from test_analysis_api import FixtureOCR, upload_page
    from test_ingestion_local import csrf

    from app import analysis_service, story_service
    from app.analysis_providers import BasicVisualAnalyzer, OpenCVPanelDetector
    from app.models import OCRResult, Page, Panel
    from app.story_providers import LLMResponse

    monkeypatch.setattr(
        analysis_service,
        "providers",
        lambda: (OpenCVPanelDetector(), FixtureOCR(0.9), BasicVisualAnalyzer()),
    )
    page_id = upload_page(client)
    assert client.post(f"/api/v1/pages/{page_id}/analyze", headers=csrf(client)).status_code == 200
    with Session(database[0]) as db:
        page = db.scalar(select(Page))
        panel = db.scalar(select(Panel))
        ocr = db.scalar(select(OCRResult))
        chapter_id = page.chapter_id
        evidence = dict(
            page_id=str(page.id),
            page_number=1,
            panel_id=str(panel.id),
            ocr_result_id=str(ocr.id),
            quote="Synthetic",
            reason="Synthetic fixture",
        )

    class FakeLLMProvider:
        name = "test-only"
        model = "fixture"

        def generate_structured(self, context):
            assert evidence["page_id"] in context
            assert evidence["panel_id"] in context
            assert evidence["ocr_result_id"] in context
            return LLMResponse(
                StoryStructuredOutput.model_validate(
                    {
                        "characters": [
                            {
                                "temp_id": "c1",
                                "name": "Synthetic",
                                "importance": 0.7,
                                "confidence": 0.9,
                                "evidence": [evidence],
                            }
                        ],
                        "scenes": [
                            {
                                "temp_id": "s1",
                                "title": "Introduction",
                                "summary": "A name appears",
                                "start_page": 1,
                                "end_page": 1,
                                "importance": 0.5,
                                "confidence": 0.8,
                                "evidence": [evidence],
                                "events": [
                                    {
                                        "temp_id": "e1",
                                        "description": "A name is shown",
                                        "importance": 0.5,
                                        "confidence": 0.8,
                                        "evidence": [evidence],
                                        "character_refs": ["c1"],
                                    }
                                ],
                            }
                        ],
                        "summary": "A synthetic introduction.",
                    }
                ),
                20,
                10,
            )

    monkeypatch.setattr(story_service, "configured_provider", FakeLLMProvider)
    route = f"/api/v1/chapters/{chapter_id}"
    result = client.post(route + "/story/analyze", headers=csrf(client))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed", result.text
    characters = client.get(route + "/characters").json()
    assert characters[0]["evidence"] == [evidence]
    scene = client.get(route + "/scenes").json()[0]
    event = client.get(f"/api/v1/scenes/{scene['id']}/events").json()[0]
    assert event["page_id"] == evidence["page_id"]
    assert event["panel_id"] == evidence["panel_id"]
    assert event["evidence"] == [evidence]
    LocalJobQueue().submit("story:" + result.json()["id"])
    assert client.get(route + "/characters").json() == characters
