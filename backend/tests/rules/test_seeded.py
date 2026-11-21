"""Seeded sources/rules: idempotent loaders, fixed demo keys, and the four rules against Appendix A events."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.ids import stable_id
from app.core.security import hash_token
from app.pipeline.fields import EventView
from app.pipeline.normalize import normalize
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
    "latencyMs": 1500,
    "message": "quote latency above threshold",
}

SECURITY_EVENT = {
    "type": "security.secret_leaked",
    "severity": "critical",
    "title": "AWS access key committed in PR #4812 (payments-api)",
    "dedupKey": "pr-4812-aws-key",
    "attributes": {"repo": "payments-api", "pullRequest": 4812, "secretType": "aws_access_key"},
}
SECURITY_ENVELOPE = {
    "typed": True,
    "_meta": {
        "source": "pr-review-agent",
        "source_name": "pr-review-agent",
        "schema": "security.secret_leaked",
        "severity": "critical",
        "type": "security.secret_leaked",
        "title": SECURITY_EVENT["title"],
    },
}

EXTRA = {"pr-review-agent": SECURITY_ENVELOPE}
SAMPLES = {
    "workflow-broker": (SETTLEMENTS_EVENT, "Settlements - workflow failed"),
    "prometheus": (ALERTMANAGER_EVENT, "Anna - critical payments-api"),
    "trade-gateway": (TRADE_EVENT, "Rates - big rejected trades"),
    "pricing-logs": (PRICING_EVENT, "Pricing - engine warnings (grouped)"),
    "pr-review-agent": (SECURITY_EVENT, "Security - typed alerts"),
}


SEEDED_RULE_IDS = {
    stable_id("rule", key)
    for key in (
        "settlements-workflow-failed",
        "pricing-engine-warnings",
        "rates-big-rejected-trades",
        "anna-payments-api-critical",
        "security-typed-alerts",
    )
}


def seeded_matches(index: RuleIndex, view: EventView) -> list[str]:
    """Names of the seeded rules matching `view` (other tests may leave their own rules in the DB)."""
    return [c.rule.name for c in index.candidates(view) if c.rule.id in SEEDED_RULE_IDS and c.matches(view)]


def event_view(source: dict, raw: dict, envelope_extra: dict | None = None) -> EventView:
    """The worker's own normalization (mapping, schema detection, `_meta`)."""
    envelope = {"raw": raw, "received_at": "2026-11-21T09:00:00.000Z", **(envelope_extra or {})}
    return normalize(envelope, source)


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
    view = event_view(sources[source_name], raw, EXTRA.get(source_name))
    matching = seeded_matches(index, view)
    assert matching == [rule_name]


async def test_seeded_rules_reject_near_misses(seeded, sources):
    index = RuleIndex()
    await index.load_all(seeded)

    def matches(source: str, raw: dict) -> list[str]:
        view = event_view(sources[source], raw)
        return seeded_matches(index, view)

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
        view = event_view(sources[source_name], raw, EXTRA.get(source_name))
        rule = next(c.rule for c in index.candidates(view) if c.rule.name == rule_name)
        titles[source_name] = render(rule.presentation.title_template, view)
    assert titles["workflow-broker"] == "order-settlement failed at validate-counterparty"
    assert titles["trade-gateway"] == "Rejected EUR 25000000 — LIMIT_BREACH"
    assert titles["prometheus"] == "HighErrorRate: Error rate 12% over the last 5 minutes"
    assert titles["pricing-logs"] == "Pricing WARN: EURUSD"
    assert titles["pr-review-agent"] == SECURITY_EVENT["title"]


async def test_security_rule_routes_typed_alerts_to_oncall_by_phone_and_sms(seeded):
    row = await seeded.fetchrow(
        "SELECT * FROM rules WHERE id = $1", stable_id("rule", "security-typed-alerts")
    )
    assert row["team_id"] == stable_id("team", "team-security")
    routing = row["definition"]["routing"]
    assert routing["hard"] == [{"kind": "oncall", "id": "team-security"}]
    assert routing["channelsBySeverity"]["critical"] == ["phone", "sms"]


async def test_security_rule_ignores_other_security_sources_and_types(seeded, sources):
    index = RuleIndex()
    await index.load_all(seeded)
    other_type = {**SECURITY_EVENT, "type": "billing.invoice"}
    meta = {**SECURITY_ENVELOPE["_meta"], "type": "billing.invoice", "schema": "billing.invoice"}
    view = event_view(sources["pr-review-agent"], other_type, {"typed": True, "_meta": meta})
    assert seeded_matches(index, view) == []
    view = event_view(
        sources["pricing-logs"],
        SECURITY_EVENT,
        SECURITY_ENVELOPE | {"_meta": {**SECURITY_ENVELOPE["_meta"], "source": "pricing-logs"}},
    )
    assert seeded_matches(index, view) == []


async def test_custody_demo_source_has_no_mapping_and_no_rules(seeded, sources):
    custody = sources["custody"]
    assert custody["key_hash"] == hash_token("demo-key-custody")
    assert custody["team_id"] == stable_id("team", "team-settlements")
    assert custody["mapping"] is None and custody["mapping_status"] is None
    assert await seeded.fetchval("SELECT count(*) FROM rules WHERE definition::text LIKE '%custody%'") == 0
