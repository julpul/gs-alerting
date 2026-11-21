"""In-memory rule index (spec 7.3) with atomic snapshot swap and Redis-driven hot reload.

Rules whose top-level AND holds an `eq` on `_source|_schema|_type` are indexed by that value; the rest sit
in a wildcard list. `candidates(ev)` = index hits plus the wildcard list. A reload builds a new immutable
snapshot and swaps one reference, so readers (the event loop) never see a half-built index.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg
import orjson

from app.core.redis import get_redis, rkey
from app.pipeline.fields import EventView
from app.pipeline.rules.compiler import INDEXED_FIELDS, CompiledRule, compile_rule
from app.pipeline.rules.model import RuleDef, RuleValidationError, parse_rule

log = logging.getLogger(__name__)

CHANNEL = "rules:changed"
FULL_RELOAD_SECONDS = 30.0
POLL_SECONDS = 1.0
RECONNECT_SECONDS = 1.0


@dataclass(frozen=True)
class _Snapshot:
    by_id: dict[UUID, CompiledRule] = field(default_factory=dict)
    by_key: dict[tuple[str, str], list[CompiledRule]] = field(default_factory=dict)
    wildcard: list[CompiledRule] = field(default_factory=list)


def _build(rules: dict[UUID, CompiledRule]) -> _Snapshot:
    by_key: dict[tuple[str, str], list[CompiledRule]] = {}
    wildcard: list[CompiledRule] = []
    for compiled in rules.values():
        if compiled.index_key is None:
            wildcard.append(compiled)
        else:
            by_key.setdefault(compiled.index_key, []).append(compiled)
    return _Snapshot(by_id=dict(rules), by_key=by_key, wildcard=wildcard)


def rule_from_row(row: asyncpg.Record) -> RuleDef:
    """DB row -> `RuleDef`; id/version/enabled come from the columns, the rest from `definition`."""
    data = dict(row["definition"])
    data.update(id=str(row["id"]), version=row["version"], enabled=row["enabled"], teamId=str(row["team_id"]))
    return parse_rule(data)


def _compile_row(row: asyncpg.Record) -> CompiledRule | None:
    try:
        return compile_rule(rule_from_row(row))
    except RuleValidationError as exc:
        log.warning("skipping invalid rule %s: %s", row["id"], exc)
        return None


class RuleIndex:
    def __init__(self) -> None:
        self._snap = _Snapshot()

    def __len__(self) -> int:
        return len(self._snap.by_id)

    def get(self, rule_id: UUID) -> CompiledRule | None:
        return self._snap.by_id.get(rule_id)

    async def load_all(self, pool: asyncpg.Pool) -> None:
        rows = await pool.fetch("SELECT id, team_id, enabled, version, definition FROM rules WHERE enabled")
        compiled = {row["id"]: c for row in rows if (c := _compile_row(row)) is not None}
        self._snap = _build(compiled)

    async def reload_rule(self, pool: asyncpg.Pool, rule_id: UUID) -> None:
        row = await pool.fetchrow(
            "SELECT id, team_id, enabled, version, definition FROM rules WHERE id = $1", rule_id
        )
        # No await below: read-modify-swap is atomic with respect to other coroutines.
        rules = dict(self._snap.by_id)
        compiled = _compile_row(row) if row is not None and row["enabled"] else None
        if compiled is None:
            rules.pop(rule_id, None)
        else:
            rules[rule_id] = compiled
        self._snap = _build(rules)

    def candidates(self, ev: EventView) -> list[CompiledRule]:
        snap = self._snap
        hits: list[CompiledRule] = []
        for name in INDEXED_FIELDS:
            value = ev.meta.get(name[1:])
            if isinstance(value, str):
                hits.extend(snap.by_key.get((name, value), ()))
        hits.extend(snap.wildcard)
        return hits

    async def start_hot_reload(self, pool: asyncpg.Pool) -> asyncio.Task[None]:
        """Subscribe to `rules:changed` (already subscribed when this returns) and keep the index fresh.

        A change message reloads that rule within ~1 s; a full reload every 30 s is the safety net.
        Cancel the returned task to stop.
        """
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(rkey(CHANNEL))
        return asyncio.create_task(self._listen(pool, pubsub), name="rule-index-hot-reload")

    async def _listen(self, pool: asyncpg.Pool, pubsub) -> None:
        loop = asyncio.get_running_loop()
        next_full = loop.time() + FULL_RELOAD_SECONDS
        try:
            while True:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_SECONDS)
                    if message is not None:
                        await self._on_message(pool, message["data"])
                    if loop.time() >= next_full:
                        await self.load_all(pool)
                        next_full = loop.time() + FULL_RELOAD_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("rule hot reload failed; resubscribing")
                    await asyncio.sleep(RECONNECT_SECONDS)
                    with contextlib.suppress(Exception):
                        await pubsub.subscribe(rkey(CHANNEL))
                        await self.load_all(pool)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def _on_message(self, pool: asyncpg.Pool, data: str | bytes) -> None:
        try:
            rule_id = UUID(str(orjson.loads(data)["ruleId"]))
        except (ValueError, KeyError, TypeError):
            await self.load_all(pool)
            return
        await self.reload_rule(pool, rule_id)
