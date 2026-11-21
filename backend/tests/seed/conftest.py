"""A second database seeded with the FULL Vistula Capital profile (the shared test database uses `core`)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def full_pool(session_env) -> AsyncIterator[asyncpg.Pool]:
    """Fresh database, migrated and seeded with SEED_PROFILE=full and embeddings skipped."""
    from app.core.db import create_pool, run_migrations
    from seed.seed import run_seed

    base = os.environ["DATABASE_URL"]
    name = f"test_full_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(base)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    patch = pytest.MonkeyPatch()
    patch.setenv("SEED_PROFILE", "full")
    patch.setenv("SEED_SKIP_EMBEDDINGS", "1")
    pool = await create_pool(urlunsplit(urlsplit(base)._replace(path=f"/{name}")))
    try:
        await run_migrations(pool)
        await run_seed(pool)
        yield pool
    finally:
        await pool.close()
        patch.undo()
        admin = await asyncpg.connect(base)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()
