"""Escalation policy API: CRUD per scope, effective policy with origins, permissions, validation."""

from __future__ import annotations

import pytest

from app.core.ids import ORG_ID
from tests.escalation.helpers import make_rule, team

BASE = "/v1/escalation-policies"
SETTLEMENTS = "team-settlements"

LEVELS = [
    {"level": 1, "target": "oncall", "channels": ["sms"], "ackTimeoutSeconds": 120},
    {"level": 2, "target": "domain_root", "channels": ["sms", "email"], "ackTimeoutSeconds": None},
]


def policy(scope_type, scope_id, severity="critical", **fields):
    return {"scopeType": scope_type, "scopeId": str(scope_id), "severity": severity, **fields}


@pytest.fixture(autouse=True)
async def clean_policies(seeded):
    """Non-org policies created by these tests are removed; org defaults are restored."""
    org = await seeded.fetch("SELECT * FROM escalation_policies WHERE scope_type = 'org'")
    yield
    await seeded.execute("DELETE FROM escalation_policies WHERE scope_type <> 'org'")
    await seeded.execute("DELETE FROM escalation_policies WHERE scope_type = 'org'")
    for r in org:
        await seeded.execute(
            "INSERT INTO escalation_policies (scope_type, scope_id, severity, levels, renotify_seconds, "
            "soft_channels, soft_digest_minutes) VALUES ('org', $1, $2, $3, $4, $5, $6)",
            r["scope_id"],
            r["severity"],
            r["levels"],
            r["renotify_seconds"],
            r["soft_channels"],
            r["soft_digest_minutes"],
        )


async def test_list_org_policies_is_visible_to_everybody(client, auth_headers):
    resp = await client.get(
        BASE, params={"scopeType": "org", "scopeId": "org"}, headers=await auth_headers("piotr@demo")
    )
    assert resp.status_code == 200
    by_severity = {p["severity"]: p for p in resp.json()}
    assert list(by_severity) == ["critical", "error", "warning", "info"]
    assert by_severity["critical"]["scopeId"] == str(ORG_ID)
    assert [lv["target"] for lv in by_severity["critical"]["levels"]] == ["oncall", "domain_root", "master"]
    assert by_severity["critical"]["renotifySeconds"] == 120


async def test_master_edits_org_defaults(client, auth_headers):
    headers = await auth_headers("master@demo")
    body = policy("org", ORG_ID, "warning", levels=LEVELS, renotifySeconds=900, softChannels=["ui"])
    saved = await client.put(BASE, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["renotifySeconds"] == 900 and saved.json()["levels"][0]["ackTimeoutSeconds"] == 120
    listed = await client.get(BASE, params={"scopeType": "org", "scopeId": str(ORG_ID)}, headers=headers)
    warning = next(p for p in listed.json() if p["severity"] == "warning")
    assert warning["softChannels"] == ["ui"] and len(warning["levels"]) == 2


@pytest.mark.parametrize("email", ["piotr@demo", "root.settlements@demo"])
async def test_only_master_edits_org(client, auth_headers, email):
    resp = await client.put(
        BASE, json=policy("org", ORG_ID, levels=LEVELS), headers=await auth_headers(email)
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


async def test_domain_root_edits_team_policy_in_their_domain_only(client, auth_headers):
    body = policy("team", SETTLEMENTS, renotifySeconds=77)
    ok = await client.put(BASE, json=body, headers=await auth_headers("root.settlements@demo"))
    assert ok.status_code == 200 and ok.json()["scopeId"] == str(team(SETTLEMENTS))
    member = await client.put(BASE, json=body, headers=await auth_headers("piotr@demo"))
    assert member.status_code == 403
    other_domain = await client.put(BASE, json=body, headers=await auth_headers("root.payments@demo"))
    assert other_domain.status_code == 404  # a team outside the caller's scope does not exist for them


async def test_domain_root_edits_domain_policy(client, auth_headers):
    body = policy("domain", "settlements", "error", levels=LEVELS)
    assert (
        await client.put(BASE, json=body, headers=await auth_headers("root.settlements@demo"))
    ).status_code == 200
    denied = await client.put(BASE, json=body, headers=await auth_headers("root.security@demo"))
    assert denied.status_code == 404
    assert (await client.put(BASE, json=body, headers=await auth_headers("master@demo"))).status_code == 200


async def test_rule_owner_team_member_edits_rule_policy(seeded, client, auth_headers):
    rule_id = await make_rule(seeded)
    body = policy("rule", rule_id, softDigestMinutes=30)
    assert (await client.put(BASE, json=body, headers=await auth_headers("piotr@demo"))).status_code == 200
    assert (await client.put(BASE, json=body, headers=await auth_headers("anna@demo"))).status_code == 404
    informed = await client.put(BASE, json=body, headers=await auth_headers("manager@demo"))
    assert informed.status_code == 403  # sees the team, but informed is read-only
    listed = await client.get(
        BASE, params={"scopeType": "rule", "scopeId": str(rule_id)}, headers=await auth_headers("piotr@demo")
    )
    assert [p["softDigestMinutes"] for p in listed.json()] == [30]


async def test_effective_policy_merges_scopes_and_reports_origins(seeded, client, auth_headers):
    root = await auth_headers("root.settlements@demo")
    master = await auth_headers("master@demo")
    params = {"teamId": SETTLEMENTS, "severity": "critical"}

    base = (await client.get(f"{BASE}/effective", params=params, headers=root)).json()
    assert base["scopeType"] == "team" and base["scopeId"] == str(team(SETTLEMENTS))
    assert base["origin"] == {
        "levels": "org",
        "renotifySeconds": "org",
        "softChannels": "org",
        "softDigestMinutes": "org",
    }

    await client.put(BASE, json=policy("domain", "settlements", renotifySeconds=300), headers=root)
    await client.put(BASE, json=policy("team", SETTLEMENTS, levels=LEVELS), headers=root)
    merged = (await client.get(f"{BASE}/effective", params=params, headers=root)).json()
    assert merged["origin"]["levels"] == "team" and merged["origin"]["renotifySeconds"] == "domain"
    assert merged["origin"]["softChannels"] == "org"
    assert merged["renotifySeconds"] == 300 and len(merged["levels"]) == 2
    assert merged["softChannels"] == ["email", "ui"]

    rule_id = await make_rule(seeded, escalation={"ackTimeoutSecondsBySeverity": {"critical": 45}})
    by_rule = (
        await client.get(
            f"{BASE}/effective", params={"ruleId": str(rule_id), "severity": "critical"}, headers=root
        )
    ).json()
    assert by_rule["scopeType"] == "rule" and by_rule["origin"]["levels"] == "rule"
    assert by_rule["levels"][0]["ackTimeoutSeconds"] == 45

    removed = await client.delete(
        BASE, params={"scopeType": "team", "scopeId": SETTLEMENTS, "severity": "critical"}, headers=root
    )
    assert removed.status_code == 204
    reverted = (await client.get(f"{BASE}/effective", params=params, headers=master)).json()
    assert reverted["origin"]["levels"] == "org" and reverted["origin"]["renotifySeconds"] == "domain"


async def test_effective_for_a_team_in_two_domains_uses_the_requested_domain(client, auth_headers):
    master = await auth_headers("master@demo")
    await client.put(BASE, json=policy("domain", "security", renotifySeconds=33), headers=master)
    params = {"teamId": "checkout-project", "severity": "critical"}
    first = (await client.get(f"{BASE}/effective", params=params, headers=master)).json()  # payments first
    assert first["origin"]["renotifySeconds"] == "org"
    chosen = (
        await client.get(f"{BASE}/effective", params={**params, "domainId": "security"}, headers=master)
    ).json()
    assert chosen["renotifySeconds"] == 33 and chosen["origin"]["renotifySeconds"] == "domain"
    wrong = await client.get(
        f"{BASE}/effective", params={**params, "domainId": "settlements"}, headers=master
    )
    assert wrong.status_code == 404


async def test_effective_requires_team_or_rule_and_visibility(client, auth_headers):
    headers = await auth_headers("piotr@demo")
    assert (
        await client.get(f"{BASE}/effective", params={"severity": "critical"}, headers=headers)
    ).status_code == 422
    hidden = await client.get(
        f"{BASE}/effective", params={"teamId": "team-payments", "severity": "critical"}, headers=headers
    )
    assert hidden.status_code == 404
    unknown = await client.get(
        f"{BASE}/effective", params={"ruleId": "nope", "severity": "critical"}, headers=headers
    )
    assert unknown.status_code == 404


async def test_delete_is_idempotent_and_needs_permission(client, auth_headers):
    params = {"scopeType": "team", "scopeId": SETTLEMENTS, "severity": "info"}
    assert (
        await client.delete(BASE, params=params, headers=await auth_headers("root.settlements@demo"))
    ).status_code == 204
    assert (
        await client.delete(BASE, params=params, headers=await auth_headers("root.settlements@demo"))
    ).status_code == 204
    assert (
        await client.delete(BASE, params=params, headers=await auth_headers("piotr@demo"))
    ).status_code == 403
    org = {"scopeType": "org", "scopeId": "org", "severity": "info"}
    assert (
        await client.delete(BASE, params=org, headers=await auth_headers("root.settlements@demo"))
    ).status_code == 403


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"levels": [{**LEVELS[0], "level": 2}]}, "levels[0].level"),
        ({"levels": [{**LEVELS[0], "channels": []}]}, "channels"),
        ({"levels": [{"level": 1, "target": "custom", "channels": ["sms"]}]}, "recipients"),
        ({"levels": [{**LEVELS[0], "ackTimeoutSeconds": 0}]}, "ackTimeoutSeconds"),
        ({"renotifySeconds": -1}, "renotifySeconds"),
        ({"softDigestMinutes": -5}, "softDigestMinutes"),
        ({"severity": "fatal"}, "severity"),
        ({"scopeType": "planet"}, "scopeType"),
    ],
)
async def test_invalid_policies_are_rejected_with_the_field_path(client, auth_headers, override, message):
    body = {**policy("team", SETTLEMENTS), **override}
    resp = await client.put(BASE, json=body, headers=await auth_headers("master@demo"))
    assert resp.status_code == 422
    assert message in resp.json()["message"]


async def test_custom_target_levels_round_trip(client, auth_headers):
    custom = {
        "level": 1,
        "target": "custom",
        "recipients": [{"kind": "user", "id": "anna@demo"}],
        "channels": ["email"],
        "ackTimeoutSeconds": 600,
    }
    master = await auth_headers("master@demo")
    saved = await client.put(BASE, json=policy("team", SETTLEMENTS, levels=[custom]), headers=master)
    assert saved.json()["levels"][0]["recipients"] == [{"kind": "user", "id": "anna@demo"}]


async def test_unknown_scopes_are_not_found_and_auth_is_required(client, auth_headers):
    master = await auth_headers("master@demo")
    assert (
        await client.get(BASE, params={"scopeType": "team", "scopeId": "nope"}, headers=master)
    ).status_code == 404
    assert (
        await client.get(BASE, params={"scopeType": "domain", "scopeId": "nope"}, headers=master)
    ).status_code == 404
    assert (
        await client.get(BASE, params={"scopeType": "org", "scopeId": "nope"}, headers=master)
    ).status_code == 404
    assert (await client.get(BASE, params={"scopeType": "org", "scopeId": "org"})).status_code == 401
