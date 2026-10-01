import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from typing import Literal

import boto3
import psycopg
from botocore.config import Config
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings

logger = logging.getLogger(__name__)


class ServiceHealth(BaseModel):
    name: str
    status: Literal["up", "down"]
    latency_ms: int


class SystemHealth(BaseModel):
    status: Literal["ready", "degraded"]
    services: list[ServiceHealth]


async def check_database(settings: Settings) -> None:
    async with await psycopg.AsyncConnection.connect(
        settings.database_url.get_secret_value(), connect_timeout=3
    ) as connection:
        await connection.execute("SELECT 1")


async def check_redis(settings: Settings) -> None:
    async with Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=2,
        socket_timeout=2,
    ) as client:
        await client.ping()


async def check_storage(settings: Settings) -> None:
    def probe() -> None:
        with closing(
            boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key.get_secret_value(),
                aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
                region_name=settings.s3_region,
                config=Config(
                    connect_timeout=2,
                    read_timeout=2,
                    retries={"max_attempts": 0},
                    s3={"addressing_style": "path"},
                ),
            )
        ) as client:
            client.head_bucket(Bucket=settings.s3_bucket)

    await asyncio.to_thread(probe)


async def check_worker(settings: Settings) -> None:
    # Celery inspection is synchronous: never block the ASGI event loop.
    from app.worker import celery_app

    responses = await asyncio.to_thread(celery_app.control.ping, timeout=2)
    if not any(item.get("ok") == "pong" for reply in responses for item in reply.values()):
        raise ConnectionError("No worker replied")


async def collect_health(settings: Settings) -> SystemHealth:
    async def probe(name: str, check: Callable[[Settings], Awaitable[None]]) -> ServiceHealth:
        start = time.monotonic()
        status: Literal["up", "down"] = "up"
        try:
            await asyncio.wait_for(check(settings), timeout=5)
        except Exception as exc:
            status = "down"
            # Do not log exception text: driver errors may contain credentials or endpoints.
            logger.warning(
                "dependency_unavailable",
                extra={"stage": name, "status": status, "error": type(exc).__name__},
            )
        return ServiceHealth(
            name=name, status=status, latency_ms=round((time.monotonic() - start) * 1000)
        )

    services = await asyncio.gather(
        probe("postgres", check_database),
        probe("redis", check_redis),
        probe("storage", check_storage),
        probe("worker", check_worker),
    )
    return SystemHealth(
        status="ready" if all(service.status == "up" for service in services) else "degraded",
        services=list(services),
    )
