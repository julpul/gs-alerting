"""Seed the three Appendix B runbooks (YAML, Markdown, free text) from `seed/fixtures/runbooks/`.

The free-text one is parsed with the deterministic heuristic at seed time (never an LLM), so seeding is
offline and idempotent. Ids are `stable_id("runbook", key)`, the ids the seeded rules reference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import yaml

from app.core.ids import stable_id
from app.runbooks import store
from app.runbooks.parsers import parse_runbook

RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "runbooks"


async def load(conn: asyncpg.Connection, fixtures: dict[str, Any]) -> None:
    index = yaml.safe_load((RUNBOOK_DIR / "index.yaml").read_text())
    for item in index["runbooks"]:
        source = (RUNBOOK_DIR / item["file"]).read_text()
        parsed = await parse_runbook(source, "auto", use_ai=False)
        attached = dict(parsed.attached)
        extra = dict(item.get("attached") or {})
        rule_ids = [str(stable_id("rule", k)) for k in extra.pop("ruleKeys", [])]
        if rule_ids:
            attached["ruleIds"] = rule_ids
        attached.update(extra)
        await store.upsert_seed(
            conn,
            stable_id("runbook", item["key"]),
            parsed,
            name=item.get("name") or parsed.name,
            source=source,
            team_id=stable_id("team", item["team"]),
            attached=attached,
        )
