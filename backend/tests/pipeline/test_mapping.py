from datetime import UTC, datetime

import pytest

from app.pipeline.mapping import apply_mapping, normalize_severity, parse_timestamp

RECEIVED = datetime(2026, 11, 21, 9, 14, 2, 331000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-23T09:14:02.331Z", datetime(2026, 10, 23, 9, 14, 2, 331000, tzinfo=UTC)),
        ("2026-10-23T11:14:02+02:00", datetime(2026, 10, 23, 9, 14, 2, tzinfo=UTC)),
        ("2026-10-23T09:14:02", datetime(2026, 10, 23, 9, 14, 2, tzinfo=UTC)),
        (1792833245000, datetime.fromtimestamp(1792833245, UTC)),
        (1792833245, datetime.fromtimestamp(1792833245, UTC)),
        ("1792833245000", datetime.fromtimestamp(1792833245, UTC)),
        (1792833245.5, datetime.fromtimestamp(1792833245.5, UTC)),
    ],
)
def test_parse_timestamp_formats(value, expected):
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", [None, True, "", "  ", "yesterday", [], {}, 1e30])
def test_parse_timestamp_garbage(value):
    assert parse_timestamp(value) is None


def test_normalize_severity_value_map_beats_builtin():
    value_map = {"WARN": "warning", "SEV-9": "critical", "info": "error"}
    assert normalize_severity("WARN", value_map) == "warning"
    assert normalize_severity("warn", value_map) == "warning"
    assert normalize_severity("SEV-9", value_map) == "critical"
    assert normalize_severity("info", value_map) == "error"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("ERROR", "error"), ("Fatal", "critical"), ("p3", "warning"), ("DEBUG", "info"), (" crit ", "critical")],
)
def test_normalize_severity_builtin_words(value, expected):
    assert normalize_severity(value) == expected


@pytest.mark.parametrize("value", [None, "", "bogus", True, [], {"a": 1}])
def test_normalize_severity_unknown(value):
    assert normalize_severity(value) is None


def apply(raw, mapping=None, default="warning"):
    return apply_mapping(
        raw, mapping, schema="s", source_name="my-source", default_severity=default, received_at=RECEIVED
    )


def test_mapping_fields_are_applied():
    raw = {
        "labels": {"sev": "ERR"},
        "meta": {"producer": "gw", "ts": 1792833245000},
        "env": "prod",
        "annotations": {"summary": "boom"},
        "error": {"code": 504},
    }
    mapping = {
        "fields": {
            "severity": "labels.sev",
            "service": "meta.producer",
            "environment": "env",
            "timestamp": "meta.ts",
            "title": "annotations.summary",
            "type": "meta.kind",
            "error_code": "error.code",
        },
        "severityValueMap": {"ERR": "error"},
    }
    meta = apply(raw, mapping)
    assert meta["severity"] == "error"
    assert meta["service"] == "gw"
    assert meta["environment"] == "prod"
    assert meta["title"] == "boom"
    assert meta["error_code"] == "504"
    assert meta["type"] is None
    assert meta["timestamp"] == "2026-10-24T09:14:05.000Z"
    assert meta["source"] == "gw" and meta["source_name"] == "my-source" and meta["schema"] == "s"


def test_unmapped_event_uses_fallback_paths_then_defaults():
    raw = {"level": "ERROR", "logger": "pricing-engine-3", "message": "slow", "time": "2026-10-23T09:14:02Z"}
    meta = apply(raw)
    assert meta["severity"] == "error"
    assert meta["service"] == "pricing-engine-3"
    assert meta["source"] == "pricing-engine-3"
    assert meta["title"] == "slow"
    assert meta["timestamp"] == "2026-10-23T09:14:02.000Z"


def test_missing_everything_falls_back_to_source_default_and_received_at():
    meta = apply({"foo": 1}, default="info")
    assert meta["severity"] == "info"
    assert meta["source"] == "my-source"
    assert meta["service"] is None and meta["title"] is None
    assert meta["timestamp"] == "2026-11-21T09:14:02.331Z"


def test_unrecognized_severity_value_keeps_default():
    assert apply({"severity": "purple"}, default="error")["severity"] == "error"
    assert apply({"severity": "purple"}, default="")["severity"] == "warning"


def test_mapped_path_missing_falls_back():
    meta = apply({"level": "WARN"}, {"fields": {"severity": "nope.nothing"}})
    assert meta["severity"] == "warning"
