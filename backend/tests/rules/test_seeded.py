"""Seeded sources/rules: idempotent loaders, fixed demo keys, and the four rules against Appendix A events."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.ids import stable_id
from app.core.security import hash_token
from app.pipeline.fields import EventView, get_path
from app.pipeline.rules.index import RuleIndex
from app.pipeline.rules.templates import render
from seed.seed import run_seed

SETTLEMENTS_EVENT = {
    "type": "WORKFLOW_FAILED",
    "workflowId": "WF-20261023-00042",
    "workflowName": "order-settlement",
    "step": "validate-counterparty",
    "severity": "ERROR",
    "environment": "prod",
    "timestamp": "2026-10-23T09:14:02.331Z",
    "error": {"code": "TIMEOUT", "message": "Counterparty service did not respond in 5000 ms"},
}
ALERTMANAGER_EVENT = {
    "alertname": "HighErrorRate",
    "labels": {"service": "payments-api", "region": "eu-west", "severity": "critical"},
    "annotations": {"summary": "Error rate 12% over the last 5 minutes"},
    "startsAt": "2026-10-23T09:13:00Z",
    "status": "firing",
}
TRADE_EVENT = {
    "meta": {"schema": "trade.rejected.v2", "producer": "trade-gateway", "ts": 1792833245000},
    "payload": {
        "tradeId": "T-998877",
        "desk": "rates",
        "notional": 25000000,
        "currency": "EUR",
        "reason": "LIMIT_BREACH",
    },
}
PRICING_EVENT = {
    "level": "WARN",
    "logger": "pricing-engine-3",
    "instrument": "EURUSD",
    "latencyMs": 450,
    "message": "quote latency above threshold",
}

SAMPLES = {
    "workflow-broker": (SETTLEMENTS_EVENT, "Settlements - workflow failed"),
    "prometheus": (ALERTMANAGER_EVENT, "Anna - critical payments-api"),
    "trade-gateway": (TRADE_EVENT, "Rates - big rejected trades"),
    "pricing-logs": (PRICING_EVENT, "Pricing - engine warnings (grouped)"),
}


def detect_schema(raw: dict) -> str:
    meta = raw.get("meta")
    for value in (
        meta.get("schema") if isinstance(meta, dict) else None,
        raw.get("type"),
        raw.get("alertname"),
    ):
        if isinstance(value, str):
            return value
    return "unknown"


def event_view(source: dict, raw: dict) -> EventView:
    """Minimal stand-in for the worker's normalization (P2-INGEST owns the real one)."""
    mapping = source["mapping"]
    meta = {"source": source["name"], "schema": detect_schema(raw)}
    for name, path in mapping["fields"].items():
        meta[name] = get_path(raw, path)
    severity = meta.get("severity")
    meta["severity"] = (
        mapping.get("severityValueMap", {}).get(severity, severity) or source["default_severity"]
    )
    return EventView(raw, meta)


@pytest.fixture
async def sources(seeded):
    rows = await seeded.fetch("SELECT * FROM sources")
    return {r["name"]: dict(r) for r in rows}


async def test_seeded_sources_have_fixed_demo_keys_and_teams(seeded, sources):
    expected = {
        "workflow-broker": ("demo-key-workflow-broker", "team-settlements", "webhook", None),
        "prometheus": ("demo-key-prometheus", "team-payments", "webhook", "alertmanager"),
        "pricing-logs": ("demo-key-pricing-logs", "team-pricing", "webhook", None),
        "pr-review-agent": ("demo-key-pr-review-agent", "team-security", "webhook", None),
        "trade-gateway": ("demo-key-trade-gateway", "team-rates", "rabbitmq", None),
    }
    assert set(expected) <= set(sources)
    for name, (key, team, type_, preset) in expected.items():
        row = sources[name]
        assert row["key_hash"] == hash_token(key)
        assert row["team_id"] == stable_id("team", team)
        assert (row["type"], row["preset"]) == (type_, preset)
        assert row["mapping_status"] == "accepted" and row["mapping"]["fields"]
    assert sources["trade-gateway"]["config"]["queue"] == "vistula.trade-gateway"
    assert sources["pr-review-agent"]["config"]["typed"] is True


async def test_seeded_rules_exist_with_versions_and_runbooks(seeded):
    rules = {r["name"]: r for r in await seeded.fetch("SELECT * FROM rules WHERE version >= 1")}
    settlements = rules["Settlements - workflow failed"]
    assert settlements["team_id"] == stable_id("team", "team-settlements")
    assert settlements["definition"]["runbookId"] == str(stable_id("runbook", "settlement-timeout"))
    assert settlements["definition"]["presentation"]["color"] == "red"
    assert settlements["definition"]["routing"]["channelsBySeverity"]["critical"] == ["phone", "sms"]
    anna = rules["Anna - critical payments-api"]["definition"]["routing"]
    assert anna["hard"] == [{"kind": "user", "id": "anna@demo"}]
    assert anna["timeWindow"] == {
        "from": "08:00",
        "to": "20:00",
        "timezone": "Europe/Warsaw",
        "outsideChannels": ["email"],
    }
    pricing = rules["Pricing - engine warnings (grouped)"]["definition"]
    assert pricing["grouping"] == {"byField": "instrument", "windowSeconds": 300}
    assert pricing["routing"]["hard"] == []
    n = await seeded.fetchval(
        "SELECT count(*) FROM rule_versions v JOIN rules r ON r.id = v.rule_id AND r.version = v.version"
    )
    assert n >= 4


async def test_seed_loaders_are_idempotent(seeded):
    async def snapshot():
        return (
            await seeded.fetchval("SELECT count(*) FROM sources"),
            await seeded.fetchval(
                "SELECT count(*) FROM rules WHERE id = ANY($1)",
                [stable_id("rule", "settlements-workflow-failed")],
            ),
            await seeded.fetchval("SELECT count(*) FROM rule_versions"),
            await seeded.fetchval("SELECT string_agg(key_hash, ',' ORDER BY name) FROM sources"),
        )

    before = await snapshot()
    await run_seed(seeded)
    await run_seed(seeded)
    assert await snapshot() == before


async def test_seed_does_not_clobber_edited_rule_or_rotated_key(seeded):
    rule_id = stable_id("rule", "pricing-engine-warnings")
    original = await seeded.fetchrow("SELECT definition, version FROM rules WHERE id = $1", rule_id)
    edited = deepcopy(original["definition"])
    edited["name"] = "Pricing - edited"
    await seeded.execute("UPDATE rules SET definition = $2, version = 2 WHERE id = $1", rule_id, edited)
    source_id = stable_id("source", "pricing-logs")
    old_hash = await seeded.fetchval("SELECT key_hash FROM sources WHERE id = $1", source_id)
    await seeded.execute("UPDATE sources SET key_hash = 'rotated' WHERE id = $1", source_id)
    try:
        await run_seed(seeded)
        assert (await seeded.fetchval("SELECT definition FROM rules WHERE id = $1", rule_id))[
            "name"
        ] == "Pricing - edited"
        assert await seeded.fetchval("SELECT key_hash FROM sources WHERE id = $1", source_id) == "rotated"
    finally:
        await seeded.execute(
            "UPDATE rules SET definition = $2, version = 1 WHERE id = $1", rule_id, original["definition"]
        )
        await seeded.execute("UPDATE sources SET key_hash = $2 WHERE id = $1", source_id, old_hash)


@pytest.mark.parametrize("source_name", list(SAMPLES))
async def test_appendix_a_event_matches_exactly_its_seeded_rule(seeded, sources, source_name):
    raw, rule_name = SAMPLES[source_name]
    index = RuleIndex()
    await index.load_all(seeded)
    view = event_view(sources[source_name], raw)
    matching = [c.rule.name for c in index.candidates(view) if c.matches(view)]
    assert matching == [rule_name]


async def test_seeded_rules_reject_near_misses(seeded, sources):
    index = RuleIndex()
    await index.load_all(seeded)

    def matches(source: str, raw: dict) -> list[str]:
        view = event_view(sources[source], raw)
        return [c.rule.name for c in index.candidates(view) if c.matches(view)]

    staging = {**SETTLEMENTS_EVENT, "environment": "staging"}
    assert matches("workflow-broker", staging) == []
    small = deepcopy(TRADE_EVENT)
    small["payload"]["notional"] = 9_000_000
    assert matches("trade-gateway", small) == []
    info = {**PRICING_EVENT, "level": "INFO"}
    assert matches("pricing-logs", info) == []
    other_logger = {**PRICING_EVENT, "logger": "risk-engine-1"}
    assert matches("pricing-logs", other_logger) == []
    warn_alert = deepcopy(ALERTMANAGER_EVENT)
    warn_alert["labels"]["severity"] = "warning"
    assert matches("prometheus", warn_alert) == []


async def test_seeded_titles_render_from_appendix_a(seeded, sources):
    index = RuleIndex()
    await index.load_all(seeded)
    titles = {}
    for source_name, (raw, rule_name) in SAMPLES.items():
        view = event_view(sources[source_name], raw)
        rule = next(c.rule for c in index.candidates(view) if c.rule.name == rule_name)
        titles[source_name] = render(rule.presentation.title_template, view)
    assert titles["workflow-broker"] == "order-settlement failed at validate-counterparty"
    assert titles["trade-gateway"] == "Rejected EUR 25000000 — LIMIT_BREACH"
    assert titles["prometheus"] == "HighErrorRate: Error rate 12% over the last 5 minutes"
    assert titles["pricing-logs"] == "Pricing WARN: EURUSD"
