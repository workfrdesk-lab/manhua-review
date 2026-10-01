from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import install_error_handlers


def test_unknown_api_route_is_json():
    app = FastAPI()
    install_error_handlers(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/missing")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"]["code"] == "NOT_FOUND"
