"""Seeded runbooks and `runbook_for_alert`: lookup by rule id, by `matches`, by schema/type; rendering."""

from __future__ import annotations

from app.core.ids import stable_id, uuid7
from app.runbooks.lookup import guard_met, matches, runbook_for_alert
from app.schemas import api as wire

from .helpers import WORKFLOW_FAILED, make_alert, runbook_id, team

ALERTMANAGER = {
    "alertname": "HighErrorRate",
    "labels": {"service": "payments-api", "region": "eu-west", "severity": "critical"},
    "annotations": {"summary": "Error rate 12% over the last 5 minutes"},
    "status": "firing",
}
PRICING = {"level": "ERROR", "logger": "pricing-engine-3", "instrument": "EURUSD", "message": "slow"}


async def test_seed_has_the_three_appendix_b_runbooks(seeded):
    rows = {r["id"]: r for r in await seeded.fetch("SELECT * FROM runbooks")}
    settlement = rows[runbook_id("settlement-timeout")]
    assert settlement["format"] == "yaml" and settlement["team_id"] == team("team-settlements")
    assert settlement["attached"]["matches"] == {"type": "WORKFLOW_FAILED", "error.code": "TIMEOUT"}
    assert [s["action"] for s in settlement["steps"]] == ["http_get", "retry_workflow", "escalate"]
    errors = rows[runbook_id("HighErrorRate")]
    assert errors["format"] == "markdown" and len(errors["steps"]) == 4
    pricing = rows[runbook_id("pricing-latency")]
    assert pricing["format"] == "text" and pricing["attached"]["parsedBy"] == "heuristic"
    assert pricing["attached"]["ruleIds"] == [str(stable_id("rule", "pricing-engine-warnings"))]


async def test_lookup_by_attached_matches_renders_event_values(seeded):
    alert = await make_alert(seeded)
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.name == "settlement-timeout"
    wire.Runbook.model_validate(view.model_dump(mode="json"))
    assert [s.state.value for s in view.steps] == ["not_started"] * 3
    retry = view.steps[1]
    assert retry.text == "Retry workflow WF-20261023-00042"
    assert retry.parameters == {"workflowId": "WF-20261023-00042"}
    assert retry.trustLevel.value == "auto"
    assert view.steps[2].guard == "previous.failures >= 2" and view.steps[2].guardMet is False
    assert view.steps[0].trustLevel.value == "auto"


async def test_lookup_by_rule_runbook_id(seeded):
    rule_id = uuid7()
    await seeded.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, 'r', $3)",
        rule_id,
        team("team-payments"),
        {"runbookId": str(runbook_id("HighErrorRate"))},
    )
    alert = await make_alert(seeded, team_slug="team-payments", raw={"x": 1}, rule_id=rule_id)
    alert["runbook_id"] = None
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.name == "HighErrorRate on payments-api"
    assert view.steps[1].action == "failover_region"


async def test_lookup_by_alert_runbook_id(seeded):
    alert = await make_alert(
        seeded, team_slug="team-payments", raw={"x": 1}, runbook=runbook_id("HighErrorRate")
    )
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.id == str(runbook_id("HighErrorRate"))


async def test_lookup_alertmanager_by_matches_and_pricing_by_glob(seeded):
    view = await runbook_for_alert(
        seeded, await make_alert(seeded, team_slug="team-payments", raw=ALERTMANAGER)
    )
    assert view is not None and view.name.startswith("HighErrorRate")
    view = await runbook_for_alert(seeded, await make_alert(seeded, team_slug="team-pricing", raw=PRICING))
    assert view is not None and view.name == "Pricing latency"
    assert view.parsedBy.value == "heuristic"


async def test_lookup_by_attached_rule_ids(seeded):
    rule_id = uuid7()
    await seeded.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, 'r2', '{}')",
        rule_id,
        team("team-pricing"),
    )
    alert = await make_alert(
        seeded,
        team_slug="team-pricing",
        raw={"nothing": 1},
        rule_id=stable_id("rule", "pricing-engine-warnings"),
    )
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.name == "Pricing latency"


async def test_lookup_by_schema_or_type_attachment(seeded):
    rb = uuid7()
    await seeded.execute(
        "INSERT INTO runbooks (id, name, format, steps, attached, team_id) "
        "VALUES ($1, 'by type', 'text', $2, $3, $4)",
        rb,
        [{"id": "s1", "order": 1, "kind": "text", "text": "Look at {{widget}}", "approval": "none"}],
        {"type": "WIDGET_BROKEN"},
        team("team-security"),
    )
    alert = await make_alert(
        seeded, team_slug="team-security", raw={"type": "WIDGET_BROKEN", "widget": "w-1"}
    )
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.name == "by type" and view.steps[0].text == "Look at w-1"


async def test_runbooks_of_other_teams_are_not_matched(seeded):
    alert = await make_alert(seeded, team_slug="team-security", raw=WORKFLOW_FAILED)
    assert await runbook_for_alert(seeded, alert) is None


async def test_lookup_without_raw_event_reads_it_from_db(seeded):
    alert = await make_alert(seeded)
    del alert["raw_event"]
    view = await runbook_for_alert(seeded, alert)
    assert view is not None and view.steps[1].parameters["workflowId"] == "WF-20261023-00042"


def test_matches_helper_supports_lists_globs_and_numbers():
    from app.pipeline.fields import EventView

    ev = EventView(raw={"a": {"b": 5}, "name": "pricing-engine-2"}, meta={"service": "x"})
    assert matches({"matches": {"a.b": 5}}, ev)
    assert matches({"matches": {"name": "pricing-*"}}, ev)
    assert matches({"matches": {"name": ["nope", "pricing-engine-2"]}}, ev)
    assert matches({"matches": {"service": "x"}}, ev)
    assert not matches({"matches": {"a.b": 6}}, ev)
    assert not matches({"matches": {"missing": "x"}}, ev)
    assert not matches({}, ev)


def test_guard_evaluation():
    assert guard_met(None, 5) is None
    assert guard_met("previous.failures >= 2", 1) is False
    assert guard_met("previous.failures >= 2", 2) is True
    assert guard_met("previous.failures > 2", 2) is False
    assert guard_met("something custom", 2) is None
