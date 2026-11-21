"""Herald `RuleDoc` <-> our `RuleDef` JSON (as lossless as the two models allow).

Herald                                   ours
`when` {all|any|not|field/op/value}      `match` ConditionGroup {op: and|or, conditions}
`@type` `@severity` `@service` ...       `_type` `_severity` `_service` ... (normalized `_meta`)
op `ne` / `severity_gte`                 `neq` / `in` [that severity and worse]
`not`                                    pushed down (De Morgan) to negated operators; `contains`, `regex`,
                                         `glob` cannot be negated -> `DslError` (422)
`notify[{to, channels, hours, else}]`    `routing` hard/soft recipients + `channelsBySeverity` + `timeWindow`
`present{title, body, fields, colour}`   `presentation{titleTemplate, bodyTemplate, fields, color}`
`group{by, window}`                      `dedup.fields` (+ `grouping{byField: by[0], windowSeconds}`)
`severity` (null = from the event)       `priority`
`owner` team:<slug> (or person:<id>)      `teamId` (a person -> their first team; read back as team:<slug>)
`runbook`, `name`, `enabled`             `runbookId`, `name`, `enabled`

What our model cannot hold (`present.highlight`, `resolve_when`, the exact Herald notify
list) is kept in `definition.herald` (ignored by our compiler). When the rule is read back, a stored
Herald section is shown only while it still translates to what the rule holds now, so a later `/v1`
edit never shows stale data. Saving over an existing rule keeps our section untouched when the Herald
section did not change (escalation, throttle, dedup window and per-severity channels survive the UI).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from app.core.scope import Scope
from app.herald.views import field_name
from app.pipeline.rules.index import rule_from_row
from app.pipeline.rules.model import RuleDef, RuleValidationError

SEVERITIES = ("critical", "error", "warning", "info")  # worst first
_SEVERITY_WORDS = {
    "debug": "info", "trace": "info", "info": "info", "notice": "info", "low": "info",
    "warn": "warning", "warning": "warning", "minor": "warning", "medium": "warning",
    "err": "error", "error": "error", "major": "error", "high": "error",
    "crit": "critical", "critical": "critical", "fatal": "critical", "emergency": "critical",
}  # fmt: skip
INTERRUPTING = ("sms", "phone")
CHANNELS = ("ui", "email", "sms", "phone", "webhook")
OPS_TO_OURS = {
    "eq": "eq", "ne": "neq", "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", "in": "in",
    "contains": "contains", "regex": "regex", "matches": "regex", "glob": "glob", "exists": "exists",
    "=": "eq", "==": "eq", "!=": "neq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte",
}  # fmt: skip
OPS_TO_HERALD = {"neq": "ne"}
NEGATE = {
    "eq": "neq",
    "neq": "eq",
    "in": "not_in",
    "not_in": "in",
    "gt": "lte",
    "lte": "gt",
    "gte": "lt",
    "lt": "gte",
}
DEFAULT_GROUP_BY = ["@type", "@service"]
_DURATION = re.compile(r"^\s*(\d+)\s*(s|m|h|d)?\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
_PLACEHOLDER_ALIAS = re.compile(r"\{\{(\s*)([@_])")


class DslError(ValueError):
    """The Herald document cannot be expressed as our rule. The message is shown in the UI."""


@dataclass
class Directory:
    """Ids the translation needs: team uuid <-> slug, person id (email local part) <-> email / uuid."""

    team_slugs: dict[UUID, str] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)  # person id -> email
    user_ids: dict[str, str] = field(default_factory=dict)  # user uuid (str) -> person id
    person_team: dict[str, UUID] = field(default_factory=dict)  # person id -> their first team

    def team_id(self, slug: str) -> UUID | None:
        return next((tid for tid, s in self.team_slugs.items() if s == slug or str(tid) == slug), None)

    def person_of(self, ref: str) -> str:
        """Person id from a user recipient id (email or uuid)."""
        if "@" in ref:
            return ref.split("@", 1)[0]
        return self.user_ids.get(ref, ref)


async def load_directory(conn: asyncpg.Connection) -> Directory:
    teams = await conn.fetch("SELECT id, slug FROM teams")
    users = await conn.fetch(
        "SELECT u.id, u.email, (SELECT tm.team_id FROM team_members tm JOIN teams t ON t.id = tm.team_id "
        " WHERE tm.user_id = u.id ORDER BY t.slug LIMIT 1) AS team_id FROM users u"
    )
    directory = Directory(team_slugs={r["id"]: r["slug"] or str(r["id"]) for r in teams})
    for row in users:
        pid = row["email"].split("@", 1)[0]
        directory.emails[pid] = row["email"]
        directory.user_ids[str(row["id"])] = pid
        if row["team_id"] is not None:
            directory.person_team[pid] = row["team_id"]
    return directory


# ---------- small helpers ----------


def field_to_ours(path: str) -> str:
    return "_" + path[1:] if path.startswith("@") else path


field_to_herald = field_name  # `_severity` -> `@severity` (HB-CORE's views)


def template_to_ours(text: str | None) -> str | None:
    return _PLACEHOLDER_ALIAS.sub(lambda m: "{{" + m.group(1) + "_", text) if text else text


def template_to_herald(text: str | None) -> str | None:
    return _PLACEHOLDER_ALIAS.sub(lambda m: "{{" + m.group(1) + "@", text) if text else text


def normalize_severity(value: Any) -> str | None:
    if value is None:
        return None
    word = _SEVERITY_WORDS.get(str(value).strip().lower())
    if word is None:
        raise DslError(f"'{value}' is not a known severity")
    return word


def severities_from(threshold: Any) -> list[str]:
    """`severity_gte error` -> [critical, error]."""
    name = normalize_severity(threshold)
    return list(SEVERITIES[: SEVERITIES.index(name) + 1])


def seconds(text: Any) -> int | None:
    if text in (None, ""):
        return None
    match = _DURATION.match(str(text).lower())
    if not match:
        raise DslError(f"'{text}' is not a duration like 30s, 5m, 2h or 1d")
    return int(match.group(1)) * _UNIT[match.group(2)]


def duration(value: int | None) -> str | None:
    if not value:
        return None
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if value % size == 0:
            return f"{value // size}{unit}"
    return f"{value}s"


# ---------- conditions ----------


def expand(node: Any) -> dict[str, Any]:
    """Herald condition in full form (`{"type": "X"}` shorthand = all-equal), validated."""
    if not isinstance(node, dict) or not node:
        raise DslError("a condition must be a non-empty object")
    groups = [k for k in ("all", "any", "not") if k in node]
    if groups:
        if len(node) != 1:
            raise DslError(f"a group has exactly one of all / any / not, found: {sorted(node)}")
        key = groups[0]
        if key == "not":
            return {"not": expand(node["not"])}
        children = node[key]
        if not isinstance(children, list) or not children:
            raise DslError(f"'{key}' needs a non-empty list of conditions")
        return {key: [expand(c) for c in children]}
    if "field" in node:
        if not isinstance(node["field"], str) or not node["field"].strip():
            raise DslError("choose a field for every condition")
        op = node.get("op", "eq")
        if op not in OPS_TO_OURS and op != "severity_gte":
            raise DslError(f"unknown operator '{op}'")
        if "value" not in node and op != "exists":
            raise DslError(f"the condition on '{node['field']}' needs a value")
        return {"field": node["field"], "op": op, "value": node.get("value", True)}
    return {"all": [{"field": str(k), "op": "eq", "value": v} for k, v in node.items()]}


def leaf_to_condition(node: dict[str, Any], negated: bool = False) -> dict[str, Any]:
    name, op, value = field_to_ours(node["field"]), node["op"], node["value"]
    if op == "severity_gte":
        if name != "_severity":
            raise DslError("'severity_gte' works on @severity only")
        op, value = "in", severities_from(value)
    else:
        op = OPS_TO_OURS[op]
    if op in ("in", "not_in") and not isinstance(value, list):
        value = [value]
    if op == "exists":
        value = value if isinstance(value, bool) else str(value).lower() != "false"
    if negated:
        if op == "exists":
            value = not value
        elif op in NEGATE:
            op = NEGATE[op]
        else:
            raise DslError(f"an excluded '{node['op']}' condition on '{node['field']}' is not supported")
    return {"field": name, "operator": op, "value": value}


def _node(node: dict[str, Any], negated: bool = False) -> dict[str, Any]:
    if "not" in node:
        return _node(node["not"], not negated)
    for key, op, flipped in (("all", "and", "or"), ("any", "or", "and")):
        if key in node:
            children = [_node(c, negated) for c in node[key]]
            if len(children) == 1:
                return children[0]
            return {"op": flipped if negated else op, "conditions": children}
    return leaf_to_condition(node, negated)


def when_to_match(when: Any) -> dict[str, Any]:
    """Herald `when` -> our ConditionGroup JSON (root is always a group)."""
    node = _node(expand(when))
    return node if "conditions" in node else {"op": "and", "conditions": [node]}


def _condition_to_herald(cond: dict[str, Any]) -> dict[str, Any]:
    name, op, value = field_to_herald(cond["field"]), cond["operator"], cond.get("value")
    if name == "@severity" and op == "in" and isinstance(value, list) and value:
        names = [str(v).lower() for v in value]
        for i in range(len(SEVERITIES)):
            if sorted(names) == sorted(SEVERITIES[: i + 1]):
                return {"field": name, "op": "severity_gte", "value": SEVERITIES[i]}
    if op == "not_in":
        return {"not": {"field": name, "op": "in", "value": value}}
    if op == "exists":
        value = True if value is None else value
    return {"field": name, "op": OPS_TO_HERALD.get(op, op), "value": value}


def match_to_when(group: dict[str, Any]) -> dict[str, Any]:
    if "conditions" not in group:
        return _condition_to_herald(group)
    key = "all" if group.get("op", "and") == "and" else "any"
    return {key: [match_to_when(c) for c in group["conditions"]]}


# ---------- notify <-> routing ----------


def _recipient_to(rec: dict[str, Any], directory: Directory) -> str:
    kind, ref = rec["kind"], rec["id"]
    if kind == "user":
        return f"person:{directory.person_of(ref)}"
    slug = directory.team_slugs.get(_uuid(ref), ref) if _uuid(ref) else ref
    if kind == "team":
        return f"team:{slug}"
    if kind == "oncall":
        return f"team:{slug}/on-call"
    return f"role:{ref}"


def _to_recipient(to: str, directory: Directory) -> dict[str, str]:
    kind, _, ref = str(to).partition(":")
    if kind == "person" and ref:
        if ref not in directory.emails:
            raise DslError(f"unknown person '{ref}'")
        return {"kind": "user", "id": directory.emails[ref]}
    if kind == "role" and ref:
        return {"kind": "role", "id": ref}
    if kind in ("team", "oncall") and ref:
        slug, _, suffix = ref.partition("/")
        if directory.team_id(slug) is None:
            raise DslError(f"unknown team '{slug}'")
        oncall = kind == "oncall" or suffix == "on-call"
        return {"kind": "oncall" if oncall else "team", "id": slug}
    raise DslError(f"'{to}': use team:<id>, team:<id>/on-call, person:<id> or role:<team>:<role>")


def _channels(items: Any) -> list[str]:
    if items in (None, "preferences"):
        return []
    if not isinstance(items, list):
        raise DslError("channels must be a list or 'preferences'")
    unknown = [c for c in items if c not in CHANNELS]
    if unknown:
        raise DslError(f"unknown channel(s): {', '.join(map(str, unknown))}")
    return list(dict.fromkeys(items))


def notify_to_routing(notify: Any, severity: str | None, directory: Directory) -> dict[str, Any]:
    """Targets on call, people, or with SMS/phone are hard (acked, escalated); the rest are soft."""
    hard: list[dict[str, str]] = []
    soft: list[dict[str, str]] = []
    hard_channels: list[str] = []
    soft_channels: list[str] = []
    window: dict[str, Any] | None = None
    for target in notify or []:
        if not isinstance(target, dict) or "to" not in target:
            raise DslError("every notify entry needs 'to'")
        rec = _to_recipient(target["to"], directory)
        channels = _channels(target.get("channels"))
        is_hard = rec["kind"] in ("user", "oncall") or any(c in INTERRUPTING for c in channels)
        bucket, chans = (hard, hard_channels) if is_hard else (soft, soft_channels)
        if rec not in bucket:
            bucket.append(rec)
        chans += [c for c in channels if c not in chans]
        hours = target.get("hours")
        if hours and window is None:
            window = {
                "from": hours.get("from") or hours.get("start"),
                "to": hours.get("to") or hours.get("end"),
                "timezone": hours.get("tz") or "UTC",
                "outsideChannels": _channels(target.get("else")),
            }
    channels = hard_channels or soft_channels
    out: dict[str, Any] = {"hard": hard, "soft": soft, "channelsBySeverity": {}}
    if channels:
        out["channelsBySeverity"] = {s: list(channels) for s in ([severity] if severity else SEVERITIES)}
    if window:
        out["timeWindow"] = window
    return out


def _rule_channels(routing: dict[str, Any], severity: str | None) -> list[str]:
    by = routing.get("channelsBySeverity") or {}
    if severity and by.get(severity):
        return list(by[severity])
    seen: list[str] = []
    for sev in SEVERITIES:
        seen += [c for c in by.get(sev) or [] if c not in seen]
    return seen


def routing_to_notify(
    routing: dict[str, Any], severity: str | None, directory: Directory
) -> list[dict[str, Any]]:
    channels = _rule_channels(routing, severity)
    window = routing.get("timeWindow")
    out: list[dict[str, Any]] = []
    for kind in ("hard", "soft"):
        for rec in routing.get(kind) or []:
            chans = channels
            if kind == "soft" and channels:
                chans = [c for c in channels if c not in INTERRUPTING] or ["ui"]
            target: dict[str, Any] = {
                "to": _recipient_to(rec, directory),
                "channels": list(chans) or "preferences",
            }
            if window and window.get("from") and window.get("to"):
                target["hours"] = {
                    "from": window["from"],
                    "to": window["to"],
                    "tz": window.get("timezone") or "UTC",
                }
                target["else"] = list(window.get("outsideChannels") or [])
            out.append(target)
    return out


# ---------- present / group ----------


def present_to_presentation(present: Any) -> dict[str, Any]:
    present = present or {}
    out: dict[str, Any] = {
        "titleTemplate": template_to_ours(present.get("title")) or "{{_type}}",
        "fields": [field_to_ours(f) for f in present.get("fields") or []],
    }
    if present.get("body"):
        out["bodyTemplate"] = template_to_ours(present["body"])
    if present.get("colour"):
        out["color"] = present["colour"]
    return out


def presentation_to_present(pres: dict[str, Any], highlight: list[str] | None = None) -> dict[str, Any]:
    return {
        "title": template_to_herald(pres.get("titleTemplate")) or "{{@type}}",
        "body": template_to_herald(pres.get("bodyTemplate")) or "",
        "fields": [field_to_herald(f) for f in pres.get("fields") or []],
        "highlight": list(highlight or []),
        "colour": pres.get("color"),
    }


def group_to_ours(group: Any) -> dict[str, Any]:
    group = group if isinstance(group, dict) else {}
    by = [field_to_ours(f) for f in (group.get("by") if "by" in group else DEFAULT_GROUP_BY) or []]
    out: dict[str, Any] = {"dedup": {"fields": by, "windowSeconds": 300}}
    window = seconds(group.get("window"))
    if window and by:
        out["grouping"] = {"byField": by[0], "windowSeconds": window}
    elif window:
        out["dedup"]["windowSeconds"] = window
    return out


def ours_to_group(dedup: dict[str, Any], grouping: dict[str, Any] | None) -> dict[str, Any]:
    by = list(dedup.get("fields") or [])
    if grouping and grouping.get("byField") and grouping["byField"] not in by:
        by.insert(0, grouping["byField"])
    window = (grouping or {}).get("windowSeconds") if grouping and grouping.get("byField") else None
    return {"by": [field_to_herald(f) for f in by], "window": duration(window)}


# ---------- whole documents ----------


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _owner_team(owner: Any, directory: Directory) -> UUID:
    kind, _, ref = str(owner or "").partition(":")
    if kind == "team" and directory.team_id(ref) is not None:
        return directory.team_id(ref)  # type: ignore[return-value]
    if kind == "person" and ref in directory.person_team:
        return directory.person_team[ref]
    raise DslError(f"owner '{owner}': use team:<id> of a known team, or person:<id> of a team member")


def _sections(doc: dict[str, Any], directory: Directory) -> dict[str, dict[str, Any]]:
    """Our fields per Herald section, keyed by section name."""
    severity = normalize_severity(doc.get("severity"))
    return {
        "when": {"match": when_to_match(doc.get("when"))},
        "severity": {"priority": severity},
        "notify": {"routing": notify_to_routing(doc.get("notify"), severity, directory)},
        "present": {"presentation": present_to_presentation(doc.get("present"))},
        "group": group_to_ours(doc.get("group")),
        "runbook": {"runbookId": doc.get("runbook") or None},
    }


def _comparable(section: str, doc: dict[str, Any]) -> Any:
    if section == "present":
        present = dict(doc.get("present") or {})
        present.pop("highlight", None)
        return present_to_presentation(present)
    return doc.get(section)


def herald_to_rule(
    doc: dict[str, Any], directory: Directory, base: RuleDef | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(our rule JSON for `parse_rule`, the `definition.herald` extras). Raises `DslError`."""
    if not isinstance(doc, dict):
        raise DslError("the rule must be an object")
    name = str(doc.get("name") or "").strip()
    if not name:
        raise DslError("give the rule a name")
    data: dict[str, Any] = base.definition() if base is not None else {}
    data.update(
        name=name[:200],
        teamId=str(_owner_team(doc.get("owner"), directory)),
        enabled=doc.get("enabled", True) is not False,
    )
    previous = rule_to_herald(base, directory) if base is not None else None
    for section, values in _sections(doc, directory).items():
        if previous is not None and _comparable(section, previous) == _comparable(section, doc):
            continue  # unchanged in the UI: keep our richer section as it is
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        if section == "group" and "grouping" not in values:
            data.pop("grouping", None)
    extras = {
        "highlight": list((doc.get("present") or {}).get("highlight") or []),
        "resolve_when": expand(doc["resolve_when"]) if doc.get("resolve_when") else None,
        "doc": {k: doc.get(k) for k in ("when", "notify", "group", "present", "severity")},
    }
    return data, extras


def rule_to_herald(
    rule: RuleDef, directory: Directory, extras: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Our rule -> Herald `RuleDoc`. Stored Herald sections win while they still mean the same rule."""
    data = rule.definition()
    extras = extras or {}
    slug = directory.team_slugs.get(rule.team_id, str(rule.team_id))
    owner = f"team:{slug}"  # always a team (both UIs filter on `team:<slug>`; we have no personal owners)
    severity = data.get("priority")
    doc: dict[str, Any] = {
        "id": str(rule.id) if rule.id else None,
        "name": rule.name,
        "owner": owner,
        "enabled": rule.enabled,
        "version": rule.version or 1,
        "severity": severity,
        "when": match_to_when(data["match"]),
        "present": presentation_to_present(data.get("presentation") or {}, extras.get("highlight")),
        "group": ours_to_group(data.get("dedup") or {}, data.get("grouping")),
        "notify": routing_to_notify(data.get("routing") or {}, severity, directory),
        "runbook": data.get("runbookId"),
        "resolve_when": extras.get("resolve_when"),
    }
    stored = extras.get("doc") or {}
    for section, ours in (
        ("when", "match"),
        ("notify", "routing"),
        ("group", None),
        ("present", "presentation"),
    ):
        if section not in stored or stored[section] is None:
            continue
        try:
            values = _sections({**doc, section: stored[section]}, directory)[section]
        except DslError:
            continue
        current = {k: data.get(k) for k in values} if ours is None else {ours: data.get(ours)}
        if section == "group":
            current = {"dedup": data.get("dedup"), "grouping": data.get("grouping")}
            values = {"dedup": values["dedup"], "grouping": values.get("grouping")}
        if _same(values, current):
            doc[section] = (
                _with_highlight(stored[section], extras) if section == "present" else stored[section]
            )
    return doc


def _with_highlight(present: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
    return {**present, "highlight": list(extras.get("highlight") or present.get("highlight") or [])}


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare after a round through our model (defaults filled, aliases normalized)."""
    return _canon(left) == _canon(right)


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items() if v not in (None, [], {})}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


# ---------- stored rules ----------

RULE_SELECT = (
    "SELECT r.id, r.team_id, r.name, r.enabled, r.version, r.definition, r.created_by, r.updated_at "
    "FROM rules r"
)


@dataclass
class StoredRule:
    row: asyncpg.Record
    rule: RuleDef
    extras: dict[str, Any]


def stored_rule(row: asyncpg.Record) -> StoredRule | None:
    try:
        rule = rule_from_row(row)
    except RuleValidationError:
        return None
    extras = (row["definition"] or {}).get("herald")
    return StoredRule(row, rule, extras if isinstance(extras, dict) else {})


async def scoped_rules(conn: asyncpg.Connection, scope: Scope) -> list[StoredRule]:
    """Every valid rule of the caller's teams (scope-filtered by `team_id`)."""
    rows = await conn.fetch(
        f"{RULE_SELECT} WHERE r.team_id = ANY($1) ORDER BY r.name, r.id",
        [t for t in scope.team_ids if scope.can_see_team(t)],
    )
    return [s for row in rows if (s := stored_rule(row)) is not None]
