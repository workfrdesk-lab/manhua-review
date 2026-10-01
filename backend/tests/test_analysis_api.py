import pytest
from test_auth import CREDENTIALS
from test_ingestion_local import csrf, make_png, setup_chapter

from app import analysis_service
from app.analysis_providers import BasicVisualAnalyzer, OCRData, OpenCVPanelDetector
from app.jobs import LocalJobQueue


class FixtureOCR:
    """Explicit test double, never used by production provider selection."""

    def __init__(self, confidence):
        self.confidence = confidence

    def extract(self, image):
        regions = (
            []
            if self.confidence is None
            else [
                {"text": "Synthetic", "confidence": self.confidence, "box": [1, 1, 20, 8]},
                {"text": "fixture", "confidence": self.confidence, "box": [1, 10, 20, 8]},
            ]
        )
        return OCRData(regions, "en", {"fixture": True})


def upload_page(client):
    route = setup_chapter(client)
    result = client.post(
        route + "/upload",
        files={"file": ("test.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert result.status_code == 200
    return client.get(route + "/pages").json()[0]["id"]


@pytest.mark.parametrize("confidence", [0.9, 0.3, None])
def test_analysis_persistence_idempotency_reanalysis_ownership(client, monkeypatch, confidence):
    monkeypatch.setattr(
        analysis_service,
        "providers",
        lambda: (OpenCVPanelDetector(), FixtureOCR(confidence), BasicVisualAnalyzer()),
    )
    page_id = upload_page(client)
    route = f"/api/v1/pages/{page_id}"
    assert client.post(route + "/analyze").status_code == 403
    result = client.post(
        route + "/analyze", headers=csrf(client), json={"reading_direction": "rtl"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed", result.text
    panels = client.get(route + "/panels").json()
    assert len(panels) == 1
    panel_route = f"/api/v1/panels/{panels[0]['id']}"
    text = client.get(panel_route + "/ocr").json()[0]
    assert text["status"] == ("needs_review" if confidence == 0.3 else "completed")
    assert text["text"] == ("" if confidence is None else "Synthetic\nfixture")
    assert text["data_json"]["raw"]["fixture"]
    assert client.get(panel_route + "/analysis").json()["data_json"]["heuristic"]
    assert client.post(route + "/analyze", headers=csrf(client)).json()["id"] == result.json()["id"]
    LocalJobQueue().submit("analysis:" + result.json()["id"])
    assert client.get(route + "/panels").json() == panels
    rerun = client.post(route + "/reanalyze", headers=csrf(client))
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["status"] == "completed", rerun.text
    assert len(client.get(route + "/panels").json()) == 1
    assert client.get(panel_route).status_code == 404
    panel_route = "/api/v1/panels/" + client.get(route + "/panels").json()[0]["id"]
    client.post("/api/v1/auth/logout", headers=csrf(client))
    client.post("/api/v1/auth/register", json={**CREDENTIALS, "email": "other@example.com"})
    for path in [
        route + "/analysis-status",
        route + "/panels",
        route + "/image",
        panel_route,
        panel_route + "/ocr",
        panel_route + "/analysis",
    ]:
        assert client.get(path).status_code == 404
    for path in [route + "/analyze", route + "/reanalyze"]:
        assert client.post(path, headers=csrf(client)).status_code == 404


def test_stage_failure_retains_other_results(client, monkeypatch):
    class Broken:
        def extract(self, image):
            raise RuntimeError("test failure")

        def detect(self, image):
            raise RuntimeError("test failure")

    monkeypatch.setattr(
        analysis_service, "providers", lambda: (Broken(), Broken(), BasicVisualAnalyzer())
    )
    route = f"/api/v1/pages/{upload_page(client)}"
    result = client.post(route + "/analyze", headers=csrf(client)).json()
    assert result["panel_detection_status"] == "completed"
    assert result["ocr_status"] == "failed"
    assert result["visual_analysis_status"] == "completed"
    panel = client.get(route + "/panels").json()[0]
    assert panel["status"] == "FULL_PAGE"
    assert client.get(f"/api/v1/panels/{panel['id']}/ocr").json()[0]["status"] == "failed"
