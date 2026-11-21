"""Operators, nesting, missing paths, validation paths."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app.pipeline.fields import EventView
from app.pipeline.rules.compiler import compile_rule
from app.pipeline.rules.model import RuleValidationError, parse_rule


def make_rule(match: dict, **extra) -> dict:
    return {"name": "r", "teamId": str(uuid4()), "match": match, **extra}


def cond(field: str, operator: str, value=None) -> dict:
    out = {"field": field, "operator": operator}
    if value is not None:
        out["value"] = value
    return out


def check(condition: dict, raw: dict, meta: dict | None = None) -> bool:
    rule = parse_rule(make_rule({"op": "and", "conditions": [condition]}))
    return compile_rule(rule).matches(EventView(raw, meta or {}))


@pytest.mark.parametrize(
    ("operator", "value", "raw", "expected"),
    [
        ("eq", "a", {"x": "a"}, True),
        ("eq", "a", {"x": "b"}, False),
        ("eq", 5, {"x": 5}, True),
        ("eq", 5, {"x": "5"}, True),  # numeric string vs number
        ("eq", "5", {"x": 5.0}, True),
        ("eq", "05", {"x": "5"}, True),  # both numeric -> numeric compare
        ("eq", "abc", {"x": 5}, False),
        ("eq", True, {"x": True}, True),
        ("eq", "true", {"x": True}, True),
        ("eq", True, {"x": 1}, False),  # bool is not numeric
        ("eq", "a", {"x": None}, False),
        ("eq", ["a"], {"x": ["a"]}, True),
        ("neq", "a", {"x": "b"}, True),
        ("neq", "a", {"x": "a"}, False),
        ("neq", "a", {}, True),  # missing path
        ("eq", "a", {}, False),
        ("gt", 10, {"x": 11}, True),
        ("gt", 10, {"x": 10}, False),
        ("gt", 10, {"x": "11"}, True),  # numeric string
        ("gt", 10, {"x": "9"}, False),  # numeric, not lexicographic
        ("gt", "10", {"x": 9}, False),
        ("gte", 10, {"x": 10}, True),
        ("lt", 10, {"x": 9.5}, True),
        ("lt", 10, {"x": 10}, False),
        ("lte", 10, {"x": 10}, True),
        ("lte", 10, {"x": 11}, False),
        ("gt", "b", {"x": "c"}, True),  # string compare
        ("lt", "b", {"x": "a"}, True),
        ("gt", 10, {}, False),
        ("gt", 10, {"x": None}, False),
        ("gt", 10, {"x": [1]}, False),
        ("gt", 10, {"x": "nan"}, True),  # nan is not a number -> string compare "nan" > "10"
        ("in", ["a", "b"], {"x": "b"}, True),
        ("in", ["a", "b"], {"x": "c"}, False),
        ("in", [1, 2], {"x": "2"}, True),
        ("in", ["a"], {"x": ["z", "a"]}, True),  # list actual: any element
        ("in", ["a"], {}, False),
        ("not_in", ["a", "b"], {"x": "c"}, True),
        ("not_in", ["a", "b"], {"x": "a"}, False),
        ("not_in", ["a"], {}, True),
        ("contains", "err", {"x": "an error"}, True),
        ("contains", "ERR", {"x": "an error"}, False),  # case-sensitive
        ("contains", "b", {"x": ["a", "b"]}, True),
        ("contains", 2, {"x": [1, 2]}, True),
        ("contains", "z", {"x": ["a"]}, False),
        ("contains", "a", {"x": 5}, False),
        ("contains", "a", {}, False),
        ("regex", r"^T-\d+$", {"x": "T-998877"}, True),
        ("regex", r"^T-\d+$", {"x": "X-1"}, False),
        ("regex", r"\d+", {"x": 42}, True),  # numbers are matched as text
        ("regex", "a", {"x": {"a": 1}}, False),
        ("regex", "a", {}, False),
        ("glob", "pricing-engine-*", {"x": "pricing-engine-3"}, True),
        ("glob", "pricing-engine-*", {"x": "other"}, False),
        ("glob", "trade.rejected.?2", {"x": "trade.rejected.v2"}, True),
        ("glob", "ABC*", {"x": "abcd"}, False),  # case-sensitive
        ("glob", "*", {}, False),
        ("exists", True, {"x": 1}, True),
        ("exists", True, {"x": None}, True),  # null is present
        ("exists", True, {}, False),
        ("exists", False, {}, True),
        ("exists", False, {"x": 1}, False),
        ("exists", "false", {}, True),
    ],
)
def test_operator(operator, value, raw, expected):
    assert check(cond("x", operator, value), raw) is expected


def test_exists_defaults_to_true():
    assert check({"field": "x", "operator": "exists"}, {"x": 0}) is True
    assert check({"field": "x", "operator": "exists"}, {}) is False


def test_nested_paths_and_array_index():
    raw = {"alerts": [{"labels": {"severity": "critical"}}]}
    assert check(cond("alerts.0.labels.severity", "eq", "critical"), raw)
    assert not check(cond("alerts.1.labels.severity", "eq", "critical"), raw)


def test_meta_fields_resolve_via_underscore():
    assert check(cond("_severity", "eq", "error"), {}, {"severity": "error"})
    assert not check(cond("_severity", "eq", "error"), {"_severity": "error"}, {})
    assert check(cond("_error_code", "exists", False), {}, {})


def test_nested_and_or_groups():
    match = {
        "op": "and",
        "conditions": [
            cond("a", "eq", 1),
            {
                "op": "or",
                "conditions": [
                    cond("b", "eq", 2),
                    {"op": "and", "conditions": [cond("c", "gt", 3), cond("d", "eq", "x")]},
                ],
            },
        ],
    }
    compiled = compile_rule(parse_rule(make_rule(match)))
    assert compiled.matches(EventView({"a": 1, "b": 2}))
    assert compiled.matches(EventView({"a": 1, "c": 4, "d": "x"}))
    assert not compiled.matches(EventView({"a": 1, "c": 4, "d": "y"}))
    assert not compiled.matches(EventView({"a": 2, "b": 2}))
    assert not compiled.matches(EventView({}))


def test_or_group_and_single_condition_group():
    any_of = compile_rule(
        parse_rule(make_rule({"op": "or", "conditions": [cond("a", "eq", 1), cond("b", "eq", 1)]}))
    )
    assert any_of.matches(EventView({"b": 1}))
    assert not any_of.matches(EventView({"c": 1}))


@pytest.mark.parametrize(
    ("condition", "path"),
    [
        (cond("x", "in", "not-a-list"), "match.conditions[1].value"),
        (cond("x", "in", []), "match.conditions[1].value"),
        (cond("x", "regex", "("), "match.conditions[1].value"),
        (cond("x", "regex", "a" * 501), "match.conditions[1].value"),
        (cond("x", "regex", "(a+)+$"), "match.conditions[1].value"),
        (cond("x", "regex", "(.*)*"), "match.conditions[1].value"),
        (cond("x", "glob", 5), "match.conditions[1].value"),
        (cond("x", "gt", [1]), "match.conditions[1].value"),
        (cond("x", "contains", [1]), "match.conditions[1].value"),
        (cond("x", "exists", "maybe"), "match.conditions[1].value"),
    ],
)
def test_semantic_validation_errors_carry_the_path(condition, path):
    rule = parse_rule(make_rule({"op": "and", "conditions": [cond("ok", "eq", 1), condition]}))
    with pytest.raises(RuleValidationError) as err:
        compile_rule(rule)
    assert err.value.path == path


def test_nested_error_path_and_empty_group():
    nested = {
        "op": "and",
        "conditions": [cond("a", "eq", 1), {"op": "or", "conditions": [cond("b", "in", "x")]}],
    }
    with pytest.raises(RuleValidationError) as err:
        compile_rule(parse_rule(make_rule(nested)))
    assert err.value.path == "match.conditions[1].conditions[0].value"
    with pytest.raises(RuleValidationError) as err:
        compile_rule(parse_rule(make_rule({"op": "and", "conditions": []})))
    assert err.value.path == "match.conditions"


def test_too_deep_nesting_rejected():
    group: dict = {"op": "and", "conditions": [cond("a", "eq", 1)]}
    for _ in range(8):
        group = {"op": "and", "conditions": [group]}
    with pytest.raises(RuleValidationError, match="nested deeper"):
        compile_rule(parse_rule(make_rule(group)))


def test_schema_errors_point_at_the_invalid_node():
    bad_operator = {
        "op": "and",
        "conditions": [cond("a", "eq", 1), {"field": "b", "operator": "like", "value": "x"}],
    }
    with pytest.raises(RuleValidationError) as err:
        parse_rule(make_rule(bad_operator))
    assert err.value.path == "match.conditions[1].operator"
    with pytest.raises(RuleValidationError) as err:
        parse_rule(make_rule({"op": "xor", "conditions": [cond("a", "eq", 1)]}))
    assert err.value.path == "match.op"
    with pytest.raises(RuleValidationError) as err:
        parse_rule({"name": "r", "teamId": str(uuid4())})
    assert err.value.path == "match"
    with pytest.raises(RuleValidationError) as err:
        parse_rule(
            make_rule(
                {"op": "and", "conditions": [cond("a", "eq", 1)]}, routing={"timeWindow": {"from": "8am"}}
            )
        )
    assert err.value.path == "routing.timeWindow.from"
    with pytest.raises(RuleValidationError) as err:
        parse_rule(
            make_rule(
                {"op": "and", "conditions": [cond("a", "eq", 1)]},
                routing={"timeWindow": {"timezone": "Mars/Base"}},
            )
        )
    assert err.value.path == "routing.timeWindow.timezone"
    with pytest.raises(RuleValidationError):
        parse_rule("not an object")


def test_regex_input_is_truncated_and_bounded_in_time():
    compiled = compile_rule(parse_rule(make_rule({"op": "and", "conditions": [cond("x", "regex", "z$")]})))
    hostile = "a" * 5_000_000 + "z"
    start = time.perf_counter()
    assert compiled.matches(EventView({"x": hostile})) is False  # the trailing z is beyond the 10k cut
    assert time.perf_counter() - start < 0.05


def test_rule_roundtrips_through_wire_json():
    data = make_rule(
        {"op": "and", "conditions": [cond("a", "eq", 1)]},
        priority="error",
        routing={
            "hard": [{"kind": "oncall", "id": "team-x"}],
            "channelsBySeverity": {"critical": ["phone", "sms"]},
            "timeWindow": {
                "from": "08:00",
                "to": "20:00",
                "timezone": "Europe/Warsaw",
                "outsideChannels": ["email"],
            },
        },
        presentation={"titleTemplate": "t {{a}}", "fields": ["a"], "color": "red"},
        grouping={"byField": "a", "windowSeconds": 300},
        runbookId=None,
    )
    rule = parse_rule(data)
    assert rule.routing.time_window.from_ == "08:00"
    assert rule.presentation.title_template == "t {{a}}"
    assert rule.dedup.window_seconds == 300
    again = parse_rule(rule.definition())
    assert again == rule
    assert rule.definition()["routing"]["timeWindow"]["from"] == "08:00"
