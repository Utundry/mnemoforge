"""
MCP Session Store — persists session context and metadata across restarts.

Two backends:
  MemorySessionStore  — in-process dict (current behaviour, zero deps)
  RedisSessionStore   — Redis HASH + EXPIRE (survives restarts, multi-instance safe)

Selected automatically:
  REDIS_URL set  → Redis backend
  otherwise      → Memory backend

Session lifecycle:
  init_session(sid)           — create meta (created_at, last_activity)
  set_context(sid, ctx)       — store agent context on MCP initialize
  get_context(sid)            — read context (for onboarding, observation)
  patch_context(sid, patch)   — merge-update (append tool calls, set pack_id, etc.)
  touch(sid)                  — refresh last_activity + extend Redis TTL
  close_session(sid)          — delete + return context (for auto-record)
  evict_expired()             — clean up stale sessions (memory backend only;
                                Redis handles its own TTL expiry)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SSE_IDLE_TTL_S  = 4 * 60 * 60   # 4 h idle TTL
_SSE_MAX_AGE_S   = 24 * 60 * 60  # 24 h hard max age


# ── Memory backend ────────────────────────────────────────────────────────────

class MemorySessionStore:
    def __init__(self) -> None:
        self._ctx:  dict[str, dict] = {}
        self._meta: dict[str, dict] = {}

    async def init_session(self, session_id: str) -> None:
        now = time.time()
        self._meta[session_id] = {"created_at": now, "last_activity": now}

    async def set_context(self, session_id: str, ctx: dict) -> None:
        self._ctx[session_id] = ctx

    async def get_context(self, session_id: str) -> Optional[dict]:
        return self._ctx.get(session_id)

    async def patch_context(self, session_id: str, patch: dict) -> None:
        ctx = self._ctx.get(session_id)
        if ctx is None:
            return
        for key, value in patch.items():
            if isinstance(value, list) and isinstance(ctx.get(key), list):
                ctx[key].extend(value)
            else:
                ctx[key] = value

    async def touch(self, session_id: str) -> None:
        meta = self._meta.get(session_id)
        if meta is not None:
            meta["last_activity"] = time.time()

    async def close_session(self, session_id: str) -> Optional[dict]:
        self._meta.pop(session_id, None)
        return self._ctx.pop(session_id, None)

    async def evict_expired(self) -> int:
        now = time.time()
        evicted = 0
        for sid in list(self._meta):
            meta = self._meta[sid]
            created_at    = float(meta.get("created_at",    now))
            last_activity = float(meta.get("last_activity", created_at))
            if (now - created_at) > _SSE_MAX_AGE_S or (now - last_activity) > _SSE_IDLE_TTL_S:
                self._meta.pop(sid, None)
                self._ctx.pop(sid, None)
                evicted += 1
        return evicted

    async def close(self) -> None:
        pass


# ── Redis backend ─────────────────────────────────────────────────────────────

class RedisSessionStore:
    """
    Stores context and meta as JSON strings.
    Keys: mcp:ctx:{sid}  /  mcp:meta:{sid}
    TTL on ctx key: refreshed to _SSE_IDLE_TTL_S on every touch.
    TTL on meta key: fixed _SSE_MAX_AGE_S (hard cap).
    """

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis
        self._r = aioredis.from_url(redis_url, decode_responses=True)
        logger.info("RedisSessionStore initialized: %s", redis_url)

    @staticmethod
    def _ctx_key(sid: str)  -> str: return f"mcp:ctx:{sid}"
    @staticmethod
    def _meta_key(sid: str) -> str: return f"mcp:meta:{sid}"

    async def init_session(self, session_id: str) -> None:
        now = time.time()
        meta = {"created_at": now, "last_activity": now}
        try:
            await self._r.set(
                self._meta_key(session_id),
                json.dumps(meta),
                ex=_SSE_MAX_AGE_S,
            )
        except Exception as e:
            logger.warning("Redis init_session failed: %s", e)

    async def set_context(self, session_id: str, ctx: dict) -> None:
        try:
            await self._r.set(
                self._ctx_key(session_id),
                json.dumps(ctx),
                ex=_SSE_IDLE_TTL_S,
            )
        except Exception as e:
            logger.warning("Redis set_context failed: %s", e)

    async def get_context(self, session_id: str) -> Optional[dict]:
        try:
            raw = await self._r.get(self._ctx_key(session_id))
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning("Redis get_context failed: %s", e)
            return None

    async def patch_context(self, session_id: str, patch: dict) -> None:
        try:
            raw = await self._r.get(self._ctx_key(session_id))
            if raw is None:
                return
            ctx = json.loads(raw)
            for key, value in patch.items():
                if isinstance(value, list) and isinstance(ctx.get(key), list):
                    ctx[key].extend(value)
                else:
                    ctx[key] = value
            await self._r.set(self._ctx_key(session_id), json.dumps(ctx), ex=_SSE_IDLE_TTL_S)
        except Exception as e:
            logger.warning("Redis patch_context failed: %s", e)

    async def touch(self, session_id: str) -> None:
        try:
            # Extend context TTL
            await self._r.expire(self._ctx_key(session_id), _SSE_IDLE_TTL_S)
            # Update last_activity in meta (best-effort)
            raw = await self._r.get(self._meta_key(session_id))
            if raw:
                meta = json.loads(raw)
                meta["last_activity"] = time.time()
                ttl = await self._r.ttl(self._meta_key(session_id))
                await self._r.set(self._meta_key(session_id), json.dumps(meta),
                                  ex=ttl if ttl > 0 else _SSE_MAX_AGE_S)
        except Exception as e:
            logger.warning("Redis touch failed: %s", e)

    async def close_session(self, session_id: str) -> Optional[dict]:
        try:
            raw = await self._r.get(self._ctx_key(session_id))
            ctx = json.loads(raw) if raw else None
            await self._r.delete(self._ctx_key(session_id), self._meta_key(session_id))
            return ctx
        except Exception as e:
            logger.warning("Redis close_session failed: %s", e)
            return None

    async def evict_expired(self) -> int:
        # Redis TTL handles expiry automatically — nothing to do here
        return 0

    async def close(self) -> None:
        try:
            await self._r.aclose()
        except Exception:
            pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[MemorySessionStore | RedisSessionStore] = None


def get_session_store() -> MemorySessionStore | RedisSessionStore:
    global _store
    if _store is None:
        _store = _build_store()
    return _store


def _build_store() -> MemorySessionStore | RedisSessionStore:
    from app.config import settings
    if settings.redis_url:
        try:
            store = RedisSessionStore(settings.redis_url)
            logger.info("MCP session store: Redis backend")
            return store
        except Exception as e:
            logger.warning("Failed to init Redis session store: %s — falling back to memory", e)
    logger.info("MCP session store: memory backend")
    return MemorySessionStore()


async def close_session_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
