"""Seed runner: `python -m seed.seed`.

Loads every YAML in `seed/fixtures/` (keyed by file stem) and runs `seed/loaders/NN_name.py`
(`async def load(conn, fixtures)`) in filename order inside ONE transaction. Loaders must be idempotent
(deterministic uuid5 ids, `ON CONFLICT`).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any

import asyncpg
import yaml

from app.core.db import create_pool, run_migrations

log = logging.getLogger("seed")

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LOADERS_PACKAGE = "seed.loaders"


def load_fixtures(directory: Path = FIXTURES_DIR) -> dict[str, Any]:
    fixtures: dict[str, Any] = {}
    for path in sorted(directory.glob("*.yaml")):
        fixtures[path.stem] = yaml.safe_load(path.read_text())
    return fixtures


def discover_loaders(package_name: str = LOADERS_PACKAGE) -> list[tuple[str, Any]]:
    package = importlib.import_module(package_name)
    names = sorted(m.name for m in pkgutil.iter_modules(package.__path__) if m.name[:2].isdigit())
    return [(name, importlib.import_module(f"{package_name}.{name}").load) for name in names]


async def run_seed(pool: asyncpg.Pool, fixtures: dict[str, Any] | None = None) -> list[str]:
    """Run all loaders in one transaction; returns the loader names that ran."""
    fixtures = load_fixtures() if fixtures is None else fixtures
    ran: list[str] = []
    async with pool.acquire() as conn, conn.transaction():
        for name, load in discover_loaders():
            await load(conn, fixtures)
            ran.append(name)
    return ran


async def main() -> None:
    pool = await create_pool()
    try:
        await run_migrations(pool)
        ran = await run_seed(pool)
        print(f"seeded: {', '.join(ran)}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
