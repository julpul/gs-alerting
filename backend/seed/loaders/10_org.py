"""Seed the demo organization: domains, teams, users, memberships and role assignments."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.core.ids import ORG_ID, stable_id
from app.core.security import hash_password


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    org = fixtures["org"]
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

    existing = {r["id"] for r in await conn.fetch("SELECT id FROM users")}
    password_hash: str | None = None
    for u in org["users"]:
        uid = user_id[u["email"]]
        if uid not in existing:
            password_hash = password_hash or hash_password(org["password"])
            await conn.execute(
                "INSERT INTO users (id, email, display_name, password_hash, phone) "
                "VALUES ($1, $2, $3, $4, $5)",
                uid,
                u["email"],
                u["name"],
                password_hash,
                u.get("phone"),
            )
        else:
            await conn.execute(
                "UPDATE users SET display_name = $2, phone = $3 WHERE id = $1", uid, u["name"], u.get("phone")
            )
    for u in org["users"]:
        manager = user_id[u["manager"]] if u.get("manager") else None
        await conn.execute("UPDATE users SET manager_id = $2 WHERE id = $1", user_id[u["email"]], manager)

    master_id = next(user_id[r["user"]] for r in org["roles"] if r["role"] == "master")
    for r in org["roles"]:
        role = r["role"]
        if role == "master":
            scope_type, scope_id = "org", ORG_ID
        elif role == "domain_root":
            scope_type, scope_id = "domain", domain_id[r["domain"]]
        else:
            scope_type, scope_id = "team", team_id[r["team"]]
        await conn.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id, granted_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            user_id[r["user"]],
            role,
            scope_type,
            scope_id,
            master_id,
        )
        if role == "member":
            await conn.execute(
                "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                scope_id,
                user_id[r["user"]],
            )
