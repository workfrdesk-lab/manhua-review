import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.analysis import router as analysis_router
from app.auth import router as auth_router
from app.config import get_settings
from app.db import build_session_factory
from app.errors import error_response, install_error_handlers
from app.health import SystemHealth, collect_health
from app.ingestion import router as ingestion_router
from app.logging_config import configure_logging
from app.projects import router as projects_router
from app.story import router as story_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.health_lock = asyncio.Lock()
    app.state.health_snapshot = None
    app.state.health_checked_at = 0.0
    engine, app.state.db_session_factory = build_session_factory(settings)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Recap Studio API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(ingestion_router)
app.include_router(analysis_router)
app.include_router(story_router)
install_error_handlers(app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            "request_failed",
            extra={"request_id": request_id, "error": type(exc).__name__},
        )
        response = error_response(500, "INTERNAL_ERROR", "An unexpected error occurred")
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "request_completed",
        extra={
            "request_id": request_id,
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - start) * 1000),
        },
    )
    return response


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


async def snapshot(request: Request) -> SystemHealth:
    # Bound expensive dependency probes even when many dashboards poll concurrently.
    async with request.app.state.health_lock:
        if time.monotonic() - request.app.state.health_checked_at >= 10:
            request.app.state.health_snapshot = await collect_health(get_settings())
            request.app.state.health_checked_at = time.monotonic()
        return request.app.state.health_snapshot


@app.get("/health/ready", response_model=SystemHealth)
async def ready(request: Request, response: Response) -> SystemHealth:
    result = await snapshot(request)
    if result.status != "ready":
        response.status_code = 503
    return result


@app.get("/api/v1/system/status", response_model=SystemHealth)
async def system_status(request: Request) -> SystemHealth:
    return await snapshot(request)
