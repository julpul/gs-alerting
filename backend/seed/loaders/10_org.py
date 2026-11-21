"""Seed the demo organization: domains, teams, users, roles, memberships and channel preferences."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from app.config import get_settings
from app.core.ids import ORG_ID, stable_id
from app.core.security import hash_password
from seed.loaders._support import profile

DEFAULT_TIMEZONE = "Europe/Warsaw"


def working_hours_json(raw: dict[str, Any] | None, timezone: str) -> dict[str, Any]:
    """Fixture `{start, end}` (weekdays are documentation only) -> what `channel_select` reads."""
    if not raw:
        return {}
    return {"from": raw["start"], "to": raw["end"], "timezone": timezone}


def _scope(role: dict[str, Any], domains: dict[str, UUID], teams: dict[str, UUID]) -> tuple[str, UUID]:
    kind = role["role"]
    if kind == "master":
        return "org", ORG_ID
    if kind == "domain_root":
        return "domain", domains[role["scope"]]
    return "team", teams[role["scope"]]


async def _upsert_users(conn: asyncpg.Connection, org: dict[str, Any], user_id: dict[str, UUID]) -> None:
    existing = {r["id"] for r in await conn.fetch("SELECT id FROM users")}
    password = get_settings().demo_password  # set on a shared server: every demo user gets it (never logged)
    password_hash: str | None = None
    for u in org["users"]:
        uid = user_id[u["email"]]
        timezone = u.get("timezone") or DEFAULT_TIMEZONE
        if uid in existing:
            await conn.execute(
                "UPDATE users SET display_name = $2, phone = $3, timezone = $4 WHERE id = $1",
                uid,
                u["displayName"],
                u.get("phone"),
                timezone,
            )
            if password:
                password_hash = password_hash or hash_password(password)
                await conn.execute("UPDATE users SET password_hash = $2 WHERE id = $1", uid, password_hash)
            continue
        password_hash = password_hash or hash_password(password or org["password"])
        await conn.execute(
            "INSERT INTO users (id, email, display_name, password_hash, phone, timezone) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            uid,
            u["email"],
            u["displayName"],
            password_hash,
            u.get("phone"),
            timezone,
        )
    for u in org["users"]:
        manager = user_id[u["manager"]] if u.get("manager") else None
        await conn.execute("UPDATE users SET manager_id = $2 WHERE id = $1", user_id[u["email"]], manager)


async def _roles_and_preferences(
    conn: asyncpg.Connection,
    org: dict[str, Any],
    core: bool,
    user_id: dict[str, UUID],
    domain_id: dict[str, UUID],
    team_id: dict[str, UUID],
) -> None:
    master_id = next(user_id[u["email"]] for u in org["users"] for r in u["roles"] if r["role"] == "master")
    for u in org["users"]:
        uid = user_id[u["email"]]
        for r in u["roles"]:
            scope_type, scope_id = _scope(r, domain_id, team_id)
            await conn.execute(
                "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                uid,
                r["role"],
                scope_type,
                scope_id,
                master_id,
            )
            if r["role"] == "member":
                await conn.execute(
                    "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    scope_id,
                    uid,
                )
        prefs = u.get("preferences")
        if prefs and not core:
            # Preferences edited through the API since the first seed are kept.
            await conn.execute(
                "INSERT INTO preferences (user_id, by_severity, working_hours) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id) DO NOTHING",
                uid,
                prefs["bySeverity"],
                working_hours_json(prefs.get("workingHours"), u.get("timezone") or DEFAULT_TIMEZONE),
            )


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    org = dict(fixtures["org"])
    core = profile() == "core"
    if core:
        org["users"] = [u for u in org["users"] if u.get("core")]
    domain_id = {d["slug"]: stable_id("domain", d["slug"]) for d in org["domains"]}
    team_id = {t["slug"]: stable_id("team", t["slug"]) for t in org["teams"]}
    user_id = {u["email"]: stable_id("user", u["email"]) for u in org["users"]}

    for d in org["domains"]:
        await conn.execute(
            "INSERT INTO domains (id, slug, name) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug, name = EXCLUDED.name",
            domain_id[d["slug"]],
            d["slug"],
            d["name"],
        )
    for t in org["teams"]:
        await conn.execute(
            "INSERT INTO teams (id, slug, name) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug, name = EXCLUDED.name",
            team_id[t["slug"]],
            t["slug"],
            t["name"],
        )
        for slug in t["domains"]:
            await conn.execute(
                "INSERT INTO team_domains (team_id, domain_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                team_id[t["slug"]],
                domain_id[slug],
            )
    await _upsert_users(conn, org, user_id)
    await _roles_and_preferences(conn, org, core, user_id, domain_id, team_id)
