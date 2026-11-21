"""Replies by SMS (spec 5.1, 11.4): `ACK <n>`, `RESOLVE <n> [note]`, `APPROVE <n>`, `REJECT <n>`.

The sender's phone number identifies the user; everything then runs as that user with their normal scope
and role checks, through the same functions the REST API uses. The return value is the text the gateway
shows as the reply.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

from app.core.audit import user_actor
from app.core.errors import AppError, not_found
from app.core.ids import format_short_id
from app.core.outbox import publish_outbox_rows
from app.core.rabbit import Rabbit
from app.core.scope import load_scope
from app.core.sse import publish_sse
from app.repositories import alert_actions as actions
from app.repositories import alerts as repo
from app.schemas.internal import User

log = logging.getLogger(__name__)

COMMAND = re.compile(r"^\s*(ack|resolve|approve|reject)\s+(?:a-)?(\d+)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
HELP = "Unknown command. Reply ACK <n>, RESOLVE <n>, APPROVE <n> or REJECT <n>."
NOTE_LIMIT = 500


def phone_digits(value: str) -> str:
    """`+48 500-100-200` and `0048500100200` -> `48500100200`."""
    digits = re.sub(r"\D", "", value)
    return digits[2:] if digits.startswith("00") else digits


async def user_by_phone(conn: asyncpg.Connection, phone: str) -> User | None:
    digits = phone_digits(phone)
    if not digits:
        return None
    row = await conn.fetchrow(
        "SELECT id, email, display_name, phone, manager_id, timezone FROM users "
        "WHERE phone IS NOT NULL AND regexp_replace(phone, '\\D', '', 'g') = $1 ORDER BY id LIMIT 1",
        digits,
    )
    return User(**dict(row)) if row else None


async def handle_sms(pool: asyncpg.Pool, rabbit: Rabbit, sender: str, text: str) -> str:
    """Process one reply; unknown senders raise 404, everything else yields a reply text."""
    async with pool.acquire() as conn:
        user = await user_by_phone(conn, sender)
    if user is None:
        raise not_found("sender")
    match = COMMAND.match(text or "")
    if match is None:
        return HELP
    verb, number, rest = match.group(1).lower(), int(match.group(2)), match.group(3).strip()
    try:
        if verb in ("approve", "reject"):
            return await _decide(pool, user, number, verb)
        return await _alert_command(pool, rabbit, user, verb, number, rest[:NOTE_LIMIT] or None)
    except AppError as exc:
        return exc.message


async def _alert_command(
    pool: asyncpg.Pool, rabbit: Rabbit, user: User, verb: str, number: int, note: str | None
) -> str:
    ref = format_short_id(number)
    outbox_ids: list[int] = []
    async with pool.acquire() as conn:
        scope = await load_scope(conn, user.id)
        async with conn.transaction():
            row = await repo.find_alert(conn, ref, scope, for_update=True)
            if row is None:
                raise not_found(f"alert {ref}")
            await actions.require_actor_role(conn, scope, row)
            if verb == "ack":
                changed = await actions.ack_alert(conn, row, user)
            else:
                changed = await actions.resolve_alert(conn, row, user, note)
                if changed:
                    outbox_ids = await actions.plan_resolved_updates(conn, row["id"])
    if changed:
        await _announce(pool, rabbit, row, user, verb, outbox_ids)
    done = "acknowledged" if verb == "ack" else "resolved"
    return f"{ref} {done}." if changed else f"{ref} was already {done}."


async def _announce(
    pool: asyncpg.Pool, rabbit: Rabbit, row: asyncpg.Record, user: User, verb: str, outbox_ids: list[int]
) -> None:
    status = "acknowledged" if verb == "ack" else "resolved"
    try:
        await publish_outbox_rows(pool, rabbit, outbox_ids)
        await publish_sse(
            "alert.acked" if verb == "ack" else "alert.resolved",
            {
                "alertId": str(row["id"]),
                "shortId": format_short_id(row["short_id"]),
                "by": {"kind": "user", "id": str(user.id), "displayName": user.display_name},
                "status": status,
            },
            team_ids=[row["team_id"]],
        )
    except Exception:
        log.exception("publishing the SMS %s of alert %s failed", verb, row["id"])


async def _decide(pool: asyncpg.Pool, user: User, number: int, verb: str) -> str:
    try:
        from app.runbooks.approvals import decide
    except ImportError:
        return "Approvals are unavailable right now."
    decision = "approve" if verb == "approve" else "reject"
    async with pool.acquire() as conn, conn.transaction():
        result: dict[str, Any] = await decide(
            conn,
            str(number),  # the numeric short id of the SMS; the approvals module resolves it
            decision=decision,
            actor=user_actor(user.id, user.display_name),
            comment=None,
        )
    status = (result or {}).get("status") or ("approved" if decision == "approve" else "rejected")
    return f"Approval {number} {status}."
