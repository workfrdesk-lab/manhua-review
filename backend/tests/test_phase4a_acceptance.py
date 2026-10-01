"""A12 contract: synthetic sources enter through ingestion, never seeded Story rows."""

import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_auth import CREDENTIALS
from test_ingestion_local import csrf, setup_chapter

from app import analysis_service, story, story_service
from app.analysis_providers import DetectedPanel, OCRData
from app.jobs import LocalJobQueue
from app.models import (
    CharacterAlias,
    OCRResult,
    Page,
    Panel,
    StoryAnalysisJob,
    StoryAudit,
    StoryVersion,
    VisualAnalysis,
)
from app.storage import get_storage
from app.story_providers import LLMResponse
from app.story_schemas import StoryStructuredOutput
from app.story_validation import validate_story


class SyntheticPageProvider:
    """Deterministic test-only panel, OCR and visual providers."""

    def detect(self, image):
        return [
            DetectedPanel(0, 0, image.width, image.height // 2, 0.99),
            DetectedPanel(0, image.height // 2, image.width, image.height // 2, 0.99),
        ]

    def extract(self, image):
        return OCRData(
            [{"text": "Jin Mira Tao masked stranger", "confidence": 0.99, "box": [1, 1, 60, 12]}],
            "en",
            {"synthetic": True},
        )

    def analyze(self, image):
        return {"synthetic": True, "description": "Four travelers at a gate"}


class SyntheticStoryProvider:
    name = "test-only"
    model = "phase4a-contract"

    def __init__(self):
        self.revision = 0
        self.chunks = []

    def generate_structured(self, context):
        sections = re.split(r"(?m)^Page (\d+)\s*\n", context)[1:]
        sources = {}
        for number, section in zip(sections[::2], sections[1::2], strict=True):
            page = re.search(r"Page ID: ([\w-]+)", section)[1]
            panels = re.findall(r"Panel ID: ([\w-]+)\nOCR result ID: ([\w-]+)", section)
            sources[int(number)] = [
                {
                    "page_id": page,
                    "page_number": int(number),
                    "panel_id": panel,
                    "ocr_result_id": ocr,
                    "quote": "Jin",
                    "reason": "Synthetic source",
                }
                for panel, ocr in panels
            ]
        self.chunks.append(sorted(sources))
        # Page 8 is the real overlap: all identities have shared source support there.
        anchor = sources[8][0]
        characters = [
            {
                "temp_id": name,
                "name": name,
                "description": f"Traveler revision {self.revision}",
                "importance": 0.8,
                "confidence": 0.3 if name == "Masked stranger" else 0.95,
                "status": "confirmed",
                "evidence": [anchor],
                "aliases": [
                    {
                        "temp_id": "alias-jin",
                        "alias": "Gatekeeper",
                        "confidence": 0.9,
                        "evidence": [anchor],
                    }
                ]
                if name == "Jin"
                else [],
            }
            for name in ("Jin", "Mira", "Tao", "Masked stranger")
        ]
        scenes = []
        for number, evidence in sources.items():
            scenes.append(
                {
                    "temp_id": f"scene-{number}",
                    "title": f"Gate {number}",
                    "summary": f"Travelers on page {number}",
                    "start_page": number,
                    "end_page": number,
                    "importance": 0.8,
                    "confidence": 0.95,
                    "status": "confirmed",
                    "evidence": evidence,
                    "character_refs": ["Jin", "Mira"],
                    "events": [
                        {
                            "temp_id": f"event-{number}-{index}",
                            "description": (
                                f"Travelers move at {number}.{index} revision {self.revision}."
                            ),
                            "importance": 0.8,
                            "confidence": 0.95,
                            "status": "confirmed",
                            "evidence": [ref],
                            "character_refs": ["Jin", "Mira"],
                        }
                        for index, ref in reversed(list(enumerate(evidence, 1)))
                    ],
                }
            )
        return LLMResponse(
            StoryStructuredOutput.model_validate(
                {
                    "characters": characters,
                    "scenes": list(reversed(scenes)),
                    "relationships": [
                        {
                            "temp_id": "allies",
                            "source": "Jin",
                            "target": "Mira",
                            "source_ref": "Jin",
                            "target_ref": "Mira",
                            "relationship_type": "ally",
                            "confidence": 0.9,
                            "status": "confirmed",
                            "evidence": [anchor],
                        }
                    ],
                    "summary": "UNTRUSTED PROVIDER SUMMARY MUST NOT BE USED",
                }
            ),
            100,
            100,
        )


def test_phase4a_full_synthetic_lifecycle(client, database, monkeypatch):
    route = setup_chapter(client)
    chapter_id = UUID(route.rsplit("/", 1)[1])
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        for number in range(1, 11):
            image_bytes = io.BytesIO()
            Image.new("RGB", (100, 160), (number * 20, 80, 120)).save(image_bytes, "PNG")
            archive.writestr(f"{number:02}.png", image_bytes.getvalue())
    response = client.post(
        route + "/upload",
        headers=csrf(client),
        files={"file": ("synthetic.zip", archive_bytes.getvalue(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    pages = client.get(route + "/pages").json()
    assert len(pages) == 10
    page_provider = SyntheticPageProvider()
    monkeypatch.setattr(analysis_service, "providers", lambda: (page_provider,) * 3)
    for page in pages:
        response = client.post(f"/api/v1/pages/{page['id']}/analyze", headers=csrf(client))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed", response.text
    provider = SyntheticStoryProvider()
    monkeypatch.setattr(story_service, "configured_provider", lambda: provider)

    # Requests compete for the database-backed chapter job, not a Python lock.
    # Dispatch is deferred so both requests observe the same pending job.
    class DeferredQueue:
        def submit(self, job_id):
            return None

    monkeypatch.setattr(story, "get_job_queue", DeferredQueue)
    barrier = Barrier(2)
    headers = csrf(client)

    def request_analysis():
        barrier.wait(timeout=10)
        return client.post(route + "/story/analyze", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_analysis) for _ in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with Session(database[0]) as db:
        assert len(db.scalars(select(StoryAnalysisJob)).all()) == 1
    LocalJobQueue().submit("story:" + responses[0].json()["id"])
    monkeypatch.setattr(story, "get_job_queue", LocalJobQueue)
    initial = client.get(route + "/story/status").json()
    assert initial["status"] == "completed", initial
    assert initial["chunks"] == 2
    assert provider.chunks == [list(range(1, 9)), [8, 9, 10]]

    def assert_graph():
        characters = client.get(route + "/characters").json()
        assert len(characters) == 4, characters
        assert len({item["name"] for item in characters}) == 4
        assert (
            next(c for c in characters if c["name"] == "Masked stranger")["status"]
            == "needs_review"
        )
        scenes = client.get(route + "/scenes").json()
        assert len(scenes) == 10
        events = [
            event
            for scene in scenes
            for event in client.get(f"/api/v1/scenes/{scene['id']}/events").json()
        ]
        assert len(events) == 20
        relationships = client.get(route + "/relationships").json()
        edges = [
            (r["source_character_id"], r["target_character_id"], r["relationship_type"])
            for r in relationships
        ]
        assert len(edges) == len(set(edges)) == 1
        assert all(source != target for source, target, _ in edges)
        summary = client.get(route + "/story/summary").json()
        assert summary["summary"] == " ".join(event["description"] for event in events)
        assert [claim["description"] for claim in summary["claims"]] == [
            event["description"] for event in events
        ]
        with Session(database[0]) as db:
            source_pages = db.scalars(select(Page).where(Page.chapter_id == chapter_id)).all()
            source_panels = db.scalars(select(Panel)).all()
            source_ocr = db.scalars(select(OCRResult)).all()
            assert len(source_panels) == len(source_ocr) == 20
            assert len(db.scalars(select(VisualAnalysis)).all()) == 20
            aliases = db.scalars(select(CharacterAlias)).all()
            assert len(aliases) == 1
            panel_map = {str(p.id): p for p in source_panels}
            page_map = {str(p.id): p for p in source_pages}
            ocr_map = {str(o.id): o for o in source_ocr}
            order = [
                (page_map[e["page_id"]].page_number, panel_map[e["panel_id"]].reading_order)
                for e in events
            ]
            assert order == sorted(order)
            for claim in [
                *characters,
                *scenes,
                *events,
                *relationships,
                *summary["claims"],
                *[{"evidence": a.evidence} for a in aliases],
            ]:
                assert claim["evidence"]
                for ref in claim["evidence"]:
                    assert ref["page_id"] in page_map
                    assert str(panel_map[ref["panel_id"]].page_id) == ref["page_id"]
                    assert str(ocr_map[ref["ocr_result_id"]].panel_id) == ref["panel_id"]
                    assert ref["quote"] in ocr_map[ref["ocr_result_id"]].text
            for version in db.scalars(select(StoryVersion)):
                validate_story(
                    StoryStructuredOutput.model_validate(version.data),
                    source_pages,
                    source_panels,
                    source_ocr,
                )
        return characters

    characters = assert_graph()
    jin = next(c for c in characters if c["name"] == "Jin")
    response = client.patch(
        f"/api/v1/characters/{jin['id']}",
        headers=csrf(client),
        json={"display_name": "Human-approved Jin", "status": "confirmed"},
    )
    assert response.status_code == 200
    assert client.post(route + "/story/analyze", headers=csrf(client)).json()["id"] == initial["id"]
    assert len(client.get(route + "/story/versions").json()) == 1
    provider.revision = 1
    with Session(database[0]) as db:
        page = db.get(Page, UUID(pages[0]["id"]))
        replacement = io.BytesIO()
        Image.new("RGB", (page.width, page.height), "teal").save(replacement, "JPEG")
        get_storage().put(page.storage_key, replacement.getvalue(), "image/jpeg")
    response = client.post(route + "/story/analyze", headers=csrf(client))
    assert response.status_code == 200
    changed = response.json()
    assert changed["status"] == "completed", changed
    assert changed["source_fingerprint"] != initial["source_fingerprint"]
    assert len(client.get(route + "/story/versions").json()) == 2
    updated = next(c for c in client.get(route + "/characters").json() if c["id"] == jin["id"])
    assert updated["display_name"] == "Human-approved Jin"
    assert updated["description"] == "Traveler revision 0"
    assert any(
        conflict.get("field") == "description"
        and conflict["current"] == "Traveler revision 0"
        and conflict["proposed"] == "Traveler revision 1"
        for conflict in updated["review_conflicts"]
    )
    assert updated["status"] == "confirmed"
    assert any(c["field"] == "display_name" for c in updated["review_conflicts"])
    with Session(database[0]) as db:
        assert db.scalar(select(StoryAudit).where(StoryAudit.action == "ai_conflict"))
    assert_graph()
    snapshot = client.get(route + "/characters").json()
    assert client.post(route + "/story/analyze", headers=csrf(client)).json()["id"] == changed["id"]
    LocalJobQueue().submit("story:" + changed["id"])
    assert client.get(route + "/characters").json() == snapshot
    assert len(client.get(route + "/story/versions").json()) == 2
    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    assert (
        client.post(
            "/api/v1/auth/register", json={**CREDENTIALS, "email": "intruder@example.com"}
        ).status_code
        == 201
    )
    assert client.get(route + "/characters").status_code == 404
    assert client.post(route + "/story/analyze", headers=csrf(client)).status_code == 404
