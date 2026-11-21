"""Seed the demo sources with fixed, literal demo keys (stored as sha256 like any other key)."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.config import get_settings
from app.core.ids import stable_id
from app.core.security import hash_token
from app.pipeline.mapping_presets import preset_mapping

DEMO_KEY_DISPLAY_LEN = 12


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    for src in fixtures["sources"]["sources"]:
        team_id = stable_id("team", src["team"])
        domain_id = await conn.fetchval(
            "SELECT td.domain_id FROM team_domains td JOIN domains d ON d.id = td.domain_id "
            "WHERE td.team_id = $1 ORDER BY d.slug LIMIT 1",
            team_id,
        )
        config = dict(src.get("config") or {})
        if src["type"] == "rabbitmq":
            config.setdefault("url", get_settings().rabbitmq_url)
        mapping = src.get("mapping") or preset_mapping(src.get("preset"))
        key = src["key"]
        # Existing rows are left alone: keys may have been rotated and mappings edited since.
        await conn.execute(
            "INSERT INTO sources (id, name, team_id, domain_id, type, preset, key_prefix, key_hash, config, "
            "mapping, mapping_status, default_severity) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) ON CONFLICT (id) DO NOTHING",
            stable_id("source", src["name"]),
            src["name"],
            team_id,
            domain_id,
            src["type"],
            src.get("preset"),
            key[:DEMO_KEY_DISPLAY_LEN],
            hash_token(key),
            config,
            mapping,
            mapping["status"] if mapping else None,
            src.get("default_severity", "warning"),
        )
