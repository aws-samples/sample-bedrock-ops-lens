"""Async Postgres connection pool. All endpoints acquire from this pool —
never instantiate connections directly."""
from __future__ import annotations

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=30,
        )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() in lifespan")
    return _pool


async def fetch(query: str, *args) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    async with pool().acquire() as conn:
        return await conn.fetchval(query, *args)


def rows_to_dicts(rows: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rows]
