"""Presets: Alertmanager splitting + resolved detection, and every preset's mapping on its public sample."""

from __future__ import annotations

import copy

import pytest

from app.pipeline.envelope import build_envelope
from app.pipeline.normalize import normalize
from app.pipeline.presets import PRESETS, RESOLVE, get_preset, split_events
from app.pipeline.presets.alertmanager import is_resolved
from app.pipeline.presets.alertmanager import split as am_split
from app.pipeline.presets.mappings import PRESET_MAPPINGS, preset_mapping


def am_alert(name: str, service: str, severity: str = "critical", status: str = "firing", **extra) -> dict:
    return {
        "status": status,
        "labels": {"alertname": name, "service": service, "severity": severity, "environment": "prod"},
        "annotations": {"summary": f"{name} on {service}"},
        "startsAt": "2026-10-23T09:13:00.000Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": f"fp-{name}-{service}",
        **extra,
    }


def am_group(*alerts: dict, status: str = "firing") -> dict:
    return {
        "receiver": "vistula-alerting",
        "status": status,
        "alerts": list(alerts),
        "groupLabels": {},
        "commonLabels": {},
        "externalURL": "http://alertmanager:9093",
        "version": "4",
        "groupKey": "{}:{}",
    }


def source_for(preset: str) -> dict:
    return {"name": f"{preset}-src", "default_severity": "warning", "mapping": preset_mapping(preset)}


def normalized(preset: str, raw: dict):
    return normalize(build_envelope(source_id="s", raw=raw, priority=3), source_for(preset)).meta


# ---- Alertmanager split ----------------------------------------------------------------------------


def test_grouped_payload_with_three_alerts_becomes_three_events():
    group = am_group(am_alert("A", "x"), am_alert("B", "y"), am_alert("C", "z"))
    events = am_split(group)
    assert len(events) == 3
    assert [e["alertname"] for e in events] == ["A", "B", "C"]
    for event, original in zip(events, group["alerts"], strict=True):
        assert event["labels"] == original["labels"] and event["fingerprint"] == original["fingerprint"]
        assert event["receiver"] == "vistula-alerting" and event["groupKey"] == "{}:{}"
        assert "alerts" not in event
    assert group == am_group(am_alert("A", "x"), am_alert("B", "y"), am_alert("C", "z"))


def test_alert_without_status_inherits_the_group_status():
    group = am_group({"labels": {"alertname": "A"}, "annotations": {}}, status="resolved")
    (event,) = am_split(group)
    assert event["status"] == "resolved" and is_resolved(event)


def test_alert_status_wins_over_group_status():
    group = am_group(am_alert("A", "x", status="firing"), status="resolved")
    assert am_split(group)[0]["status"] == "firing"


@pytest.mark.parametrize(
    "raw",
    [
        {"alertname": "A", "labels": {"alertname": "A"}, "status": "firing"},  # single-alert brief shape
        {"alerts": []},
        {"alerts": "nope"},
        {"alerts": [1, 2]},
        {"foo": "bar"},
    ],
)
def test_other_shapes_pass_through_unchanged(raw):
    before = copy.deepcopy(raw)
    assert am_split(raw) == [raw] and raw == before


def test_existing_top_level_alertname_is_kept():
    (event,) = am_split(am_group({**am_alert("A", "x"), "alertname": "Custom"}))
    assert event["alertname"] == "Custom"


@pytest.mark.parametrize(
    ("status", "resolved"), [("resolved", True), ("Resolved ", True), ("firing", False), (None, False)]
)
def test_is_resolved(status, resolved):
    assert is_resolved({"status": status}) is resolved


def test_registry_hooks():
    am = get_preset("alertmanager")
    assert am.on_event({"status": "resolved"}) == RESOLVE and am.on_event({"status": "firing"}) is None
    assert am.resolve_note == "Resolved by source (Alertmanager status=resolved)"
    assert set(PRESETS) == set(PRESET_MAPPINGS) == {"alertmanager", "sentry", "datadog", "cloudevents"}
    for name in ("sentry", "datadog", "cloudevents"):
        preset = get_preset(name)
        assert preset.on_event is None and preset.split({"a": 1}) == [{"a": 1}]
    assert get_preset(None) is None and get_preset("nope") is None
    assert split_events("nope", {"a": 1}) == [{"a": 1}] and split_events(None, {"a": 1}) == [{"a": 1}]
    assert len(split_events("alertmanager", am_group(am_alert("A", "x"), am_alert("B", "y")))) == 2


# ---- mappings normalize their sample payloads ---------------------------------------------------------


def test_alertmanager_mapping_on_a_split_alert():
    (event,) = am_split(am_group(am_alert("HighErrorRate", "payments-api", "critical")))
    meta = normalized("alertmanager", event)
    assert meta["schema"] == "HighErrorRate"
    assert (meta["severity"], meta["service"], meta["environment"]) == ("critical", "payments-api", "prod")
    assert meta["type"] == "HighErrorRate" and meta["title"] == "HighErrorRate on payments-api"
    assert meta["timestamp"] == "2026-10-23T09:13:00.000Z"


def test_alertmanager_mapping_on_the_single_alert_brief_shape():
    brief = {
        "alertname": "HighErrorRate",
        "labels": {"service": "payments-api", "region": "eu-west", "severity": "critical"},
        "annotations": {"summary": "Error rate 12% over the last 5 minutes"},
        "startsAt": "2026-10-23T09:13:00Z",
        "status": "firing",
    }
    meta = normalized("alertmanager", brief)
    assert (meta["severity"], meta["service"], meta["type"]) == ("critical", "payments-api", "HighErrorRate")
    assert meta["title"] == "Error rate 12% over the last 5 minutes"


def test_alertmanager_preset_uses_the_documented_label_paths():
    fields = PRESET_MAPPINGS["alertmanager"]["fields"]
    assert fields["severity"] == "labels.severity" and fields["service"] == "labels.service"
    assert fields["type"] == "alertname" and fields["title"] == "annotations.summary"


def test_sentry_mapping():
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "title": "ZeroDivisionError: division by zero",
                "level": "fatal",
                "project": "payments-api",
                "environment": "prod",
                "timestamp": 1790155980.5,
            },
            "triggered_rule": "High volume errors",
        },
    }
    meta = normalized("sentry", payload)
    assert (meta["severity"], meta["service"], meta["environment"]) == ("critical", "payments-api", "prod")
    assert meta["title"] == "ZeroDivisionError: division by zero" and meta["type"] == "High volume errors"
    assert meta["timestamp"].startswith("2026-")


def test_datadog_mapping():
    payload = {
        "id": "4815162342",
        "title": "[Triggered on {host:pricing-1}] CPU high",
        "event_type": "query_alert_monitor",
        "alert_type": "error",
        "hostname": "pricing-1",
        "env": "prod",
        "date": 1790155980000,
    }
    meta = normalized("datadog", payload)
    assert (meta["severity"], meta["service"], meta["environment"]) == ("error", "pricing-1", "prod")
    assert meta["type"] == "query_alert_monitor" and meta["title"].endswith("CPU high")
    assert meta["timestamp"].startswith("2026-")


def test_datadog_success_is_info():
    assert normalized("datadog", {"title": "Recovered", "alert_type": "success"})["severity"] == "info"


def test_cloudevents_mapping():
    payload = {
        "specversion": "1.0",
        "id": "A234-1234-1234",
        "source": "trade-gateway",
        "type": "com.vistula.trade.rejected",
        "time": "2026-10-23T09:13:00Z",
        "datacontenttype": "application/json",
        "data": {"severity": "WARNING", "environment": "prod", "message": "Trade T-1 rejected"},
    }
    meta = normalized("cloudevents", payload)
    assert (meta["severity"], meta["service"], meta["environment"]) == ("warning", "trade-gateway", "prod")
    assert meta["type"] == "com.vistula.trade.rejected" and meta["title"] == "Trade T-1 rejected"
    assert meta["schema"] == "com.vistula.trade.rejected"
    assert meta["timestamp"] == "2026-10-23T09:13:00.000Z"


# ---- the seeded sources agree with the presets ---------------------------------------------------------


async def test_seeded_prometheus_mapping_is_the_alertmanager_preset(seeded):
    row = await seeded.fetchrow(
        "SELECT preset, mapping, mapping_status FROM sources WHERE name = 'prometheus'"
    )
    assert row["preset"] == "alertmanager" and row["mapping_status"] == "accepted"
    assert row["mapping"] == preset_mapping("alertmanager")


async def test_seeded_trade_gateway_is_a_rabbitmq_source_on_the_vistula_queue(seeded):
    row = await seeded.fetchrow("SELECT type, config FROM sources WHERE name = 'trade-gateway'")
    assert row["type"] == "rabbitmq" and row["config"]["queue"] == "vistula.trade-gateway"
    assert row["config"]["url"].startswith("amqp")
