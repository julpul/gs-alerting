import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio

from app.core.db import create_pool, run_migrations


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_dsn(session_env):
    """A brand-new empty database (dropped afterwards)."""
    base = urlsplit(os.environ["DATABASE_URL"])
    name = f"{session_env['db']}_{uuid.uuid4().hex[:4]}"
    admin = await asyncpg.connect(urlunsplit(base._replace(path="/postgres")))
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield urlunsplit(base._replace(path=f"/{name}"))
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_migrations_are_idempotent(fresh_dsn):
    pool = await create_pool(fresh_dsn)
    try:
        first = await run_migrations(pool)
        assert first and first[0] == "001_init.sql"
        assert await run_migrations(pool) == []
        tables = {
            r["tablename"]
            for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert {
            "users",
            "alerts",
            "deliveries",
            "outbox",
            "knowledge_chunks",
            "audit_log",
            "schema_migrations",
        } <= tables
    finally:
        await pool.close()


async def test_concurrent_migration_runs_do_not_conflict(fresh_dsn):
    pools = [await create_pool(fresh_dsn) for _ in range(3)]
    try:
        results = await asyncio.gather(*(run_migrations(p) for p in pools))
        assert sum(1 for r in results if r) == 1, "exactly one runner applies the migrations"
    finally:
        for p in pools:
            await p.close()


async def test_jsonb_codec_and_vector(pool):
    value = await pool.fetchval("SELECT $1::jsonb", {"a": [1, {"b": None}]})
    assert value == {"a": [1, {"b": None}]}
    vec = await pool.fetchval("SELECT '[1,2,3]'::vector")
    assert vec.to_list() == [1.0, 2.0, 3.0]


async def test_schema_has_slugs_and_timezone(pool):
    cols = {
        (r["table_name"], r["column_name"])
        for r in await pool.fetch(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
        )
    }
    assert ("domains", "slug") in cols
    assert ("teams", "slug") in cols
    assert ("users", "timezone") in cols
    assert ("alerts", "short_id") in cols
