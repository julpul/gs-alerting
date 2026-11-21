"""Rules REST API: CRUD, versions, YAML, dry-run, validation paths, scope, AI copilot degradation."""

from __future__ import annotations

import asyncio
import sys
import time
import types
from uuid import UUID, uuid4

import orjson
import pytest
import yaml

from app.core.ids import stable_id
from app.core.redis import rkey
from app.pipeline.rules.model import parse_rule

RATES = str(stable_id("team", "team-rates"))
PAYMENTS = str(stable_id("team", "team-payments"))


def body(team: str = "team-payments", name: str | None = None, **extra) -> dict:
    return {
        "name": name or f"rule-{uuid4().hex[:8]}",
        "teamId": team,
        "priority": "error",
        "match": {
            "op": "and",
            "conditions": [
                {"field": "labels.service", "operator": "eq", "value": f"svc-{uuid4().hex[:8]}"},
                {"field": "labels.severity", "operator": "in", "value": ["critical", "error"]},
            ],
        },
        "routing": {
            "hard": [{"kind": "user", "id": "anna@demo"}],
            "channelsBySeverity": {"critical": ["sms"]},
        },
        "presentation": {"titleTemplate": "T {{labels.service}}", "fields": ["labels.service"]},
        **extra,
    }


@pytest.fixture
async def made(seeded):
    """Rule ids created by a test; removed afterwards so seeded-rule assertions elsewhere stay exact."""
    ids: list[str] = []
    yield ids
    uuids = [UUID(i) for i in ids]
    await seeded.execute("DELETE FROM rule_versions WHERE rule_id = ANY($1)", uuids)
    await seeded.execute("DELETE FROM rules WHERE id = ANY($1)", uuids)


async def create(client, headers, made, **kwargs):
    resp = await client.post("/v1/rules", json=body(**kwargs), headers=headers)
    assert resp.status_code == 201, resp.text
    made.append(resp.json()["id"])
    return resp.json()


async def test_create_get_list_roundtrip(client, auth_headers, made):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made, team="team-payments")
    assert created["version"] == 1 and created["teamId"] == PAYMENTS  # slug normalised to the uuid
    assert created["enabled"] is True and created["dedup"]["windowSeconds"] == 300
    assert created["routing"]["hard"] == [{"kind": "user", "id": "anna@demo"}]

    got = await client.get(f"/v1/rules/{created['id']}", headers=h)
    assert got.status_code == 200 and got.json() == created

    listing = await client.get("/v1/rules", headers=h)
    assert listing.status_code == 200
    row = next(r for r in listing.json() if r["id"] == created["id"])
    assert row["name"] == created["name"] and row["teamId"] == PAYMENTS and row["enabled"] is True
    assert row["owner"]["displayName"] == "Anna Kowalska" and row["fires24h"] == 0
    assert row["lastFiredAt"] is None

    by_team = await client.get("/v1/rules", params={"teamId": "team-payments"}, headers=h)
    assert created["id"] in {r["id"] for r in by_team.json()}
    other = await client.get("/v1/rules", params={"teamId": "no-such-team"}, headers=h)
    assert other.json() == []


async def test_list_is_scoped_to_the_callers_teams(client, auth_headers, made):
    master = await auth_headers("master@demo")
    mine = await create(client, master, made, team="team-rates")
    anna = await auth_headers("anna@demo")
    assert mine["id"] not in {r["id"] for r in (await client.get("/v1/rules", headers=anna)).json()}
    manager = await auth_headers("manager@demo")  # informed in team-rates
    assert mine["id"] in {r["id"] for r in (await client.get("/v1/rules", headers=manager)).json()}


async def test_update_creates_version_and_announces_change(client, auth_headers, made, redis):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made)
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("rules:changed"))
    try:
        updated_body = {**body(), "name": "renamed", "enabled": False}
        resp = await client.put(f"/v1/rules/{created['id']}", json=updated_body, headers=h)
        assert resp.status_code == 200, resp.text
        assert (
            resp.json()["version"] == 2
            and resp.json()["name"] == "renamed"
            and resp.json()["enabled"] is False
        )
        message = None
        for _ in range(20):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                break
        assert orjson.loads(message["data"]) == {"ruleId": created["id"], "version": 2}
    finally:
        await pubsub.aclose()

    versions = (await client.get(f"/v1/rules/{created['id']}/versions", headers=h)).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["changedBy"]["displayName"] == "Anna Kowalska" and versions[0]["changedAt"]
    assert (await client.get(f"/v1/rules/{created['id']}", headers=h)).json()["version"] == 2


async def test_delete_removes_rule_and_versions(client, auth_headers, made):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made)
    assert (await client.delete(f"/v1/rules/{created['id']}", headers=h)).status_code == 204
    assert (await client.get(f"/v1/rules/{created['id']}", headers=h)).status_code == 404
    assert (await client.get(f"/v1/rules/{created['id']}/versions", headers=h)).status_code == 404
    assert (await client.delete(f"/v1/rules/{created['id']}", headers=h)).status_code == 404
    assert (await client.get("/v1/rules/not-a-uuid", headers=h)).status_code == 404


async def test_invalid_rule_returns_422_with_node_path(client, auth_headers):
    h = await auth_headers("anna@demo")
    bad_op = body()
    bad_op["match"]["conditions"][1]["operator"] = "like"
    resp = await client.post("/v1/rules", json=bad_op, headers=h)
    assert resp.status_code == 422
    err = resp.json()
    assert err["code"] == "invalid_rule" and err["details"]["path"] == "match.conditions[1].operator"

    bad_regex = body()
    bad_regex["match"]["conditions"][0] = {"field": "a", "operator": "regex", "value": "(x+)+$"}
    resp = await client.post("/v1/rules", json=bad_regex, headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "match.conditions[0].value"

    bad_in = body()
    bad_in["match"]["conditions"][1]["value"] = "critical"
    resp = await client.put(f"/v1/rules/{uuid4()}", json=bad_in, headers=h)
    assert resp.status_code == 404  # unknown rule is checked before the body

    missing = body()
    del missing["match"]
    resp = await client.post("/v1/rules", json=missing, headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "match"

    resp = await client.post("/v1/rules", json=body(team="no-such-team"), headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "teamId"
    resp = await client.post("/v1/rules", json=[1], headers=h)
    assert resp.status_code == 422


async def test_invalid_update_does_not_bump_version(client, auth_headers, made):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made)
    broken = body()
    broken["routing"]["timeWindow"] = {"from": "25:99"}
    resp = await client.put(f"/v1/rules/{created['id']}", json=broken, headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "routing.timeWindow.from"
    assert (await client.get(f"/v1/rules/{created['id']}", headers=h)).json()["version"] == 1


async def test_scope_denial(client, auth_headers, made):
    master = await auth_headers("master@demo")
    rates_rule = await create(client, master, made, team="team-rates")
    rid = rates_rule["id"]

    piotr = await auth_headers("piotr@demo")  # member of team-settlements only
    assert (await client.get(f"/v1/rules/{rid}", headers=piotr)).status_code == 404
    assert (
        await client.put(f"/v1/rules/{rid}", json=body(team="team-rates"), headers=piotr)
    ).status_code == 404
    assert (await client.delete(f"/v1/rules/{rid}", headers=piotr)).status_code == 404
    assert (await client.get(f"/v1/rules/{rid}/versions", headers=piotr)).status_code == 404
    assert (await client.get(f"/v1/rules/{rid}/yaml", headers=piotr)).status_code == 404
    assert (await client.post("/v1/rules", json=body(team="team-rates"), headers=piotr)).status_code == 403

    manager = await auth_headers("manager@demo")  # informed: may read, never edit
    assert (await client.get(f"/v1/rules/{rid}", headers=manager)).status_code == 200
    assert (
        await client.put(f"/v1/rules/{rid}", json=body(team="team-rates"), headers=manager)
    ).status_code == 403
    assert (await client.delete(f"/v1/rules/{rid}", headers=manager)).status_code == 403
    assert (await client.post("/v1/rules", json=body(team="team-rates"), headers=manager)).status_code == 403

    unauth = await client.get(f"/v1/rules/{rid}")
    assert unauth.status_code == 401
    assert (await client.get(f"/v1/rules/{rid}")).json()["code"] == "unauthorized"
    assert (await client.get(f"/v1/rules/{rid}", headers=master)).status_code == 200


async def test_members_edit_own_team_and_domain_roots_their_domains(client, auth_headers, made):
    anna = await auth_headers("anna@demo")
    rule = await create(client, anna, made, team="team-payments")
    root_payments = await auth_headers("root.payments@demo")
    assert (
        await client.put(f"/v1/rules/{rule['id']}", json=body(), headers=root_payments)
    ).status_code == 200
    root_settlements = await auth_headers("root.settlements@demo")
    assert (await client.get(f"/v1/rules/{rule['id']}", headers=root_settlements)).status_code == 404
    # a domain root may also create for the cross-domain team of their domain
    ok = await client.post("/v1/rules", json=body(team="checkout-project"), headers=root_payments)
    assert ok.status_code == 201
    made.append(ok.json()["id"])
    # members cannot move a rule into a team they cannot edit
    moved = await client.put(f"/v1/rules/{rule['id']}", json=body(team="team-rates"), headers=anna)
    assert moved.status_code == 403


async def test_yaml_roundtrip(client, auth_headers, made):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made)
    resp = await client.get(f"/v1/rules/{created['id']}/yaml", headers=h)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/yaml")
    doc = yaml.safe_load(resp.text)
    assert doc["teamId"] == "team-payments" and "id" not in doc and "version" not in doc
    assert doc["match"]["conditions"][1]["operator"] == "in"

    # unchanged YAML saves the same definition as a new version
    same = await client.put(f"/v1/rules/{created['id']}/yaml", content=resp.text, headers=h)
    assert same.status_code == 200, same.text
    assert same.json()["version"] == 2
    assert {k: v for k, v in same.json().items() if k != "version"} == {
        k: v for k, v in created.items() if k != "version"
    }

    doc["presentation"]["titleTemplate"] = "Edited {{labels.service}} — ünïcode"
    edited = await client.put(
        f"/v1/rules/{created['id']}/yaml", content=yaml.safe_dump(doc, allow_unicode=True), headers=h
    )
    assert edited.status_code == 200
    again = await client.get(f"/v1/rules/{created['id']}/yaml", headers=h)
    assert (
        yaml.safe_load(again.text)["presentation"]["titleTemplate"] == "Edited {{labels.service}} — ünïcode"
    )
    assert (await client.get(f"/v1/rules/{created['id']}", headers=h)).json()["version"] == 3


async def test_yaml_errors_are_422(client, auth_headers, made):
    h = await auth_headers("anna@demo")
    created = await create(client, h, made)
    url = f"/v1/rules/{created['id']}/yaml"
    for text in ("- just\n- a list\n", "a: [unclosed", "x: &a 1\ny: *a\n"):
        resp = await client.put(url, content=text, headers=h)
        assert resp.status_code == 422 and resp.json()["code"] == "invalid_rule"
    bad = yaml.safe_dump(
        {**body(), "match": {"op": "and", "conditions": [{"field": "a", "operator": "zzz"}]}}
    )
    resp = await client.put(url, content=bad, headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "match.conditions[0].operator"
    assert (await client.get(url, headers=h)).status_code == 200
    assert (await client.put(url, content="a: " + "b" * 200_000, headers=h)).status_code == 422


async def _seed_stream(redis, source_id: UUID, events: list[dict], old: list[dict] = ()):
    key = rkey("recent", str(source_id))
    await redis.delete(key)
    two_hours_ago = int(time.time() * 1000) - 2 * 3600 * 1000
    for i, raw in enumerate(old):
        await redis.xadd(
            key,
            {"envelope": orjson.dumps({"event_id": f"old{i}", "raw": raw}).decode()},
            id=f"{two_hours_ago + i}-0",
        )
    for i, raw in enumerate(events):
        env = {
            "event_id": f"e{i}",
            "received_at": "2026-11-21T09:00:00.000Z",
            "raw": raw,
            "_meta": {"schema": "trade.rejected.v2"},
        }
        await redis.xadd(key, {"envelope": orjson.dumps(env).decode()})
    return key


async def test_dry_run_counts_dedup_samples_and_notifications(client, auth_headers, redis):
    h = await auth_headers("anna@demo")
    team = "team-payments"
    source_id = stable_id("source", "prometheus")  # team-payments
    trades = [
        {"labels": {"service": "svc-x", "severity": "critical"}, "id": i % 2, "n": 20_000_000}
        for i in range(4)
    ]
    trades.append({"labels": {"service": "svc-x", "severity": "critical"}, "id": 9, "n": 5})
    trades.append({"labels": {"service": "other", "severity": "critical"}, "id": 9, "n": 99_000_000})
    key = await _seed_stream(
        redis, source_id, trades, old=[{"labels": {"service": "svc-x"}, "id": 7, "n": 99_000_000}]
    )
    try:
        rule = {
            "name": "dry",
            "teamId": team,
            "match": {
                "op": "and",
                "conditions": [
                    {"field": "labels.service", "operator": "eq", "value": "svc-x"},
                    {"field": "n", "operator": "gt", "value": 10_000_000},
                ],
            },
            "routing": {
                "hard": [{"kind": "user", "id": "anna@demo"}],
                "soft": [
                    {"kind": "role", "id": "team-payments:informed"},
                    {"kind": "team", "id": "team-payments"},
                ],
            },
            "dedup": {"fields": ["id"]},
        }
        resp = await client.post("/v1/rules/dry-run", json={"rule": rule, "lookbackMinutes": 60}, headers=h)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["evaluated"] == 6  # the 2 h old entry is outside the 60 min lookback
        assert result["matched"] == 4 and result["alertsAfterDedup"] == 2
        assert len(result["sampleMatches"]) == 4
        sample = result["sampleMatches"][0]
        assert (
            set(sample) == {"eventId", "receivedAt", "payload"}
            and sample["payload"]["labels"]["service"] == "svc-x"
        )
        assert sample["eventId"].startswith("e") and sample["receivedAt"].startswith("2026-11-21T09:00:00")
        notes = {(n["kind"], n["recipient"]["displayName"]): n["count"] for n in result["notifications"]}
        assert notes[("hard", "Anna Kowalska")] == 2
        assert notes[("soft", "Marta Manager")] == 2  # informed role of the team
        assert ("soft", "Anna Kowalska") in notes  # team members
        assert all(n["recipient"]["kind"] == "user" for n in result["notifications"])

        # grouping collapses the events by the group field
        rule["grouping"] = {"byField": "labels.service", "windowSeconds": 300}
        grouped = (await client.post("/v1/rules/dry-run", json={"rule": rule}, headers=h)).json()
        assert grouped["matched"] == 4 and grouped["alertsAfterDedup"] == 1

        # a wider lookback sees the old entry too
        rule.pop("grouping")
        wide = (
            await client.post("/v1/rules/dry-run", json={"rule": rule, "lookbackMinutes": 180}, headers=h)
        ).json()
        assert wide["evaluated"] == 7 and wide["matched"] == 5
    finally:
        await redis.delete(key)


async def test_dry_run_without_events_and_validation(client, auth_headers):
    h = await auth_headers("anna@demo")
    empty = await client.post("/v1/rules/dry-run", json={"rule": body()}, headers=h)
    assert empty.status_code == 200 and empty.json()["matched"] == 0 and empty.json()["notifications"] == []
    bad = body()
    bad["match"]["conditions"][0]["operator"] = "nope"
    resp = await client.post("/v1/rules/dry-run", json={"rule": bad}, headers=h)
    assert resp.status_code == 422 and resp.json()["details"]["path"] == "match.conditions[0].operator"
    denied = await client.post("/v1/rules/dry-run", json={"rule": body(team="team-rates")}, headers=h)
    assert denied.status_code == 403
    bad_lookback = await client.post(
        "/v1/rules/dry-run", json={"rule": body(), "lookbackMinutes": 0}, headers=h
    )
    assert bad_lookback.status_code == 422


async def test_from_text_degrades_to_503_without_the_ai_module(client, auth_headers, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.ai.rule_copilot", None)  # forces ImportError
    h = await auth_headers("anna@demo")
    resp = await client.post(
        "/v1/rules/from-text", json={"text": "page me", "teamId": "team-payments"}, headers=h
    )
    assert resp.status_code == 503 and resp.json()["code"] == "ai_unavailable"


async def test_from_text_uses_the_copilot_and_dry_runs_the_draft(client, auth_headers, monkeypatch):
    calls = []

    async def draft_rule(text, scope, conn, team_id=None):
        calls.append(text)
        return parse_rule(body(team=PAYMENTS)), "because"

    fake = types.ModuleType("app.ai.rule_copilot")
    fake.draft_rule = draft_rule
    monkeypatch.setitem(sys.modules, "app.ai.rule_copilot", fake)
    h = await auth_headers("anna@demo")
    resp = await client.post(
        "/v1/rules/from-text", json={"text": "page me", "teamId": "team-payments"}, headers=h
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert calls == ["page me"] and out["explanation"] == "because"
    assert out["draft"]["name"].startswith("rule-") and "id" not in out["draft"]
    assert out["dryRun"]["matched"] == 0
    denied = await client.post("/v1/rules/from-text", json={"text": "x", "teamId": "team-rates"}, headers=h)
    assert denied.status_code == 403

    async def broken(text, scope, conn, team_id=None):
        raise RuntimeError("provider down")

    fake.draft_rule = broken
    resp = await client.post("/v1/rules/from-text", json={"text": "x", "teamId": "team-payments"}, headers=h)
    assert resp.status_code == 503 and resp.json()["code"] == "ai_unavailable"


async def test_update_is_visible_to_a_rule_index_within_two_seconds(client, auth_headers, made, pool):
    from app.pipeline.fields import EventView
    from app.pipeline.rules.index import RuleIndex

    h = await auth_headers("anna@demo")
    index = RuleIndex()
    await index.load_all(pool)
    task = await index.start_hot_reload(pool)
    try:
        started = time.perf_counter()
        created = await create(client, h, made)
        rule_id = UUID(created["id"])
        await _until(lambda: index.get(rule_id) is not None)
        assert time.perf_counter() - started < 2

        started = time.perf_counter()
        new_body = body()
        new_body["match"]["conditions"] = [{"field": "marker", "operator": "eq", "value": "v2"}]
        assert (await client.put(f"/v1/rules/{created['id']}", json=new_body, headers=h)).status_code == 200
        await _until(lambda: index.get(rule_id).rule.version == 2)
        assert time.perf_counter() - started < 2
        assert index.get(rule_id).matches(EventView({"marker": "v2"}))

        disabled = {**new_body, "enabled": False}
        await client.put(f"/v1/rules/{created['id']}", json=disabled, headers=h)
        await _until(lambda: index.get(rule_id) is None)

        await client.delete(f"/v1/rules/{created['id']}", headers=h)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _until(predicate, within: float = 2.0) -> None:
    deadline = time.perf_counter() + within
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")
