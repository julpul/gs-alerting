"""AI drafts: schema mapping, rule copilot (+ dry-run through the API), runbook text parsing."""

from __future__ import annotations

import copy
import sys
import types

import pytest

from app.ai.llm import AIUnavailable
from app.ai.mapping import MappingAnswer, suggest_mapping, validate_mapping
from app.ai.rule_copilot import draft_rule, field_paths
from app.ai.runbook_parse import parse_text, registry_actions, split_plain
from app.core.errors import AppError
from app.core.ids import stable_id
from app.core.scope import load_scope
from app.pipeline.rules.compiler import compile_rule
from tests.api.alerts_helpers import user

PROMETHEUS = str(stable_id("source", "prometheus"))
SAMPLE = {
    "labels": {"severity": "CRITICAL", "alertname": "HighErrorRate", "service": "payments-api"},
    "annotations": {"summary": "5xx above 5%"},
    "startsAt": "2026-11-21T10:00:00Z",
    "alerts": [{"labels": {"env": "prod"}}],
}
RULE = {
    "name": "Payments critical to on-call",
    "teamId": "team-payments",
    "match": {"op": "and", "conditions": [{"field": "_severity", "operator": "eq", "value": "critical"}]},
    "routing": {"hard": [{"kind": "oncall", "id": "team-payments"}], "soft": []},
    "dedup": {"fields": ["_service"], "windowSeconds": 300},
}


async def test_mapping_drops_paths_that_do_not_exist_in_the_sample(llm):
    llm.responses = {
        "fast": {
            "fields": {
                "severity": "labels.severity",
                "service": "labels.service",
                "environment": "alerts.0.labels.env",
                "title": "annotations.message",  # not in the sample
                "error_code": "labels.code",  # not in the sample
                "bogus": "labels.severity",  # not a mapping target
                "timestamp": None,
            },
            "severityValueMap": {"CRITICAL": "critical", "WARN": "loud"},
            "explanation": "Prometheus style.",
        }
    }
    out = await suggest_mapping(SAMPLE, {"name": "prom", "type": "webhook", "preset": None})
    assert out["fields"] == {
        "severity": "labels.severity",
        "service": "labels.service",
        "environment": "alerts.0.labels.env",
    }
    assert out["severityValueMap"] == {"CRITICAL": "critical"}  # "loud" is not a severity
    assert "annotations.message" in out["explanation"] and "labels.code" in out["explanation"]
    assert out["detectedSchema"].startswith("HighErrorRate") or out["detectedSchema"].startswith("keys:")
    assert "<untrusted_event>" in llm.calls[0]["user"] and llm.calls[0]["tier"] == "fast"


async def test_mapping_without_any_valid_path_is_unavailable(llm):
    llm.responses = {"fast": {"fields": {"severity": "nope.nope"}}}
    with pytest.raises(AIUnavailable):
        await suggest_mapping(SAMPLE, {})
    assert validate_mapping(MappingAnswer(), SAMPLE)["fields"] == {}


async def test_mapping_endpoint_uses_the_llm_and_falls_back_to_the_heuristic(client, auth_headers, llm):
    anna = await auth_headers("anna@demo")
    url = f"/v1/sources/{PROMETHEUS}/mapping/suggest"
    llm.responses = {"fast": {"fields": {"severity": "labels.severity"}, "explanation": "AI says so"}}
    resp = await client.post(url, json={"sample": SAMPLE}, headers=anna)
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft" and resp.json()["fields"] == {"severity": "labels.severity"}
    assert resp.json()["explanation"] == "AI says so"
    llm.fail = True
    resp = await client.post(url, json={"sample": SAMPLE}, headers=anna)
    assert resp.status_code == 200 and resp.json()["explanation"].startswith(
        "AI unavailable, heuristic draft."
    )


async def test_rule_copilot_output_compiles_and_the_endpoint_returns_a_dry_run(client, auth_headers, llm):
    anna = await auth_headers("anna@demo")
    llm.responses = {
        "smart": {"rule": {**RULE, "id": "ignored", "version": 9}, "explanation": "Pages on-call."}
    }
    resp = await client.post(
        "/v1/rules/from-text",
        json={"text": "page payments on-call for critical alerts", "teamId": "team-payments"},
        headers=anna,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["explanation"] == "Pages on-call." and "id" not in body["draft"]
    assert body["draft"]["teamId"] == str(stable_id("team", "team-payments"))
    assert {"evaluated", "matched", "alertsAfterDedup", "notifications"} <= set(body["dryRun"])
    call = llm.calls[0]
    assert call["tier"] == "smart" and "<untrusted_event>" in call["user"]
    assert "team-payments" in call["user"] and "anna@demo" in call["user"]  # scope catalog
    assert "team-settlements" not in call["user"]  # teams outside the caller's scope are not disclosed

    llm.fail = True
    resp = await client.post(
        "/v1/rules/from-text", json={"text": "x", "teamId": "team-payments"}, headers=anna
    )
    assert resp.status_code == 503 and resp.json()["code"] == "ai_unavailable"


async def test_rule_copilot_repairs_once_then_gives_up_and_enforces_scope(seeded, llm):
    async with seeded.acquire() as conn:
        scope = await load_scope(conn, user("anna@demo"))
        bad = copy.deepcopy(RULE)
        bad["match"]["conditions"][0]["operator"] = "approximately"
        llm.responses = [{"rule": bad}, {"rule": RULE, "explanation": ""}]
        rule, explanation = await draft_rule("critical to on-call", scope, conn)
        compile_rule(rule)
        assert explanation and len(llm.calls) == 2 and "rejected" in llm.calls[1]["user"]
        assert "match.conditions[0].operator" in llm.calls[1]["user"]

        llm.responses = [{"rule": bad}, {"rule": bad}]
        with pytest.raises(AIUnavailable, match="valid rule"):
            await draft_rule("critical", scope, conn)

        llm.responses = [{"rule": {**RULE, "teamId": "team-settlements"}}]
        with pytest.raises(AppError) as err:
            await draft_rule("critical", scope, conn)
        assert err.value.status_code == 422

        llm.responses = [{"rule": {**RULE, "teamId": "team-settlements"}}]
        rule, _ = await draft_rule("critical", scope, conn, team_id=stable_id("team", "team-payments"))
        assert rule.team_id == stable_id("team", "team-payments")  # an explicit team wins


def test_field_paths_lists_leaves_without_values():
    assert field_paths({"a": {"b": 1}, "l": [{"c": 2}], "d": None}) == ["a.b", "l.0.c", "d"]


async def test_runbook_parse_keeps_only_registry_actions_and_forces_approval(llm):
    assert "retry_workflow" in registry_actions()
    llm.responses = {
        "smart": {
            "steps": [
                {"text": "Check the workflow engine health", "kind": "check"},
                {"text": "Retry the workflow", "kind": "action", "action": "retry_workflow", "risk": "low"},
                {"text": "Fail over region", "kind": "action", "action": "failover_to_mars", "risk": "high"},
                {"text": "Restart service", "kind": "action", "action": "restart_service", "risk": "high"},
                {"text": "  ", "kind": "text"},
            ]
        }
    }
    steps = await parse_text("1. Check the engine\n2. Retry\n3. Fail over\n4. Restart")
    assert [s["id"] for s in steps] == ["s1", "s2", "s3", "s4"] and [s["order"] for s in steps] == [
        1,
        2,
        3,
        4,
    ]
    assert steps[0]["kind"] == "check" and steps[0]["action"] is None
    assert (
        steps[1]["kind"] == "action"
        and steps[1]["action"] == "retry_workflow"
        and steps[1]["approval"] == "none"
    )
    assert steps[2]["kind"] == "text" and steps[2]["action"] is None and steps[2]["approval"] == "none"
    assert steps[3]["approval"] == "required" and steps[3]["risk"] == "high"
    assert llm.calls[0]["tier"] == "smart" and "<untrusted_event>" in llm.calls[0]["user"]
    assert await parse_text("   ") == []


async def test_runbook_parse_degrades_to_plain_steps_without_the_model(llm):
    llm.fail = True
    steps = await parse_text("1. First check logs\n2. Then restart the consumer\n- and tell the channel")
    assert [s["text"] for s in steps] == [
        "First check logs",
        "Then restart the consumer",
        "and tell the channel",
    ]
    assert all(s["kind"] == "text" and s["action"] is None for s in steps)
    assert [s.text for s in split_plain("para one\n\npara two")] == ["para one", "para two"]


def test_registry_names_come_from_the_runbooks_module_when_present(monkeypatch):
    module = types.ModuleType("app.runbooks.actions.registry")
    module.REGISTRY = {"zeta": object(), "alpha": object()}
    monkeypatch.setitem(sys.modules, "app.runbooks.actions", types.ModuleType("app.runbooks.actions"))
    monkeypatch.setitem(sys.modules, "app.runbooks.actions.registry", module)
    sys.modules["app.runbooks.actions"].registry = module
    assert registry_actions() == ["alpha", "zeta"]
    del module.REGISTRY
    module.names = lambda: ["b", "a"]
    assert registry_actions() == ["a", "b"]
