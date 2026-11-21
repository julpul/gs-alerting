"""asyncpg pool (orjson jsonb codec, pgvector) and the migration runner."""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg
import orjson
from pgvector.asyncpg import register_vector

from app.config import get_settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_LOCK_KEY = 7_351_001
_EXTENSION_LOCK_KEY = 7_351_002


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=lambda v: orjson.dumps(v).decode(),
            decoder=orjson.loads,
            schema="pg_catalog",
            format="text",
        )
    await register_vector(conn)


async def _ensure_vector_extension(dsn: str) -> None:
    """Make sure `vector` exists before the pool registers its codec (safe under concurrent startup)."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _EXTENSION_LOCK_KEY)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("SELECT pg_advisory_unlock($1)", _EXTENSION_LOCK_KEY)
    finally:
        await conn.close()


async def create_pool(dsn: str | None = None) -> asyncpg.Pool:
    dsn = dsn or get_settings().database_url
    await _ensure_vector_extension(dsn)
    return await asyncpg.create_pool(dsn, min_size=2, max_size=20, init=_init_connection)


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    """Apply `migrations/*.sql` in filename order. Safe for N replicas starting together (advisory lock)."""
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
                applied.append(path.name)
                log.info("applied migration %s", path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
    return applied
