import threading
import time

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings

# INCR and expiry are atomic, including across multiple API processes.
WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""

LOCAL_WINDOWS: dict[str, tuple[int, float]] = {}
LOCAL_LOCK = threading.Lock()


async def enforce_auth_limit(request: Request) -> None:
    settings = get_settings()
    # Do not trust client-supplied forwarding headers. Behind Next.js this is
    # intentionally a conservative shared limit for that proxy's connections.
    peer = request.client.host if request.client else "unknown"
    key = f"recap:auth-limit:{request.url.path}:{peer}"
    if settings.local_development and settings.app_env != "production":
        with LOCAL_LOCK:
            now = time.monotonic()
            for expired in [k for k, (_, expiry) in LOCAL_WINDOWS.items() if expiry <= now]:
                del LOCAL_WINDOWS[expired]
            count, expiry = LOCAL_WINDOWS.get(key, (0, now + settings.auth_rate_window_seconds))
            LOCAL_WINDOWS[key] = (count + 1, expiry)
            if count >= settings.auth_rate_attempts:
                raise HTTPException(
                    429,
                    "Too many authentication attempts",
                    headers={"Retry-After": str(max(1, int(expiry - now)))},
                )
        return
    try:
        async with Redis.from_url(
            settings.redis_url.get_secret_value(), socket_timeout=2, socket_connect_timeout=2
        ) as redis:
            count, ttl = await redis.eval(WINDOW_SCRIPT, 1, key, settings.auth_rate_window_seconds)
    except RedisError:
        raise HTTPException(503, "Authentication rate limiter unavailable") from None
    if count > settings.auth_rate_attempts:
        raise HTTPException(
            429, "Too many authentication attempts", headers={"Retry-After": str(max(ttl, 1))}
        )
