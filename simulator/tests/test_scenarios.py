from __future__ import annotations

import random

import pytest

from tests.conftest import NOW
from vistula_sim.scenarios import SCENARIOS, Context, SimState


def build(
    name: str, intensity: float = 1.0, state: SimState | None = None, fmt: str = "alertmanager", **params
):
    ctx = Context(
        rng=random.Random(1),
        now=NOW,
        intensity=intensity,
        params={k: str(v) for k, v in params.items()},
        state=state or SimState(),
        prometheus_format=fmt,
    )
    return SCENARIOS[name].build(ctx)


def test_all_expected_scenarios_registered():
    assert set(SCENARIOS) == {
        "settlement_outage", "payments_error_spike", "pricing_latency", "trade_limit_breach",
        "secret_leaked", "poisoned_log", "storm_500", "new_schema", "resolve_all",
    }  # fmt: skip


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_builds_ordered_steps(name):
    steps = build(name)
    assert steps
    delays = [s.delay for s in steps]
    assert delays == sorted(delays) and delays[0] >= 0


def test_settlement_outage_is_repeated_counterparty_timeouts_in_prod():
    state = SimState()
    steps = build("settlement_outage", state=state)
    assert len(steps) >= 5
    for step in steps:
        assert step.message.source == "workflow-broker"
        e = step.message.payload
        assert (e["type"], e["workflowName"], e["environment"]) == (
            "WORKFLOW_FAILED",
            "order-settlement",
            "prod",
        )
        assert e["error"]["code"] == "TIMEOUT" and e["step"] == "validate-counterparty"
    assert len({s.message.payload["workflowId"] for s in steps}) == len(steps)
    assert len(state.failed_workflows) == len(steps)


def test_payments_spike_fires_repeats_then_resolves():
    state = SimState()
    steps = build("payments_error_spike", state=state, resolve_after=120)
    first = steps[0].message.payload
    assert first["status"] == "firing" and first["receiver"]
    critical = next(a for a in first["alerts"] if a["alertname"] == "HighErrorRate")
    assert critical["labels"] == {
        "alertname": "HighErrorRate", "service": "payments-api", "region": "eu-west", "severity": "critical",
    }  # fmt: skip
    assert [s.delay for s in steps][-1] == 120
    last = steps[-1].message.payload
    assert last["status"] == "resolved" and all(a["status"] == "resolved" for a in last["alerts"])
    assert all(s.message.source == "prometheus" for s in steps)
    assert state.firing == {}


def test_payments_spike_without_resolve_keeps_alerts_tracked():
    state = SimState()
    steps = build("payments_error_spike", state=state, resolve_after=0)
    assert steps[-1].message.payload["status"] == "firing"
    assert len(state.firing) == 2


def test_payments_spike_single_format_uses_brief_shape():
    steps = build("payments_error_spike", fmt="single", resolve_after=0)
    assert "alerts" not in steps[0].message.payload and steps[0].message.payload["alertname"]


def test_pricing_latency_lines_on_engine_3_over_many_instruments():
    steps = build("pricing_latency")
    lines = [s.message.payload for s in steps]
    assert {line["logger"] for line in lines} == {"pricing-engine-3"}
    assert all(line["level"] in {"WARN", "ERROR"} for line in lines)
    assert len({line["instrument"] for line in lines}) >= 4


def test_trade_limit_breach_goes_via_amqp_with_25m_eur():
    (step,) = build("trade_limit_breach")
    assert step.message.kind == "amqp" and step.message.source == "trade-gateway"
    payload = step.message.payload["payload"]
    assert (payload["desk"], payload["notional"], payload["currency"], payload["reason"]) == (
        "rates", 25_000_000, "EUR", "LIMIT_BREACH",
    )  # fmt: skip


def test_secret_leaked_is_typed_raise():
    (step,) = build("secret_leaked")
    assert step.message.kind == "raise" and step.message.source == "pr-review-agent"
    assert step.message.payload["type"] == "security.secret_leaked"


def test_poisoned_log_contains_injection():
    (step,) = build("poisoned_log")
    assert "ignore previous instructions" in step.message.payload["message"]
    assert "kubectl delete deployment payments-api" in step.message.payload["message"]


def test_storm_500_is_500_identical_errors_within_60_seconds():
    steps = build("storm_500")
    assert len(steps) == 500
    dedup_view = {
        (
            s.message.payload["workflowId"],
            s.message.payload["workflowName"],
            s.message.payload["step"],
            s.message.payload["environment"],
            s.message.payload["error"]["code"],
            s.message.payload["error"]["message"],
        )
        for s in steps
    }
    assert len(dedup_view) == 1
    assert steps[-1].delay < 60 and steps[0].delay == 0


def test_storm_intensity_scales_count():
    assert len(build("storm_500", intensity=0.1)) == 50


def test_new_schema_default_and_custom_key():
    steps = build("new_schema")
    assert all(s.message.source == "custody" and s.message.key_override is None for s in steps)
    assert steps[0].message.payload["evt"] == "custody.break"
    custom = build("new_schema", sourceKey="my-key")
    assert all(s.message.key_override == "my-key" for s in custom)


def test_resolve_all_resolves_tracked_alerts_and_recovers_workflows():
    state = SimState()
    build("payments_error_spike", state=state, resolve_after=0)
    build("settlement_outage", state=state)
    steps = build("resolve_all", state=state)
    prom = steps[0].message.payload
    assert prom["status"] == "resolved" and len(prom["alerts"]) == 2
    assert all(a["endsAt"].endswith("Z") and a["status"] == "resolved" for a in prom["alerts"])
    recovered = [s.message.payload for s in steps[1:]]
    assert recovered and all(p["type"] == "WORKFLOW_SUCCEEDED" for p in recovered)
    assert state.firing == {} and state.failed_workflows == []


def test_resolve_all_without_state_resolves_canonical_spike():
    steps = build("resolve_all")
    assert steps[0].message.payload["status"] == "resolved"
    assert steps[0].message.payload["alerts"][0]["alertname"] == "HighErrorRate"
