"""Unit tests: text log parsing (`textlog`), wildcard paths (`fields`), wildcard rule semantics and templates."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.routers.ingest import body_events
from app.core.errors import AppError
from app.pipeline.fields import MISSING, AnyOf, EventView, get_path, lookup
from app.pipeline.presets import detect_preset, split_events
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import parse_rule
from app.pipeline.rules.templates import render
from app.pipeline.textlog import is_json_content_type, parse_line, parse_text

S3 = (
    '2026-11-20T14:05:09.120Z WARN pricing-engine-3 [quote-cache] instrument=EURUSD latency_ms=1840 '
    'msg="cache miss storm"'
)
LEGS = {
    "legs": [
        {"legId": 1, "pair": "EURUSD", "status": "FILLED", "qty": 5000000},
        {"legId": 2, "pair": "GBPUSD", "status": "REJECTED", "qty": 12000000},
        {"legId": 3, "pair": "USDJPY", "status": "FILLED"},
    ],
    "tags": ["prod", "eu"],
}


def test_s3_line_gets_timestamp_level_and_pairs():
    event = parse_line(S3)
    assert event == {
        "message": S3,
        "timestamp": "2026-11-20T14:05:09.120Z",
        "level": "WARN",
        "instrument": "EURUSD",
        "latency_ms": 1840,
        "msg": "cache miss storm",
    }


def test_levels_numbers_and_reserved_keys():
    assert parse_line("ERROR boom code=E42 ratio=0.5")["level"] == "ERROR"
    assert parse_line("ERROR boom code=E42 ratio=0.5")["ratio"] == 0.5
    assert parse_line("[info] started")["level"] == "INFO"
    assert parse_line("error: disk full")["level"] == "ERROR"
    assert "level" not in parse_line("alert fired for db-07")  # a lower-case word is not a level
    assert parse_line("message=x stack=y k=v") == {"message": "message=x stack=y k=v", "k": "v"}
    assert parse_line('k="say \\"hi\\""')["k"] == 'say "hi"'
    assert parse_line("2026-11-20 14:05:09,120 INFO ok")["timestamp"] == "2026-11-20 14:05:09,120"


def test_syslog_stamp_tag_and_priority():
    event = parse_line("<11>Nov 20 14:05:09 fx-gw-02 fix-gateway[28114]: Session DROPPED")
    assert event["timestamp"] == "Nov 20 14:05:09"
    assert (event["host"], event["app"], event["pid"]) == ("fx-gw-02", "fix-gateway", "28114")
    assert event["level"] == "ERROR"  # priority 11 = facility 1, severity 3


def test_text_is_one_event_per_line_and_stack_traces_attach():
    text = (
        "INFO one\n\n"
        "ERROR java.lang.IllegalStateException: boom\n"
        "    at a.b.C.run(C.java:10)\n"
        "Caused by: java.io.IOException\n"
        "\t... 12 more\n"
        "WARN three\n"
    )
    events = parse_text(text)
    assert [e["message"] for e in events] == [
        "INFO one",
        "ERROR java.lang.IllegalStateException: boom",
        "WARN three",
    ]
    assert events[1]["stack"].count("\n") == 2
    assert parse_text("   \n\n") == []


def test_body_events_shapes():
    assert body_events(b'{"a": 1}', "application/json") == (None, {"a": 1})
    assert body_events(b'[{"a": 1}, 2, null]', "application/json") == (
        [{"a": 1}, {"value": 2}, {"value": None}],
        None,
    )
    events, single = body_events(S3.encode(), "text/plain")
    assert single is None and events[0]["latency_ms"] == 1840
    events, _ = body_events(b"plain line", None)  # no content type, not JSON -> text
    assert events == [{"message": "plain line"}]
    events, _ = body_events(b"42", "text/plain; charset=utf-8")
    assert events == [{"message": "42"}]


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not json", "application/json"),
        (b"not json", "application/cloudevents+json"),
        (b"null", "application/json"),
        (b'"text"', None),
        (b"", None),
        (b"  \n ", "text/plain"),
    ],
)
def test_body_events_rejects(body, content_type):
    with pytest.raises(AppError) as err:
        body_events(body, content_type)
    assert err.value.status_code == 422


def test_json_content_types():
    assert is_json_content_type("application/json; charset=utf-8")
    assert is_json_content_type("application/vnd.api+json")
    assert not is_json_content_type("text/plain")
    assert not is_json_content_type(None)


def test_wildcard_lookup():
    assert lookup(LEGS, "legs.*.status") == ["FILLED", "REJECTED", "FILLED"]
    assert isinstance(lookup(LEGS, "legs.*.status"), AnyOf)
    assert lookup(LEGS, "legs.*.qty") == [5000000, 12000000]  # elements without it are skipped
    assert lookup(LEGS, "legs.*.nope") is MISSING
    assert lookup(LEGS, "tags.*") == ["prod", "eu"]
    assert lookup({"a": {"x": {"v": 1}, "y": {"v": 2}}}, "a.*.v") == [1, 2]  # dict values
    assert lookup({"a": [{"b": [{"c": 1}, {"c": 2}]}, {"b": [{"c": 3}]}]}, "a.*.b.*.c") == [1, 2, 3]
    assert lookup({"a": {"*": 5}}, "a.*") == 5  # a literal "*" key wins
    assert lookup({"a": 5}, "a.*") is MISSING
    assert get_path(LEGS, "legs.1.status") == "REJECTED"


def _matches(condition: dict, raw: dict) -> bool:
    rule = parse_rule(
        {"name": "r", "teamId": str(uuid4()), "match": {"op": "and", "conditions": [condition]}}
    )
    return compile_rule(rule).matches(EventView(raw, {}))


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("eq", "REJECTED", True),
        ("eq", "CANCELLED", False),
        ("neq", "REJECTED", False),  # no element may equal it
        ("neq", "CANCELLED", True),
        ("in", ["CANCELLED", "REJECTED"], True),
        ("not_in", ["CANCELLED", "REJECTED"], False),
        ("contains", "JECT", True),
        ("regex", "^REJ", True),
        ("glob", "FILL*", True),
        ("exists", True, True),
    ],
)
def test_wildcard_operators_mean_any_element(operator, value, expected):
    assert _matches({"field": "legs.*.status", "operator": operator, "value": value}, LEGS) is expected


def test_wildcard_numeric_and_missing():
    assert _matches({"field": "legs.*.qty", "operator": "gt", "value": 10000000}, LEGS)
    assert not _matches({"field": "legs.*.qty", "operator": "gt", "value": 20000000}, LEGS)
    assert not _matches({"field": "legs.*.nope", "operator": "exists", "value": True}, LEGS)
    assert _matches({"field": "legs.*.nope", "operator": "exists", "value": False}, LEGS)
    assert _matches({"field": "legs.*.nope", "operator": "neq", "value": "x"}, LEGS)
    assert _matches({"field": "tags.*", "operator": "eq", "value": "prod"}, LEGS)


def test_wildcard_template_joins_values():
    ev = EventView(LEGS, {})
    assert render("Pairs: {{legs.*.pair}}; tags {{tags}}", ev) == 'Pairs: EURUSD, GBPUSD, USDJPY; tags ["prod","eu"]'


def test_alertmanager_body_is_split_without_a_preset():
    body = {
        "receiver": "herald",
        "groupKey": "{}",
        "status": "firing",
        "alerts": [
            {"status": "firing", "labels": {"alertname": "DiskFull"}, "startsAt": "2026-11-20T13:58:00Z"},
            {"status": "resolved", "labels": {"alertname": "DiskFull"}, "startsAt": "2026-11-20T12:10:00Z"},
        ],
    }
    assert detect_preset(body) == "alertmanager"
    parts = split_events(None, body)
    assert [p["status"] for p in parts] == ["firing", "resolved"]
    assert all(detect_preset(p) == "alertmanager" for p in parts)
    assert detect_preset({"alerts": [{"a": 1}]}) is None
    assert split_events(None, {"alerts": [1]}) == [{"alerts": [1]}]
