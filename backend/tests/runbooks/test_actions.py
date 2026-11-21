"""Built-in actions against the in-process mockgw."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.core.ids import uuid7
from app.pipeline.fields import EventView
from app.runbooks.actions import registry
from app.runbooks.actions._http import validate_external_url
from app.runbooks.actions.http_get import resolve_url

from .helpers import WORKFLOW_FAILED, make_alert, user


def ctx(alert=None, conn=None):
    alert = alert or {"id": uuid7(), "severity": "critical"}
    return registry.ActionContext(
        alert=alert, ev=EventView(WORKFLOW_FAILED), actor={}, step_id="s1", run_id=uuid7(), conn=conn
    )


async def run(name: str, params: dict, **kw):
    action = registry.get_action(name)
    return await action.run(action.params_schema.model_validate(params), ctx(**kw))


def test_registry_has_every_builtin_with_default_risk():
    risks = {n: a.default_risk for n, a in registry.actions().items()}
    assert risks == {
        "http_get": "low",
        "retry_workflow": "medium",
        "restart_service": "high",
        "failover_region": "high",
        "rollback_deploy": "high",
        "open_ticket": "low",
        "webhook": "medium",
        "escalate": "low",
        "notify_text": "low",
    }
    assert (
        registry.describe("retry_workflow", {"workflowId": "{{workflowId}}"})
        == "Retry workflow {{workflowId}}"
    )
    assert registry.describe("nope", {}) == "nope"


def test_register_decorator_adds_an_action():
    from pydantic import BaseModel

    class Params(BaseModel):
        pass

    @registry.register
    class Custom:
        name = "custom_test_action"
        params_schema = Params
        default_risk = "low"
        label = "Custom"
        affects_trust = False

        async def run(self, params, ctx):
            return registry.ActionResult(True, "ok")

    try:
        assert registry.get_action("custom_test_action") is not None
    finally:
        registry._ACTIONS.pop("custom_test_action")


def test_http_get_maps_dotless_hosts_to_the_gateway(mockgw_server):
    assert (
        resolve_url("http://counterparty-service/health")
        == f"{mockgw_server}/svc/counterparty-service/health"
    )
    assert resolve_url("http://billing/ping?x=1") == f"{mockgw_server}/svc/billing/ping?x=1"
    assert resolve_url("https://status.example.com/health") == "https://status.example.com/health"


@pytest.mark.parametrize(
    "url", ["ftp://x.example.com", "http://localhost/x", "http://127.0.0.1/", "http://169.254.169.254/"]
)
def test_urls_to_internal_or_odd_targets_are_refused(url):
    with pytest.raises(ValueError):
        validate_external_url(url)


async def test_http_get_ok_and_unhealthy(gw):
    ok = await run("http_get", {"url": "http://counterparty-service/health"})
    assert ok.ok and "HTTP 200" in ok.output
    await gw.post("/admin/fail", json={"channel": "svc:counterparty-service", "seconds": 30})
    down = await run("http_get", {"url": "http://counterparty-service/health"})
    assert not down.ok and "HTTP 503" in down.output
    assert not (await run("http_get", {"url": "http://localhost/x"})).ok


async def test_http_get_connection_error_is_a_failed_result(mockgw_server):
    result = await run("http_get", {"url": "http://nonexistent-host.invalid/health"})
    assert not result.ok and "failed" in result.output


async def test_retry_workflow_and_failure(gw):
    assert (await run("retry_workflow", {"workflowId": "WF-1"})).ok
    state = (await gw.get("/admin/state")).json()
    assert state["workflows"][-1]["result"] == "retried"
    await gw.post("/admin/fail", json={"channel": "workflow", "count": 1})
    failed = await run("retry_workflow", {"workflowId": "WF-1"})
    assert not failed.ok and "HTTP 500" in failed.output


async def test_workflow_id_cannot_inject_a_path(gw):
    await run("retry_workflow", {"workflowId": "../../admin/reset"})
    state = (await gw.get("/admin/state")).json()
    assert state["workflows"] == [] or state["workflows"][-1]["workflowId"] != "reset"


async def test_restart_failover_rollback_and_ticket(gw):
    assert (await run("restart_service", {"service": "pricing-engine-3"})).ok
    assert (await run("failover_region", {"region": "eu-central"})).ok
    assert (await run("rollback_deploy", {"service": "payments-api"})).ok
    assert (await run("open_ticket", {"summary": "Settlement broken"})).ok
    state = (await gw.get("/admin/state")).json()
    assert state["restarts"][0]["service"] == "pricing-engine-3"
    assert state["failovers"][0]["region"] == "eu-central"
    assert len(state["rollbacks"]) == 1
    assert state["tickets"][0]["summary"] == "Settlement broken"
    await gw.post("/admin/fail", json={"channel": "restart", "count": 1})
    assert not (await run("restart_service", {"service": "x"})).ok


async def test_webhook_is_signed_and_validated(monkeypatch):
    sent = {}

    async def fake_call(method, url, **kwargs):
        sent.update(method=method, url=url, **kwargs)
        return registry.ActionResult(True, "ok")

    monkeypatch.setattr("app.runbooks.actions.webhook.call", fake_call)
    result = await run("webhook", {"url": "https://hooks.example.com/x", "body": {"a": 1}, "secret": "s3"})
    assert result.ok
    expected = "sha256=" + hmac.new(b"s3", sent["content"], hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Signature"] == expected
    assert not (await run("webhook", {"url": "http://localhost/x"})).ok


async def test_notify_text_is_a_noop():
    result = await run("notify_text", {"text": "Posted in #payments"})
    assert result.ok and result.output == "Posted in #payments"


async def test_escalate_pages_the_target_teams_oncall(seeded, gw):
    team_id = uuid7()
    await seeded.execute(
        "INSERT INTO teams (id, slug, name) VALUES ($1, 'escalate-target-team', 'Counterparty')", team_id
    )
    await seeded.execute(
        "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", team_id, user("anna@demo")
    )
    alert = await make_alert(seeded)
    context = ctx(alert, seeded)
    action = registry.get_action("escalate")
    result = await action.run(action.params_schema.model_validate({"team": "escalate-target-team"}), context)
    assert result.ok and "Anna Kowalska" in result.output
    rows = await seeded.fetch("SELECT * FROM deliveries WHERE alert_id = $1", alert["id"])
    assert [(r["channel"], r["kind"], r["recipient_user_id"]) for r in rows] == [
        ("sms", "hard", user("anna@demo"))
    ]
    assert len(context.outbox_ids) == 1
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM timeline WHERE alert_id = $1 AND type = 'escalated'", alert["id"]
        )
        == 1
    )


async def test_escalate_unknown_team_falls_back_to_domain_roots(seeded):
    alert = await make_alert(seeded)
    context = ctx(alert, seeded)
    action = registry.get_action("escalate")
    result = await action.run(action.params_schema.model_validate({"team": "no-such-team"}), context)
    assert result.ok and "domain roots" in result.output and "Stefan Settlements" in result.output


async def test_escalate_without_connection_fails():
    result = await run("escalate", {})
    assert not result.ok
