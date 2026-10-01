import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import get_settings
from app.main import app
from app.rate_limit import enforce_auth_limit

ORIGIN = {"origin": "http://localhost:3000"}


@pytest.fixture
def database(monkeypatch, tmp_path):
    # Opt-in dedicated disposable database only: roundtrip tests drop tables.
    url = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    if not url.startswith("sqlite:") and os.environ.get("ALLOW_TEST_DB_RESET") != "1":
        pytest.fail("Set ALLOW_TEST_DB_RESET=1 only for a disposable TEST_DATABASE_URL")
    for key, value in {
        "APP_ENV": "test",
        "LOCAL_STORAGE_PATH": str(tmp_path / "storage"),
        "STORAGE_BACKEND": "local",
        "PROCESSING_MODE": "local",
        "LOCAL_DEVELOPMENT": "false",
        "DATABASE_URL": url,
        "REDIS_URL": "redis://localhost:6379/15",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "test",
        "S3_SECRET_KEY": "secret",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    yield engine, config
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client(database):
    async def bypass_external_limiter():
        return None

    app.dependency_overrides[enforce_auth_limit] = bypass_external_limiter
    try:
        with TestClient(app, headers=ORIGIN) as client:
            yield client
    finally:
        app.dependency_overrides.pop(enforce_auth_limit, None)
