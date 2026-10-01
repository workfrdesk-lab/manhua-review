import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import health
from app.config import Settings, get_settings
from app.logging_config import JsonFormatter
from app.main import app


@pytest.fixture
def settings(monkeypatch):
    values = {
        "DATABASE_URL": "postgresql://test:secret@localhost/test",
        "REDIS_URL": "redis://localhost:6379/15",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "test",
        "S3_SECRET_KEY": "secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield Settings(_env_file=None)
    get_settings.cache_clear()


async def successful_probe(settings):
    return None


@pytest.fixture
def probes(monkeypatch):
    for name in ("check_database", "check_redis", "check_storage", "check_worker"):
        monkeypatch.setattr(health, name, successful_probe)


def test_liveness_and_request_headers(settings):
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.json() == {"status": "alive"}
    assert response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"


def test_all_dependencies_ready(settings, probes):
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert len(response.json()["services"]) == 4


def test_failure_is_visible_but_secrets_are_not(settings, probes, monkeypatch):
    async def failing_probe(settings):
        raise ConnectionError("password=SUPER_SECRET")

    monkeypatch.setattr(health, "check_storage", failing_probe)
    with TestClient(app) as client:
        response = client.get("/health/ready")
        dashboard = client.get("/api/v1/system/status")
    assert response.status_code == 503
    assert dashboard.status_code == 200
    assert dashboard.json()["status"] == "degraded"
    assert "SUPER_SECRET" not in response.text
    storage = next(s for s in response.json()["services"] if s["name"] == "storage")
    assert storage["status"] == "down"


def test_probes_are_cached(settings, probes, monkeypatch):
    calls = []

    async def counted_probe(settings):
        calls.append(1)

    monkeypatch.setattr(health, "check_database", counted_probe)
    with TestClient(app) as client:
        client.get("/api/v1/system/status")
        client.get("/api/v1/system/status")
    assert len(calls) == 1


def test_timeout_is_a_dependency_failure(settings, probes, monkeypatch):
    async def timed_out(settings):
        raise TimeoutError()

    monkeypatch.setattr(health, "check_worker", timed_out)
    result = asyncio.run(health.collect_health(settings))
    assert result.status == "degraded"


@pytest.mark.parametrize("fails", [False, True])
def test_storage_probe_closes_client_without_context_manager(settings, monkeypatch, fails):
    class FakeClient:
        def __init__(self):
            self.closed = False

        def head_bucket(self, *, Bucket):
            assert Bucket == settings.s3_bucket
            if fails:
                raise ConnectionError("unavailable")

        def close(self):
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(health.boto3, "client", lambda *args, **kwargs: client)
    if fails:
        with pytest.raises(ConnectionError):
            asyncio.run(health.check_storage(settings))
    else:
        asyncio.run(health.check_storage(settings))
    assert client.closed


def test_log_schema():
    record = logging.LogRecord("test", logging.INFO, "", 1, "job_started", (), None)
    record.job_id = "job-1"
    value = json.loads(JsonFormatter().format(record))
    assert value["job_id"] == "job-1"
    assert {"user_id", "project_id", "stage", "status", "error", "duration_ms"} <= value.keys()
