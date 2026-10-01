from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from test_ingestion_local import make_png
from test_projects import register, write_headers

from app.models import StoryVersion
from app.script_providers import DeterministicScriptProvider
from app.script_schemas import ScriptProfile
from app.script_validation import ScriptValidationError, validate_script
from app.story_schemas import StoryStructuredOutput


def source():
    page = SimpleNamespace(id=uuid4(), page_number=1)
    evidence = {"page_id": page.id, "page_number": 1, "reason": "Visible action"}
    story = StoryStructuredOutput.model_validate(
        {
            "scenes": [
                {
                    "temp_id": "scene",
                    "title": "Arrival",
                    "summary": "A traveler arrives",
                    "start_page": 1,
                    "end_page": 1,
                    "importance": 0.8,
                    "confidence": 0.8,
                    "evidence": [evidence],
                    "events": [
                        {
                            "temp_id": "event",
                            "description": "A traveler arrives",
                            "importance": 0.8,
                            "confidence": 0.8,
                            "evidence": [evidence],
                        }
                    ],
                }
            ]
        }
    )
    return story, [page], [], []


def test_script_grounding_and_snapshot_preservation():
    story, pages, panels, ocr = source()
    before = story.model_dump()
    profile = ScriptProfile()
    result = DeterministicScriptProvider().generate_script(story, profile)
    assert validate_script(result, story, pages, panels, ocr, profile) == []
    assert story.model_dump() == before
    result.segments[0].evidence[0].event_ref = "invented"
    with pytest.raises(ScriptValidationError):
        validate_script(result, story, pages, panels, ocr, profile)


@pytest.mark.parametrize("change", ["page", "confirmed", "confidence", "rejected"])
def test_script_rejects_invalid_sources(change):
    story, pages, panels, ocr = source()
    profile = ScriptProfile()
    result = DeterministicScriptProvider().generate_script(story, profile)
    if change == "page":
        result.segments[0].evidence[0].page_id = uuid4()
    elif change == "confirmed":
        result.status = "confirmed"
    elif change == "confidence":
        result.segments[0].confidence = 1
    else:
        story.scenes[0].events[0].status = "rejected"
    with pytest.raises(ScriptValidationError):
        validate_script(result, story, pages, panels, ocr, profile)


def test_script_api_create_review_and_ownership(client, database, monkeypatch):
    register(client, "script@example.com")
    headers = write_headers(client)
    project = client.post("/api/v1/projects", json={"name": "Script"}, headers=headers).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Chapter"}, headers=headers
    ).json()
    story, pages, panels, ocr = source()
    upload = client.post(
        f"/api/v1/chapters/{chapter['id']}/upload",
        files={"file": ("page.png", make_png(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 200
    actual_page = client.get(f"/api/v1/chapters/{chapter['id']}/pages").json()[0]
    for evidence in [story.scenes[0].evidence[0], story.scenes[0].events[0].evidence[0]]:
        evidence.page_id = UUID(actual_page["id"])
    with Session(database[0]) as db:
        version = StoryVersion(
            chapter_id=UUID(chapter["id"]),
            fingerprint="s" * 64,
            data=story.model_dump(mode="json"),
            decisions=[],
        )
        db.add(version)
        db.commit()
        version_id = str(version.id)

    monkeypatch.setattr("app.script.configured_script_provider", DeterministicScriptProvider)
    monkeypatch.setattr(
        "app.script_service.configured_script_provider", DeterministicScriptProvider
    )
    route = f"/api/v1/chapters/{chapter['id']}/scripts"
    response = client.post(route, json={"story_version_id": version_id}, headers=headers)
    assert response.status_code == 201, response.text
    job = response.json()["job"]
    assert job["status"] == "completed", job
    script = client.get(f"/api/v1/scripts/{job['script_version_id']}").json()
    assert script["status"] == "needs_review"
    assert (
        client.post(route, json={"story_version_id": version_id}, headers=headers).json()["job"][
            "id"
        ]
        == job["id"]
    )
    detail = f"/api/v1/scripts/{script['id']}"
    assert len(client.get(detail).json()["segments"]) == 1
    segment = script["segments"][0]
    edited = client.patch(
        f"/api/v1/script-segments/{segment['id']}",
        json={"narration_text": "The traveler arrives at the scene."},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert client.get(detail).json()["data"]["hook"] == "The traveler arrives at the scene."
    assert (
        client.patch(detail + "/review", json={"status": "confirmed"}, headers=headers).status_code
        == 200
    )
    confirmed = client.get(detail).json()
    with Session(database[0]) as db:
        newer = StoryVersion(
            chapter_id=UUID(chapter["id"]),
            fingerprint="n" * 64,
            data=story.model_dump(mode="json"),
            decisions=[],
        )
        db.add(newer)
        db.commit()
        newer_id = str(newer.id)

    class FailedProvider(DeterministicScriptProvider):
        def generate_script(self, story, profile):
            raise RuntimeError("secret-bearing failure must not be stored")

    monkeypatch.setattr("app.script_service.configured_script_provider", FailedProvider)
    failed = client.post(route, json={"story_version_id": newer_id}, headers=headers).json()["job"]
    assert failed["status"] == "failed"
    assert "secret-bearing" not in failed["error"]
    monkeypatch.setattr(
        "app.script_service.configured_script_provider", DeterministicScriptProvider
    )
    retry = client.post(f"/api/v1/script-jobs/{failed['id']}/retry", headers=headers)
    assert retry.status_code == 202, retry.text
    assert retry.json()["status"] == "completed"
    assert retry.json()["story_version_id"] == newer_id
    assert retry.json()["profile_fingerprint"] == failed["profile_fingerprint"]
    assert client.get(detail).json() == confirmed
    assert len(client.get(route).json()) == 2
    assert client.get(f"/api/v1/script-jobs/{failed['id']}/status").json()["status"] == "failed"
    client.post("/api/v1/auth/logout", headers=headers)
    register(client, "outsider-script@example.com")
    assert client.get(detail).status_code == 404
    assert client.get(route).status_code == 404
