"""Deterministic mapping draft: field names, severity maps, every path exists in the sample."""

import pytest

from app.pipeline.fields import MISSING, lookup
from app.pipeline.mapping_heuristic import detect_schema, suggest_mapping_heuristic
from app.pipeline.mapping_presets import PRESET_MAPPINGS, preset_mapping

from .test_seeded import ALERTMANAGER_EVENT, PRICING_EVENT, SETTLEMENTS_EVENT, TRADE_EVENT


@pytest.mark.parametrize("sample", [SETTLEMENTS_EVENT, ALERTMANAGER_EVENT, TRADE_EVENT, PRICING_EVENT])
def test_every_proposed_path_exists_in_the_sample(sample):
    mapping = suggest_mapping_heuristic(sample)
    assert mapping["status"] == "draft"
    assert mapping["explanation"].startswith("AI unavailable, heuristic draft.")
    assert mapping["fields"]
    for path in mapping["fields"].values():
        assert lookup(sample, path) is not MISSING


def test_workflow_event_mapping():
    fields = suggest_mapping_heuristic(SETTLEMENTS_EVENT)["fields"]
    assert fields["severity"] == "severity"
    assert fields["environment"] == "environment"
    assert fields["timestamp"] == "timestamp"
    assert fields["service"] == "workflowName"
    assert fields["type"] == "type"
    assert fields["error_code"] == "error.code"
    assert fields["title"] == "error.message"


def test_alertmanager_event_mapping():
    mapping = suggest_mapping_heuristic(ALERTMANAGER_EVENT)
    assert mapping["fields"]["severity"] == "labels.severity"
    assert mapping["fields"]["service"] == "labels.service"
    assert mapping["fields"]["title"] == "annotations.summary"
    assert mapping["fields"]["timestamp"] == "startsAt"
    assert mapping["fields"]["type"] == "alertname"
    assert mapping["detectedSchema"] == "HighErrorRate"
    assert (
        mapping["severityValueMap"]["critical"] == "critical"
        or mapping["severityValueMap"]["CRITICAL"] == "critical"
    )


def test_trade_event_mapping_uses_meta_paths():
    mapping = suggest_mapping_heuristic(TRADE_EVENT)
    assert mapping["fields"]["service"] == "meta.producer"
    assert mapping["fields"]["timestamp"] == "meta.ts"
    assert mapping["fields"]["type"] == "meta.schema"
    assert "severity" not in mapping["fields"] and "severityValueMap" not in mapping
    assert mapping["detectedSchema"] == "trade.rejected.v2"


def test_pricing_log_mapping_and_severity_words():
    mapping = suggest_mapping_heuristic(PRICING_EVENT)
    assert mapping["fields"]["severity"] == "level" and mapping["fields"]["service"] == "logger"
    assert mapping["fields"]["title"] == "message"
    assert (
        mapping["severityValueMap"]["WARN"] == "warning"
        and mapping["severityValueMap"]["FATAL"] == "critical"
    )


def test_numeric_priority_and_unknown_word_values():
    assert suggest_mapping_heuristic({"priority": 1})["severityValueMap"]["1"] == "critical"
    odd = suggest_mapping_heuristic({"severity": "Warn"})
    assert odd["severityValueMap"]["Warn"] == "warning"


def test_shallower_and_better_named_keys_win():
    sample = {
        "detail": {"level": "x"},
        "level": "ERROR",
        "nested": {"deep": {"message": "m"}},
        "message": "top",
    }
    fields = suggest_mapping_heuristic(sample)["fields"]
    assert fields["severity"] == "level" and fields["title"] == "message"


def test_unrecognised_event_yields_empty_mapping_and_hashed_schema():
    mapping = suggest_mapping_heuristic({"foo": 1, "bar": {"baz": True}})
    assert mapping["fields"] == {}
    assert mapping["detectedSchema"].startswith("keys:")
    assert "no known field names" in mapping["explanation"]
    assert detect_schema({"foo": 1}) == detect_schema({"foo": 2}) != detect_schema({"bar": 1})


def test_array_leaves_and_depth_limit():
    sample = {
        "alerts": [{"labels": {"severity": "warning", "service": "x"}}],
        "a": {"b": {"c": {"d": {"e": {"f": {"severity": "z"}}}}}},
    }
    fields = suggest_mapping_heuristic(sample)["fields"]
    assert fields["severity"] == "alerts.0.labels.severity"


def test_presets_cover_every_contract_preset():
    assert set(PRESET_MAPPINGS) == {"alertmanager", "sentry", "datadog", "cloudevents"}
    for name in PRESET_MAPPINGS:
        mapping = preset_mapping(name)
        assert mapping["status"] == "accepted" and mapping["fields"]["severity"]
    assert preset_mapping(None) is None and preset_mapping("nope") is None
    first = preset_mapping("sentry")
    first["fields"]["severity"] = "mutated"
    assert preset_mapping("sentry")["fields"]["severity"] != "mutated"
