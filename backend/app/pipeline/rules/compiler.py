"""Compile a `RuleDef` into a closure tree. No `eval`, no dynamic code: every node is a small function.

Operators (spec 7.2): eq neq gt gte lt lte (numeric when both sides are numeric, numeric strings
included, else string compare), in/not_in, contains (substring or list membership), regex, glob, exists.

Missing paths: `eq/gt/gte/lt/lte/in/contains/regex/glob/exists(true)` are False, `neq/not_in` are True,
`exists(false)` is True. A present `null` is not "missing".

Wildcard paths (`legs.*.status`, see `fields.lookup`) mean "any element": a positive operator holds when it
holds for one element; `neq` / `not_in` are their negation, i.e. "no element equals". Two wildcard
conditions in one rule may be satisfied by two different elements.

Regex "50 ms guard": Python's `re` cannot be interrupted, so the guard is preventive: patterns are capped
at 500 chars, patterns with nested quantifiers (`(a+)+`, the classic catastrophic shape) are rejected at
save time, and the input is truncated to 10 000 chars so even a quadratic pattern stays far below 50 ms.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Callable
from typing import Any

from app.pipeline.fields import MISSING, AnyOf, EventView
from app.pipeline.rules.model import (
    MAX_CONDITIONS,
    MAX_DEPTH,
    Condition,
    ConditionGroup,
    RuleDef,
    RuleValidationError,
)

__all__ = ["CompiledRule", "RuleValidationError", "compile_rule"]

MAX_REGEX_LEN = 500
MAX_REGEX_INPUT = 10_000
MAX_LIST_VALUES = 1000
INDEXED_FIELDS = ("_source", "_schema", "_type")

_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d)")

Predicate = Callable[[EventView], bool]


class CompiledRule:
    """A rule plus its compiled predicate; `index_key` is `(field, value)` when indexable."""

    __slots__ = ("_predicate", "index_key", "rule")

    def __init__(self, rule: RuleDef, predicate: Predicate, index_key: tuple[str, str] | None) -> None:
        self.rule = rule
        self._predicate = predicate
        self.index_key = index_key

    def matches(self, ev: EventView) -> bool:
        return self._predicate(ev)


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int | float):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, str):
        s = x.strip()
        if not s or "_" in s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _text(x: Any) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def _equals(actual: Any, expected: Any) -> bool:
    na, ne = _num(actual), _num(expected)
    if na is not None and ne is not None:
        return na == ne
    if isinstance(actual, dict | list) or isinstance(expected, dict | list):
        return actual == expected
    if actual is None or expected is None:
        return actual is None and expected is None
    return _text(actual) == _text(expected)


def _ordered(actual: Any, expected: Any) -> int | None:
    """-1/0/1 for numeric-or-string ordering; None when not comparable."""
    na, ne = _num(actual), _num(expected)
    if na is not None and ne is not None:
        return (na > ne) - (na < ne)
    if actual is None or expected is None or isinstance(actual, dict | list):
        return None
    a, e = _text(actual), _text(expected)
    return (a > e) - (a < e)


def _in(actual: Any, values: list[Any]) -> bool:
    items = actual if isinstance(actual, list) else [actual]
    return any(_equals(item, v) for item in items for v in values)


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return not isinstance(expected, dict | list) and expected is not None and _text(expected) in actual
    if isinstance(actual, list):
        return any(_equals(item, expected) for item in actual)
    return False


def _scalar_text(actual: Any) -> str | None:
    if actual is MISSING or actual is None or isinstance(actual, dict | list):
        return None
    return _text(actual)[:MAX_REGEX_INPUT]


def _compile_regex(pattern: str, path: str) -> re.Pattern[str]:
    if len(pattern) > MAX_REGEX_LEN:
        raise RuleValidationError(path, f"regex longer than {MAX_REGEX_LEN} characters")
    if _NESTED_QUANTIFIER.search(pattern):
        raise RuleValidationError(
            path, "regex with nested quantifiers is rejected (catastrophic backtracking)"
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleValidationError(path, f"invalid regex: {exc}") from exc


def _exists_flag(value: Any, path: str) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise RuleValidationError(path, "exists expects true or false")


def _present(name: str, test: Callable[[Any], bool]) -> Predicate:
    """True when the field is present and `test` holds; on a wildcard path, when it holds for any element."""

    def _check(ev: EventView) -> bool:
        value = ev.lookup(name)
        if value is MISSING:
            return False
        if isinstance(value, AnyOf):
            return any(test(item) for item in value)
        return test(value)

    return _check


def _negated(positive: Predicate) -> Predicate:
    """`neq` / `not_in`: missing is True; on a wildcard path, no element may match (De Morgan of eq / in)."""
    return lambda ev: not positive(ev)


def _compile_condition(cond: Condition, path: str) -> Predicate:
    op, name, expected = cond.operator, cond.field, cond.value
    vpath = f"{path}.value"

    if op == "exists":
        flag = _exists_flag(expected, vpath)
        return lambda ev: (ev.lookup(name) is not MISSING) == flag

    if op in ("in", "not_in"):
        if not isinstance(expected, list) or not expected:
            raise RuleValidationError(vpath, f"{op} expects a non-empty list")
        if len(expected) > MAX_LIST_VALUES:
            raise RuleValidationError(vpath, f"{op} list longer than {MAX_LIST_VALUES}")
        values = list(expected)
        member = _present(name, lambda v: _in(v, values))
        return member if op == "in" else _negated(member)

    if op in ("regex", "glob"):
        if not isinstance(expected, str) or not expected:
            raise RuleValidationError(vpath, f"{op} expects a non-empty string")
        if op == "regex":
            pattern = _compile_regex(expected, vpath)
        else:
            if len(expected) > MAX_REGEX_LEN:
                raise RuleValidationError(vpath, f"glob longer than {MAX_REGEX_LEN} characters")
            pattern = re.compile(fnmatch.translate(expected))

        def _match(value: Any) -> bool:
            text = _scalar_text(value)
            if text is None:
                return False
            return (pattern.search(text) if op == "regex" else pattern.match(text)) is not None

        return _present(name, _match)

    if op in ("gt", "gte", "lt", "lte"):
        if expected is None or isinstance(expected, dict | list):
            raise RuleValidationError(vpath, f"{op} expects a number or a string")
        accept = {"gt": (1,), "gte": (0, 1), "lt": (-1,), "lte": (-1, 0)}[op]
        return _present(name, lambda v: _ordered(v, expected) in accept)

    if op == "contains":
        if expected is None or isinstance(expected, dict | list):
            raise RuleValidationError(vpath, "contains expects a scalar value")
        return _present(name, lambda v: _contains(v, expected))
    equal = _present(name, lambda v: _equals(v, expected))
    return equal if op == "eq" else _negated(equal)


def _compile_group(group: ConditionGroup, path: str, depth: int, budget: list[int]) -> Predicate:
    if depth > MAX_DEPTH:
        raise RuleValidationError(path, f"conditions nested deeper than {MAX_DEPTH} levels")
    if not group.conditions:
        raise RuleValidationError(f"{path}.conditions", "a group needs at least one condition")
    children: list[Predicate] = []
    for i, node in enumerate(group.conditions):
        child_path = f"{path}.conditions[{i}]"
        budget[0] -= 1
        if budget[0] < 0:
            raise RuleValidationError(child_path, f"more than {MAX_CONDITIONS} conditions")
        if isinstance(node, ConditionGroup):
            children.append(_compile_group(node, child_path, depth + 1, budget))
        else:
            children.append(_compile_condition(node, child_path))
    if len(children) == 1:
        return children[0]
    if group.op == "and":
        return lambda ev: all(child(ev) for child in children)
    return lambda ev: any(child(ev) for child in children)


def _index_key(group: ConditionGroup) -> tuple[str, str] | None:
    """`eq` on _source/_schema/_type in the top-level AND (a one-condition OR group is also an AND)."""
    if group.op != "and" and len(group.conditions) != 1:
        return None
    for node in group.conditions:
        if (
            isinstance(node, Condition)
            and node.operator == "eq"
            and node.field in INDEXED_FIELDS
            and isinstance(node.value, str)
            and node.value
        ):
            return node.field, node.value
    return None


def compile_rule(rule: RuleDef) -> CompiledRule:
    """Compile; raises `RuleValidationError(path, message)` for operators/values that cannot work."""
    predicate = _compile_group(rule.match, "match", 1, [MAX_CONDITIONS])
    return CompiledRule(rule, predicate, _index_key(rule.match))
