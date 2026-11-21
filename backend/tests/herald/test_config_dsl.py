"""Herald RuleDoc <-> our RuleDef: round trips (brief rules, every seeded rule) and edge cases."""

from __future__ import annotations

import copy
from uuid import uuid4

import pytest
import yaml

from app.herald import dsl
from app.herald.dsl import DslError, herald_to_rule, load_directory, rule_to_herald, when_to_match
from app.herald.explain import event_view, explain_when, failing_leaves
from app.pipeline.fields import EventView
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.index import rule_from_row
from app.pipeline.rules.model import parse_rule
from tests.herald.config_helpers import RUNBOOKS, brief_rules, sample

SEED_RULES = RUNBOOKS.parent / "rules.yaml"

SECTIONS = ("name", "owner", "enabled", "severity", "when", "notify", "group", "runbook", "resolve_when")


def _events() -> list[EventView]:
    """Sample events plus variations that flip each brief rule's conditions."""
    raws = [
        sample(n) for n in ("workflow_failed", "prometheus_high_error_rate", "pricing_log", "trade_rejected")
    ]
    small = copy.deepcopy(raws[3])
    small["payload"]["notional"] = 2_000_000
    test_env = {**raws[0], "environment": "test"}
    warn = {**raws[1], "labels": {**raws[1]["labels"], "severity": "warning"}}
    info = {**raws[2], "level": "INFO"}
    views = []
    for raw in [*raws, small, test_env, warn, info]:
        meta = {"severity": str(raw.get("severity") or raw.get("level") or "").lower() or "critical"}
        meta.update(
            service=raw.get("workflowName") or raw.get("logger") or (raw.get("labels") or {}).get("service")
        )
        meta["severity"] = {"warn": "warning"}.get(meta["severity"], meta["severity"])
        if "labels" in raw:
            meta["severity"] = raw["labels"]["severity"]
        views.append(EventView(raw, meta))
    return views


@pytest.fixture
async def directory(seeded):
    async with seeded.acquire() as conn:
        return await load_directory(conn)


async def test_brief_rules_round_trip_exactly(directory):
    for doc in brief_rules():
        data, extras = herald_to_rule(doc, directory)
        rule = parse_rule(data)
        compile_rule(rule)
        back = rule_to_herald(rule, directory, extras)
        expected = {**doc, "owner": "team:team-payments" if doc["owner"] == "person:anna" else doc["owner"]}
        for key in SECTIONS:
            assert back[key] == expected.get(key, True if key == "enabled" else None), (doc["id"], key)
        expected_present = {"body": "", "fields": [], "highlight": [], "colour": None, **doc["present"]}
        assert back["present"] == expected_present


async def test_brief_rules_translate_semantically(directory):
    """Without the stored Herald sections the derived document still means the same rule."""
    events = _events()
    for doc in brief_rules():
        data, _ = herald_to_rule(doc, directory)
        rule = parse_rule(data)
        derived = rule_to_herald(rule, directory)
        again = parse_rule(herald_to_rule(derived, directory)[0])
        compiled, recompiled = compile_rule(rule), compile_rule(again)
        assert [compiled.matches(e) for e in events] == [recompiled.matches(e) for e in events], doc["id"]
        assert again.routing.model_dump() == rule.routing.model_dump(), doc["id"]
        assert rule_to_herald(again, directory) == {**derived, "version": 1}
    rules = {d["id"]: parse_rule(herald_to_rule(d, directory)[0]) for d in brief_rules()}
    assert rules["anna-payments-critical"].routing.time_window.timezone == "Europe/London"
    assert rules["anna-payments-critical"].routing.hard[0].id == "anna@demo"
    assert rules["pricing-engine-warnings"].grouping.window_seconds == 300
    assert rules["settlements-order-settlement-failed"].priority == "critical"
    assert [r.kind for r in rules["settlements-order-settlement-failed"].routing.hard] == ["oncall"]


async def test_brief_rules_match_the_samples(directory):
    docs = {d["id"]: d for d in brief_rules()}
    rates = parse_rule(herald_to_rule(docs["rates-large-trade-rejected"], directory)[0])
    settle = parse_rule(herald_to_rule(docs["settlements-order-settlement-failed"], directory)[0])
    assert compile_rule(rates).matches(EventView(sample("trade_rejected"), {}))
    assert compile_rule(settle).matches(EventView(sample("workflow_failed"), {}))


async def test_every_seeded_rule_round_trips(seeded, directory):
    seeded_names = [r["rule"]["name"] for r in yaml.safe_load(SEED_RULES.read_text())["rules"]]
    rows = await seeded.fetch(
        "SELECT id, team_id, enabled, version, definition FROM rules WHERE name = ANY($1) ORDER BY name",
        seeded_names,
    )
    assert len(rows) == len(seeded_names) == 5
    events = _events()
    for row in rows:
        rule = rule_from_row(row)
        doc = rule_to_herald(rule, directory)
        # unchanged in the UI -> exactly our rule again (escalation, dedup window, channels kept)
        data, _ = herald_to_rule(doc, directory, base=rule)
        assert parse_rule(data).definition() == rule.definition(), rule.name
        # translated from scratch -> same decisions and recipients
        fresh = parse_rule(herald_to_rule(doc, directory)[0])
        assert [compile_rule(fresh).matches(e) for e in events] == [
            compile_rule(rule).matches(e) for e in events
        ]
        assert fresh.routing.hard == rule.routing.hard and fresh.routing.soft == rule.routing.soft, rule.name
        assert fresh.presentation.title_template == rule.presentation.title_template
        assert fresh.runbook_id == rule.runbook_id


async def test_changed_section_replaces_only_that_section(seeded, directory):
    row = await seeded.fetchrow(
        "SELECT id, team_id, enabled, version, definition FROM rules WHERE name = $1",
        "Rates - big rejected trades",
    )
    rule = rule_from_row(row)
    doc = rule_to_herald(rule, directory)
    doc["present"]["title"] = "Rejected {{payload.tradeId}}"
    data, _ = herald_to_rule(doc, directory, base=rule)
    changed = parse_rule(data)
    assert changed.presentation.title_template == "Rejected {{payload.tradeId}}"
    assert changed.routing == rule.routing and changed.dedup == rule.dedup and changed.match == rule.match


def test_not_is_pushed_down_to_negated_operators():
    when = {
        "all": [
            {"field": "type", "op": "eq", "value": "X"},
            {
                "not": {
                    "any": [
                        {"field": "env", "op": "in", "value": ["test"]},
                        {"field": "n", "op": "gt", "value": 5},
                    ]
                }
            },
            {"not": {"field": "flag", "op": "exists"}},
        ]
    }
    match = when_to_match(when)
    assert match == {
        "op": "and",
        "conditions": [
            {"field": "type", "operator": "eq", "value": "X"},
            {
                "op": "and",
                "conditions": [
                    {"field": "env", "operator": "not_in", "value": ["test"]},
                    {"field": "n", "operator": "lte", "value": 5},
                ],
            },
            {"field": "flag", "operator": "exists", "value": False},
        ],
    }
    assert dsl.match_to_when(
        {"op": "and", "conditions": [{"field": "e", "operator": "not_in", "value": [1]}]}
    ) == {"all": [{"not": {"field": "e", "op": "in", "value": [1]}}]}
    double = when_to_match({"not": {"not": {"field": "a", "op": "ne", "value": 1}}})
    assert double["conditions"] == [{"field": "a", "operator": "neq", "value": 1}]
    either = when_to_match(
        {"not": {"all": [{"field": "a", "op": "eq", "value": 1}, {"field": "b", "op": "eq", "value": 2}]}}
    )
    assert either["op"] == "or"


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ({"not": {"field": "msg", "op": "contains", "value": "x"}}, "excluded 'contains'"),
        ({"field": "a", "op": "between", "value": 1}, "unknown operator"),
        ({"field": "", "op": "eq", "value": 1}, "choose a field"),
        ({"field": "a", "op": "eq"}, "needs a value"),
        ({"all": []}, "non-empty list"),
        ({"all": [{"field": "a", "value": 1}], "any": []}, "exactly one"),
        ({"field": "level", "op": "severity_gte", "value": "warn"}, "@severity only"),
        ({"field": "@severity", "op": "severity_gte", "value": "loud"}, "not a known severity"),
        ("nope", "non-empty object"),
    ],
)
def test_invalid_conditions_are_explained(when, message):
    with pytest.raises(DslError, match=message):
        when_to_match(when)


def test_operators_aliases_and_shorthand():
    match = when_to_match({"type": "WORKFLOW_FAILED", "error.code": "TIMEOUT"})
    assert [c["operator"] for c in match["conditions"]] == ["eq", "eq"]
    leaf = when_to_match({"field": "@severity", "op": "severity_gte", "value": "WARN"})["conditions"][0]
    assert leaf == {"field": "_severity", "operator": "in", "value": ["critical", "error", "warning"]}
    assert dsl.match_to_when({"op": "or", "conditions": [leaf]}) == {
        "any": [{"field": "@severity", "op": "severity_gte", "value": "warning"}]
    }
    assert when_to_match({"field": "a", "op": ">=", "value": 3})["conditions"][0]["operator"] == "gte"
    assert when_to_match({"field": "a", "op": "in", "value": "x"})["conditions"][0]["value"] == ["x"]
    assert (
        when_to_match({"field": "a", "op": "matches", "value": "^x"})["conditions"][0]["operator"] == "regex"
    )
    assert dsl.template_to_ours("{{@type}} on {{ @service }}") == "{{_type}} on {{ _service }}"
    assert dsl.template_to_herald("{{_type}}!") == "{{@type}}!"
    assert dsl.seconds("5m") == 300 and dsl.seconds(None) is None and dsl.duration(7200) == "2h"
    assert dsl.duration(45) == "45s"
    with pytest.raises(DslError):
        dsl.seconds("soon")


async def test_bad_documents_are_rejected(directory):
    base = brief_rules()[0]
    for change, message in [
        ({"owner": "team:nope"}, "owner"),
        ({"name": " "}, "name"),
        ({"notify": [{"to": "person:ghost"}]}, "unknown person"),
        ({"notify": [{"to": "team:ghost"}]}, "unknown team"),
        ({"notify": [{"to": "everyone"}]}, "use team"),
        ({"notify": [{"to": "team:team-rates", "channels": ["pigeon"]}]}, "unknown channel"),
        ({"notify": [{"to": "team:team-rates", "channels": "loud"}]}, "list"),
        ({"notify": [{"channels": ["ui"]}]}, "needs 'to'"),
        ({"severity": "huge"}, "severity"),
        ({"group": {"by": ["a"], "window": "later"}}, "duration"),
    ]:
        with pytest.raises(DslError, match=message):
            herald_to_rule({**base, **change}, directory)
    with pytest.raises(DslError):
        herald_to_rule("rule", directory)  # type: ignore[arg-type]


async def test_recipients_and_preferences(directory):
    doc = {
        "name": "r",
        "owner": "team:team-rates",
        "when": {"field": "a", "op": "eq", "value": 1},
        "notify": [
            {"to": "oncall:team-rates", "channels": "preferences"},
            {"to": "role:team-rates:informed", "channels": ["ui"]},
            {"to": f"team:{uuid4()}"[:0] + "team:team-rates", "channels": ["email"]},
        ],
        "group": {"window": "10m", "by": []},
    }
    rule = parse_rule(herald_to_rule(doc, directory)[0])
    assert [(r.kind, r.id) for r in rule.routing.hard] == [("oncall", "team-rates")]
    assert [(r.kind, r.id) for r in rule.routing.soft] == [
        ("role", "team-rates:informed"),
        ("team", "team-rates"),
    ]
    assert rule.routing.channels_by_severity == {s: ["ui", "email"] for s in dsl.SEVERITIES}
    assert rule.dedup.window_seconds == 600 and rule.grouping is None
    back = rule_to_herald(rule, directory)
    assert back["notify"][0] == {"to": "team:team-rates/on-call", "channels": ["ui", "email"]}
    assert back["notify"][1]["to"] == "role:team-rates:informed"


def test_explain_tree_matches_our_compiler():
    when = {
        "all": [
            {"field": "payload.desk", "op": "eq", "value": "rates"},
            {"field": "payload.notional", "op": "gt", "value": 10_000_000},
            {"not": {"field": "payload.currency", "op": "in", "value": ["USD"]}},
        ]
    }
    raw = sample("trade_rejected")
    raw["payload"]["notional"] = 2_000_000
    ev = event_view(raw, None)
    why = explain_when(when, ev)
    assert why["passed"] is False
    fails = failing_leaves(why)
    assert fails == [
        {
            "field": "payload.notional",
            "op": "gt",
            "value": 10_000_000,
            "actual": 2_000_000,
            "missing": False,
            "passed": False,
        }
    ]
    rule = parse_rule({"name": "x", "teamId": str(uuid4()), "match": when_to_match(when)})
    assert compile_rule(rule).matches(ev) is False
    assert failing_leaves({"not": {"passed": True}, "passed": False}) == [
        {"not": {"passed": True}, "passed": False}
    ]
