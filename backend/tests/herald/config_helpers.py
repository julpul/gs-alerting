"""Shared bits of the HB-CONFIG tests (kept out of conftest.py so HB-CORE's conftest merges cleanly)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import orjson
import yaml

from app.core.redis import rkey

SAMPLES = Path(__file__).resolve().parents[2] / "seed" / "fixtures" / "samples"
RUNBOOKS = Path(__file__).resolve().parents[2] / "seed" / "fixtures" / "runbooks"
MASTER = {"X-Herald-User": "person:master"}
ANNA = {"X-Herald-User": "person:anna"}

# The four rules of the brief, as Herald's config/rules/*.yaml writes them (owners use our team slugs).
BRIEF_RULES_YAML = """
- id: settlements-order-settlement-failed
  owner: team:team-settlements
  name: Order settlement failed in prod
  when:
    all:
      - {field: type, op: eq, value: WORKFLOW_FAILED}
      - {field: workflowName, op: eq, value: order-settlement}
      - {field: environment, op: eq, value: prod}
  severity: critical
  present:
    title: "Settlement failed at {{step}}"
    body: "{{error.code}}: {{error.message}}"
    fields: [workflowId, step, error.code, error.message, environment]
    highlight: [step, error.message]
    colour: red
  group: {by: [workflowId]}
  notify:
    - {to: team:team-settlements/on-call, channels: [phone, sms]}
    - {to: team:team-settlements, channels: [ui]}
  resolve_when:
    all:
      - {field: type, op: eq, value: WORKFLOW_SUCCEEDED}
      - {field: workflowName, op: eq, value: order-settlement}
- id: anna-payments-critical
  owner: person:anna
  name: Anything critical from payments-api
  when:
    all:
      - {field: "@service", op: eq, value: payments-api}
      - {field: "@severity", op: severity_gte, value: critical}
  present:
    title: "{{alertname}} on {{labels.service}} ({{labels.region}})"
    body: "{{annotations.summary}}"
    fields: [labels.service, labels.region, labels.severity, startsAt]
    colour: red
  group: {by: [alertname, labels.service, labels.region]}
  notify:
    - to: person:anna
      channels: [sms]
      hours: {from: "08:00", to: "20:00", tz: Europe/London}
      else: [email]
    - {to: person:anna, channels: [ui]}
- id: pricing-engine-warnings
  owner: team:team-pricing
  name: Pricing engine warnings
  when:
    all:
      - {field: "@service", op: glob, value: "pricing-engine-*"}
      - {field: "@severity", op: severity_gte, value: warning}
  present:
    title: "{{instrument}}: {{message}}"
    body: "Reported by {{@service}}"
    fields: [instrument, latency_ms, logger]
    colour: amber
  group: {by: [instrument], window: 5m}
  notify:
    - {to: team:team-pricing, channels: [ui]}
- id: rates-large-trade-rejected
  owner: team:team-rates
  name: Large rates trade rejected
  when:
    all:
      - {field: meta.schema, op: glob, value: "trade.rejected.*"}
      - {field: payload.desk, op: eq, value: rates}
      - {field: payload.notional, op: gt, value: 10000000}
  severity: warning
  present:
    title: "Rejected {{payload.currency}} {{payload.notional}} — {{payload.reason}}"
    body: "Trade {{payload.tradeId}} from {{meta.producer}}"
    fields: [payload.tradeId, payload.desk, payload.notional, payload.currency, payload.reason]
    colour: amber
  group: {by: [payload.tradeId]}
  notify:
    - {to: team:team-rates, channels: [email, ui]}
"""


def brief_rules() -> list[dict[str, Any]]:
    return yaml.safe_load(BRIEF_RULES_YAML)


def sample(name: str) -> dict[str, Any]:
    return orjson.loads((SAMPLES / f"{name}.json").read_bytes())


async def push_recent(redis, source_id, raw: dict[str, Any], meta: dict[str, Any], event_id: str) -> None:
    """Write one entry into `recent:<source_id>` the way the worker does."""
    envelope = {"event_id": event_id, "source_id": str(source_id), "raw": raw, "_meta": meta}
    await redis.xadd(rkey("recent", str(source_id)), {"envelope": orjson.dumps(envelope).decode()})


async def eventually(check: Callable[[], Awaitable[Any]], timeout: float = 15.0) -> Any:  # noqa: ASYNC109
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s (last: {value!r})")
        await asyncio.sleep(0.05)
