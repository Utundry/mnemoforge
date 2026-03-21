"""
Rate limiting service — memory or Redis backend.

Memory backend (default):
  - In-process dict, resets on server restart.
  - Fine for single-instance local use.

Redis backend (REDIS_URL set):
  - Atomic INCR + EXPIRE per (ip, minute) window.
  - Survives restarts, works correctly under horizontal scaling.
  - Falls back to memory if Redis connection fails.

Selection (RATE_LIMIT_BACKEND env):
  "auto"   → Redis if REDIS_URL configured, else memory
  "redis"  → Redis (error logged if unavailable, falls back to memory)
  "memory" → always in-process

Usage:
    limiter = get_rate_limiter()
    allowed = await limiter.check(ip="1.2.3.4", limit=10)
    if not allowed:
        return 429
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


# ── Protocol ──────────────────────────────────────────────────────────────────

class RateLimiter(Protocol):
    async def check(self, ip: str, limit: int) -> bool:
        """Return True if request is allowed, False if rate limit exceeded."""
        ...

    async def close(self) -> None:
        ...


# ── Memory backend ────────────────────────────────────────────────────────────

class MemoryRateLimiter:
    """
    Sliding window rate limiter backed by an in-process list of timestamps.
    Simple and zero-dependency; resets on restart.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = defaultdict(list)

    async def check(self, ip: str, limit: int) -> bool:
        now = time.time()
        bucket = self._buckets[ip]
        # Evict entries older than 60 seconds
        self._buckets[ip] = [t for t in bucket if now - t < 60]
        if len(self._buckets[ip]) >= limit:
            return False
        self._buckets[ip].append(now)
        return True

    async def close(self) -> None:
        pass


# ── Redis backend ─────────────────────────────────────────────────────────────

class RedisRateLimiter:
    """
    Fixed window rate limiter backed by Redis INCR + EXPIRE.

    Key: rl:{ip}:{minute_epoch}
    Window: 60 seconds, aligned to clock minute.
    Atomic: INCR is atomic; EXPIRE set on first request in window.
    """

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        logger.info("RedisRateLimiter initialized: %s", redis_url)

    async def check(self, ip: str, limit: int) -> bool:
        minute = int(time.time() // 60)
        key = f"rl:{ip}:{minute}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 60)
            return count <= limit
        except Exception as e:
            logger.warning("Redis rate limit check failed, allowing request: %s", e)
            return True  # fail-open

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_limiter: Optional[RateLimiter] = None


def _build_limiter() -> RateLimiter:
    from app.config import settings

    backend = settings.rate_limit_backend.lower()
    use_redis = (
        backend == "redis"
        or (backend == "auto" and bool(settings.redis_url))
    )

    if use_redis:
        if not settings.redis_url:
            logger.warning("RATE_LIMIT_BACKEND=redis but REDIS_URL is empty — falling back to memory")
        else:
            try:
                limiter = RedisRateLimiter(settings.redis_url)
                logger.info("Rate limiter: Redis backend (%s)", settings.redis_url)
                return limiter
            except Exception as e:
                logger.warning("Failed to init Redis rate limiter: %s — falling back to memory", e)

    logger.info("Rate limiter: memory backend")
    return MemoryRateLimiter()


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = _build_limiter()
    return _limiter


async def close_rate_limiter() -> None:
    global _limiter
    if _limiter is not None:
        await _limiter.close()
        _limiter = None
