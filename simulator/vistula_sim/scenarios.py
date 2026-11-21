"""Scripted incidents. Each scenario builds a list of timed ``Step``s; the engine plays them."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from . import generators as g

Kind = Literal["ingest", "raise", "amqp"]


@dataclass(frozen=True)
class Message:
    source: str
    payload: Any
    kind: Kind = "ingest"
    key_override: str | None = None


@dataclass(frozen=True)
class Step:
    delay: float
    message: Message


@dataclass
class SimState:
    """What the simulated systems currently consider broken, so resolve_all can send matching resolves."""

    firing: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_workflows: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Context:
    rng: random.Random
    now: datetime
    intensity: float
    params: dict[str, str]
    state: SimState
    prometheus_format: str = "alertmanager"

    def count(self, base: int, minimum: int = 1) -> int:
        return max(minimum, round(base * self.intensity))

    def at(self, delay: float) -> datetime:
        return self.now + timedelta(seconds=delay)

    def param_float(self, name: str, default: float) -> float:
        try:
            return float(self.params[name])
        except (KeyError, ValueError):
            return default


Builder = Callable[[Context], list[Step]]


@dataclass(frozen=True)
class ScenarioDef:
    name: str
    description: str
    build: Builder


SCENARIOS: dict[str, ScenarioDef] = {}


def scenario(name: str, description: str) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        SCENARIOS[name] = ScenarioDef(name, description, fn)
        return fn

    return register


def _prom_steps(ctx: Context, alerts: list[dict[str, Any]], delay: float) -> list[Step]:
    return [
        Step(delay, Message("prometheus", payload))
        for payload in g.prometheus_payloads(alerts, ctx.prometheus_format)
    ]


@scenario("settlement_outage", "Counterparty TIMEOUT failures on order-settlement (prod), repeated")
def settlement_outage(ctx: Context) -> list[Step]:
    steps: list[Step] = []
    for i in range(ctx.count(6)):
        delay = i * 4.0
        wf_id = g.workflow_id(ctx.rng, ctx.at(delay))
        ctx.state.failed_workflows.append(("order-settlement", wf_id))
        event = g.workflow_failed(
            ctx.rng, ctx.at(delay), step="validate-counterparty", code="TIMEOUT", wf_id=wf_id
        )
        steps.append(Step(delay, Message("workflow-broker", event)))
    return steps


def _spike_alerts(ctx: Context, status: str, ends_at: datetime | None = None) -> list[dict[str, Any]]:
    error_rate = min(95, round(12 * ctx.intensity))
    started = ctx.now
    return [
        g.prom_alert(
            ctx.now,
            alertname="HighErrorRate",
            service="payments-api",
            region="eu-west",
            severity="critical",
            summary=f"Error rate {error_rate}% over the last 5 minutes",
            status=status,
            starts_at=started,
            ends_at=ends_at,
        ),
        g.prom_alert(
            ctx.now,
            alertname="HighLatencyP99",
            service="payments-api",
            region="eu-west",
            severity="warning",
            summary="p99 latency 2.8s on POST /v1/payments",
            status=status,
            starts_at=started,
            ends_at=ends_at,
        ),
    ]


@scenario(
    "payments_error_spike", "Alertmanager: critical HighErrorRate on payments-api eu-west, resolved later"
)
def payments_error_spike(ctx: Context) -> list[Step]:
    firing = _spike_alerts(ctx, "firing")
    for alert in firing:
        ctx.state.firing[alert["fingerprint"]] = alert
    steps = _prom_steps(ctx, firing, 0.0)
    for repeat in range(1, 4):  # Alertmanager repeat_interval notifications
        steps += _prom_steps(ctx, firing, repeat * 30.0)
    resolve_after = ctx.param_float("resolve_after", 600.0)
    if resolve_after > 0:
        end = ctx.at(resolve_after)
        resolved = _spike_alerts(ctx, "resolved", ends_at=end)
        for alert in resolved:
            ctx.state.firing.pop(alert["fingerprint"], None)
        steps += _prom_steps(ctx, resolved, resolve_after)
    return steps


@scenario("pricing_latency", "WARN/ERROR latency lines across instruments on pricing-engine-3")
def pricing_latency(ctx: Context) -> list[Step]:
    instruments = ["EURUSD", "GBPUSD", "USDPLN", "EURPLN", "DE10Y", "US10Y"]
    steps: list[Step] = []
    for i in range(ctx.count(12)):
        delay = i * 2.0
        line = g.pricing_latency_line(ctx.rng, ctx.at(delay), instrument=instruments[i % len(instruments)])
        steps.append(Step(delay, Message("pricing-logs", line)))
    return steps


@scenario("trade_limit_breach", "Rates desk: EUR 25,000,000 rejected for LIMIT_BREACH (trade-gateway)")
def trade_limit_breach(ctx: Context) -> list[Step]:
    return [
        Step(
            i * 3.0,
            Message(
                "trade-gateway",
                g.trade_event(
                    ctx.rng,
                    ctx.at(i * 3.0),
                    rejected=True,
                    desk="rates",
                    notional=25_000_000,
                    currency="EUR",
                    reason="LIMIT_BREACH",
                ),
                kind="amqp",
            ),
        )
        for i in range(ctx.count(1))
    ]


@scenario("secret_leaked", "pr-review-agent raises security.secret_leaked (critical)")
def secret_leaked(ctx: Context) -> list[Step]:
    return [
        Step(i * 5.0, Message("pr-review-agent", g.secret_leaked(ctx.rng, ctx.at(i * 5.0)), kind="raise"))
        for i in range(ctx.count(1))
    ]


@scenario("poisoned_log", "Log line containing a prompt injection (kubectl delete deployment ...)")
def poisoned_log(ctx: Context) -> list[Step]:
    return [Step(0.0, Message("pricing-logs", g.poisoned_log(ctx.rng, ctx.now)))]


@scenario("storm_500", "500 identical errors in 60 s (dedup test: expect 1 alert, count 500)")
def storm_500(ctx: Context) -> list[Step]:
    total = ctx.count(500)
    duration = ctx.param_float("duration", 60.0)
    spacing = duration / total
    wf_id = "WF-STORM-00001"
    steps = []
    for i in range(total):
        delay = i * spacing
        event = g.workflow_failed(
            ctx.rng,
            ctx.at(delay),
            step="ledger-commit",
            code="DB_POOL_EXHAUSTED",
            wf_id=wf_id,
        )
        steps.append(Step(delay, Message("workflow-broker", event)))
    return steps


@scenario("new_schema", "A system we have never seen: custody.break event (onboard it with AI mapping)")
def new_schema(ctx: Context) -> list[Step]:
    override = ctx.params.get("sourceKey") or None
    return [
        Step(i * 2.0, Message("custody", g.custody_break(ctx.rng, ctx.at(i * 2.0)), key_override=override))
        for i in range(ctx.count(3))
    ]


@scenario("resolve_all", "Send resolved notifications for everything the simulator knows is firing")
def resolve_all(ctx: Context) -> list[Step]:
    steps: list[Step] = []
    firing = list(ctx.state.firing.values())
    if not firing:  # nothing tracked (restart): resolve the canonical spike anyway
        firing = _spike_alerts(ctx, "firing")
    resolved = [{**a, "status": "resolved", "endsAt": g.iso(ctx.now)} for a in firing]
    steps += _prom_steps(ctx, resolved, 0.0)
    for i, (workflow, wf_id) in enumerate(ctx.state.failed_workflows[-10:]):
        event = g.workflow_succeeded(ctx.rng, ctx.at(1.0 + i), workflow=workflow, wf_id=wf_id)
        steps.append(Step(1.0 + i * 0.5, Message("workflow-broker", event)))
    ctx.state.firing.clear()
    ctx.state.failed_workflows.clear()
    return steps


# (scenario, intensity, weight) for random background incidents
INCIDENT_POOL: list[tuple[str, float, int]] = [
    ("pricing_latency", 0.5, 4),
    ("trade_limit_breach", 1.0, 3),
    ("settlement_outage", 0.5, 3),
    ("payments_error_spike", 1.0, 1),
]
