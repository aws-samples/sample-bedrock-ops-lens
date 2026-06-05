"""Cache layer with three backends — in-memory (default, single-process),
Memcached (Lambda + ElastiCache), and Redis (alternative). Same get/set
interface; backend is selected by env var:

  * MEMCACHED_HOST set → ElastiCache Memcached. cache_generation atomic
    invalidation: a single integer key is bumped by the pre-warm Lambda
    after every ingester run; cached values store the generation they
    were written under, and reads fail if the gen doesn't match. This
    is exactly the pattern from the internal Bedrock Lens (sqlite-mirror
    `cache_generation` file), ported to AWS-managed Memcached.
  * REDIS_URL set     → Redis (kept for compatibility, not used in v1).
  * neither           → in-memory dict (local dev, no infra).

Key shape: bl:<scope>:<path>?<sorted-params>. Per-user account scope
prevents cache leakage across users.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from typing import Any, Protocol

from .config import settings


class CacheBackend(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl: int) -> None: ...
    def invalidate_all(self) -> None: ...


class _InMemoryTTLCache:
    """Default backend. LRU + TTL, single process."""
    def __init__(self, max_entries: int) -> None:
        self.max = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (time.monotonic() + ttl, value)
        self._store.move_to_end(key)
        while len(self._store) > self.max:
            self._store.popitem(last=False)

    def invalidate_all(self) -> None:
        self._store.clear()


class _RedisTTLCache:
    """Redis backend. Lazy-imported so the dep isn't required for local dev."""
    def __init__(self, url: str) -> None:
        try:
            import redis  # type: ignore
        except ImportError as e:
            raise RuntimeError("REDIS_URL set but `redis` package not installed") from e
        self._r = redis.Redis.from_url(url, decode_responses=False)
        # Verify reachable on construction; failure should be loud at startup.
        self._r.ping()

    def get(self, key: str) -> Any | None:
        raw = self._r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._r.set(key, json.dumps(value, default=str), ex=ttl)

    def invalidate_all(self) -> None:
        # Redis FLUSHDB is destructive — only flush our prefix.
        for k in self._r.scan_iter(match="bl:*", count=500):
            self._r.delete(k)


class _MemcachedTTLCache:
    """Memcached backend with atomic cache-generation invalidation.

    Storage shape: each value is wrapped as
        {"v": <real value>, "g": <generation int when written>}
    Reads check that the stored generation matches the live value of
    `bedrock_lens:cache_generation`. The pre-warm Lambda bumps that
    integer after each ingester run — every cached entry is then
    logically stale even though the bytes are still in Memcached, and
    the next read returns None → triggering a re-fetch from Postgres
    that re-populates with the new generation.

    Why this beats simple TTL: TTL leaves a brief window where users
    see post-ingest data via a still-fresh pre-ingest cache entry.
    cache_generation invalidation is instant and atomic.
    """
    GEN_KEY = "bedrock_lens:cache_generation"

    def __init__(self, host: str, port: int) -> None:
        try:
            from pymemcache.client.base import Client  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "MEMCACHED_HOST set but `pymemcache` not installed; "
                "add to requirements.txt"
            ) from e
        # connect_timeout small so a pinned cache cluster doesn't hang
        # cold-start; if Memcached is briefly unreachable, treat as a
        # cache miss (we read from Postgres anyway).
        self._client = Client((host, port), connect_timeout=2, timeout=2)

    def _generation(self) -> int:
        raw = self._client.get(self.GEN_KEY)
        if raw is None:
            try:
                self._client.set(self.GEN_KEY, b"1", expire=0)
            except Exception:
                pass
            return 1
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(key)
            if raw is None:
                return None
            obj = json.loads(raw)
            if not isinstance(obj, dict) or "v" not in obj:
                return None
            if obj.get("g") != self._generation():
                return None  # stale: pre-warm bumped generation
            return obj["v"]
        except Exception:
            # Treat any cache misbehaviour as a miss — never break the request.
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            envelope = {"v": value, "g": self._generation()}
            self._client.set(
                key,
                json.dumps(envelope, default=str).encode("utf-8"),
                expire=ttl,
            )
        except Exception:
            pass  # cache write best-effort

    def invalidate_all(self) -> None:
        # Bump the generation. Atomic for all keys in a single op.
        try:
            self._client.incr(self.GEN_KEY, 1)
        except Exception:
            try:
                self._client.set(self.GEN_KEY, b"1", expire=0)
            except Exception:
                pass


_memc_host = os.environ.get("MEMCACHED_HOST", "").strip()
_memc_port = int(os.environ.get("MEMCACHED_PORT", "11211") or "11211")
_redis_url = os.environ.get("REDIS_URL", "").strip()
_backend: CacheBackend = (
    _MemcachedTTLCache(_memc_host, _memc_port) if _memc_host
    else _RedisTTLCache(_redis_url) if _redis_url
    else _InMemoryTTLCache(settings.cache_max_entries)
)


def make_key(path: str, params: dict, user_scope: str = "default") -> str:
    """Build a stable, scope-prefixed cache key. Account-scoped users never
    share a cache entry with the public/all-accounts view."""
    parts = [f"{k}={params[k]}" for k in sorted(params) if params[k] is not None]
    return f"bl:{user_scope}:{path}?{'&'.join(parts)}"


def get(key: str):
    return _backend.get(key)


def set_(key: str, value: Any, ttl: int | None = None) -> None:
    _backend.set(key, value, ttl or settings.cache_ttl_seconds)


def invalidate_all() -> None:
    _backend.invalidate_all()
