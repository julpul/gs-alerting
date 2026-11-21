"""Who is calling the Herald UI endpoints.

Demo mode (the UI's "Viewing as" switcher) sends `X-Herald-User: person:<id>`, `X-Herald-Role`,
`X-Herald-Teams`. A person id is the local part of our user's email (`anna@demo` -> `anna`).
Authorization always uses OUR scope/roles of that user (the role header is only a UI hint).
A Bearer JWT / PAT (our /v1 auth) is accepted too and wins over the headers.
Without any identity the caller is `master@demo` (demo default, like Herald's `person:demo` admin).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import Depends, Request

from app.core.errors import AppError
from app.core.scope import Scope, current_user, load_scope

HERALD_ROLES = ("responder", "approver", "editor", "admin")
DEFAULT_PERSON = "master"


@dataclass
class HeraldActor:
    user: dict[str, Any]  # users row: id, email, display_name, phone, timezone
    scope: Scope
    ref: str  # "person:<id>"
    role: str  # UI hint from X-Herald-Role (responder|approver|editor|admin)
    teams: list[str]  # team slugs from X-Herald-Teams (UI "acting for" hint)

    @property
    def user_id(self) -> UUID:
        return self.user["id"]

    @property
    def actor(self) -> dict[str, Any]:
        """Actor dict for timeline/audit (plan §3.1)."""
        return {
            "type": "user",
            "id": str(self.user["id"]),
            "name": self.user["display_name"],
            "onBehalfOf": None,
        }


def person_id(email: str) -> str:
    return email.split("@", 1)[0]


def person_ref(email: str) -> str:
    return f"person:{person_id(email)}"


async def user_by_person(conn: asyncpg.Connection, ref: str) -> asyncpg.Record | None:
    pid = ref.removeprefix("person:")
    return await conn.fetchrow(
        "SELECT id, email, display_name, phone, timezone FROM users WHERE split_part(email, '@', 1) = $1", pid
    )


async def herald_actor(request: Request) -> HeraldActor:
    pool: asyncpg.Pool = request.app.state.pool
    role = request.headers.get("x-herald-role", "admin")
    if role not in HERALD_ROLES:
        raise AppError(400, "unknown_role", f"unknown role '{role}'")
    teams = [t.strip() for t in request.headers.get("x-herald-teams", "").split(",") if t.strip()]
    async with pool.acquire() as conn:
        row = None
        if request.headers.get("authorization") or request.query_params.get("access_token"):
            user = await current_user(request)
            row = await conn.fetchrow(
                "SELECT id, email, display_name, phone, timezone FROM users WHERE id = $1", user.id
            )
        if row is None:
            row = await user_by_person(conn, request.headers.get("x-herald-user") or DEFAULT_PERSON)
        if row is None:
            raise AppError(401, "unknown_person", "unknown person in X-Herald-User")
        scope = await load_scope(conn, row["id"])
    return HeraldActor(user=dict(row), scope=scope, ref=person_ref(row["email"]), role=role, teams=teams)


Actor = Annotated[HeraldActor, Depends(herald_actor)]
