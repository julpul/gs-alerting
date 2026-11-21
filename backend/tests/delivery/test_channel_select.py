"""Channel selection matrix (spec 9.2) and rule view parsing. Pure logic, no infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.delivery.channel_select import in_window, select_channels
from app.delivery.ruleview import rule_view
from app.escalation.policies import EffectivePolicy
from app.schemas.api import EscalationLevel

WARSAW_NOON = datetime(2026, 11, 20, 11, 0, tzinfo=UTC)  # 12:00 in Warsaw (CET)
WARSAW_LATE = datetime(2026, 11, 20, 21, 30, tzinfo=UTC)  # 22:30 in Warsaw
ANNA_WINDOW = {"start": "08:00", "end": "20:00", "timezone": "Europe/Warsaw", "outsideChannels": ["email"]}


def policy(level_channels=("phone", "sms"), soft=("email", "ui")) -> EffectivePolicy:
    return EffectivePolicy(
        severity="critical",
        levels=[
            EscalationLevel(level=1, target="oncall", channels=list(level_channels), ackTimeoutSeconds=300)
        ],
        soft_channels=list(soft),
    )


def pick(rule: dict, *, kind="hard", severity="critical", prefs=None, now=WARSAW_NOON, pol=None, level=1):
    return select_channels(
        kind=kind,
        severity=severity,
        rule=rule_view({"routing": rule}),
        prefs=prefs,
        policy=pol or policy(),
        level=level,
        now=now,
    )


def test_anna_sms_inside_window_email_outside():
    routing = {"channelsBySeverity": {"critical": ["sms"]}, "timeWindow": ANNA_WINDOW}
    assert pick(routing, now=WARSAW_NOON) == ["sms"]
    assert pick(routing, now=WARSAW_LATE) == ["email"]


def test_contract_window_keys_from_to_are_accepted():
    window = {"from": "08:00", "to": "20:00", "timezone": "Europe/Warsaw", "outsideChannels": ["email"]}
    routing = {"channelsBySeverity": {"critical": ["sms"]}, "timeWindow": window}
    assert pick(routing, now=WARSAW_NOON) == ["sms"]
    assert pick(routing, now=WARSAW_LATE) == ["email"]


def test_window_edges_and_overnight_and_garbage():
    window = {"start": "08:00", "end": "20:00", "timezone": "Europe/Warsaw"}
    assert in_window(window, datetime(2026, 11, 20, 7, 0, tzinfo=UTC))  # 08:00 local: inclusive start
    assert not in_window(window, datetime(2026, 11, 20, 19, 0, tzinfo=UTC))  # 20:00 local: exclusive end
    night = {"start": "22:00", "end": "06:00", "timezone": "UTC"}
    assert in_window(night, datetime(2026, 1, 1, 23, 0, tzinfo=UTC))
    assert in_window(night, datetime(2026, 1, 1, 5, 0, tzinfo=UTC))
    assert not in_window(night, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert in_window(None, WARSAW_NOON)
    assert in_window({"start": "nope", "end": "x"}, WARSAW_NOON)
    assert in_window({"start": "08:00", "end": "09:00", "timezone": "Mars/Base"}, WARSAW_NOON) is False


def test_outside_window_without_outside_channels_continues_selection():
    routing = {
        "channelsBySeverity": {"critical": ["phone", "sms"]},
        "timeWindow": {"start": "08:00", "end": "20:00"},
    }
    assert pick(routing, now=datetime(2026, 11, 20, 22, 0, tzinfo=UTC)) == ["phone", "sms"]


def test_channels_by_severity_beats_prefs_and_policy():
    routing = {"channelsBySeverity": {"critical": ["phone"], "warning": ["email"]}}
    prefs = {"by_severity": {"critical": ["sms"]}}
    assert pick(routing, prefs=prefs) == ["phone"]
    assert pick(routing, severity="warning", prefs=prefs) == ["email"]


def test_prefs_beat_policy_when_rule_has_no_mapping_for_severity():
    prefs = {"by_severity": {"critical": ["sms", "email"]}}
    assert pick({"channelsBySeverity": {"warning": ["email"]}}, prefs=prefs) == ["sms", "email"]


def test_prefs_respect_working_hours_for_non_critical():
    prefs = {
        "by_severity": {"warning": ["sms", "email"], "critical": ["sms", "email"]},
        "working_hours": {"from": "08:00", "to": "20:00", "timezone": "Europe/Warsaw"},
    }
    assert pick({}, severity="warning", prefs=prefs, now=WARSAW_NOON) == ["sms", "email"]
    assert pick({}, severity="warning", prefs=prefs, now=WARSAW_LATE) == ["email"]
    assert pick({}, severity="critical", prefs=prefs, now=WARSAW_LATE) == ["sms", "email"]


def test_prefs_only_interrupting_outside_hours_falls_through_to_policy():
    prefs = {
        "by_severity": {"warning": ["sms"]},
        "working_hours": {"from": "08:00", "to": "20:00", "timezone": "Europe/Warsaw"},
    }
    assert pick({}, severity="warning", prefs=prefs, now=WARSAW_LATE) == ["phone", "sms"]


def test_policy_level_channels_are_the_fallback():
    assert pick({}) == ["phone", "sms"]
    assert pick({}, pol=policy(("email",))) == ["email"]
    two_levels = EffectivePolicy(
        severity="critical",
        levels=[
            EscalationLevel(level=1, target="oncall", channels=["sms"], ackTimeoutSeconds=60),
            EscalationLevel(level=2, target="domain_root", channels=["phone"]),
        ],
    )
    assert pick({}, pol=two_levels, level=2) == ["phone"]
    assert pick({}, pol=two_levels, level=5) == ["phone"]  # beyond the chain: last level


def test_soft_recipients_never_get_sms_or_phone():
    routing = {"channelsBySeverity": {"critical": ["phone", "sms"]}}
    assert pick(routing, kind="soft") == ["email", "ui"]  # rule is interrupting only -> policy soft channels
    assert pick({"channelsBySeverity": {"critical": ["sms", "email"]}}, kind="soft") == ["email"]


def test_unknown_channels_dropped_and_final_fallback_is_email():
    assert pick({"channelsBySeverity": {"critical": ["pigeon", "sms"]}}) == ["sms"]
    empty = EffectivePolicy(severity="critical", levels=[], soft_channels=[])
    assert pick({}, pol=empty) == ["email"]
    assert pick({}, kind="soft", pol=empty) == ["email"]


def test_rule_view_accepts_dicts_objects_and_strings():
    d = {
        "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "teamId": "7c9e6679-7425-40de-944b-e07fc1f90ae8",
        "routing": {
            "hard": [{"kind": "oncall", "id": "team-rates"}, "user:anna@demo"],
            "soft": [SimpleNamespace(kind=SimpleNamespace(value="role"), id="team-rates:informed")],
            "channelsBySeverity": {"error": ["email", "ui"]},
        },
        "throttle": {"maxPerRecipientPerMinute": 3},
        "escalation": {"ackTimeoutSecondsBySeverity": {"critical": 30}, "path": ["master:"]},
    }
    view = rule_view(d)
    assert [(s.kind, s.id) for s in view.hard] == [("oncall", "team-rates"), ("user", "anna@demo")]
    assert [(s.kind, s.id) for s in view.soft] == [("role", "team-rates:informed")]
    assert view.channels_by_severity == {"error": ["email", "ui"]}
    assert view.max_per_minute == 3 and view.ack_timeout_by_severity == {"critical": 30}
    assert view.escalation_path[0].kind == "master" and str(view.id).startswith("7c9e6679")
    assert rule_view(None).hard == [] and rule_view(None).max_per_minute == 10
    obj = SimpleNamespace(id=None, team_id=None, routing={"hard": [{"kind": "team", "id": "x"}]})
    assert rule_view(obj).hard[0].kind == "team"


def test_rule_view_from_pydantic_rule_model():
    from app.schemas.api import Rule

    rule = Rule.model_validate(
        {
            "name": "r",
            "teamId": "t",
            "match": {"op": "and", "conditions": []},
            "routing": {
                "hard": [{"kind": "oncall", "id": "team-settlements"}],
                "channelsBySeverity": {"critical": ["phone", "sms"]},
                "timeWindow": {"from": "08:00", "to": "20:00", "timezone": "Europe/Warsaw"},
            },
        }
    )
    view = rule_view(rule)
    assert view.hard[0].kind == "oncall" and view.hard[0].id == "team-settlements"
    assert view.channels_by_severity["critical"] == ["phone", "sms"]
    assert view.time_window["to"] == "20:00"


@pytest.mark.parametrize("bad", [None, 5, {"x": 1}])
def test_rule_view_tolerates_garbage(bad):
    if bad == 5:
        bad = SimpleNamespace()
    assert rule_view(bad).hard == []
