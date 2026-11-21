"""Automatic incident manager: the pure decision model, its facts from the database, recording, backfill."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import uuid7
from app.core.scope import load_scope
from app.escalation import auto_assign as aa
from app.escalation.auto_assign import AUTO_ACTOR, Candidate, Decision, Facts, decide
from tests.delivery.helpers import domain, make_alert, team, user

HOUR = timedelta(hours=1)
CORE_USERS = (
    "master@demo",
    "root.payments@demo",
    "root.settlements@demo",
    "root.security@demo",
    "anna@demo",
    "piotr@demo",
    "manager@demo",
)


def cand(name: str, open_alerts: int = 0, eligible: bool = True) -> Candidate:
    return Candidate(uuid.uuid5(uuid.NAMESPACE_DNS, name), name, open_alerts, eligible)


# ---------------------------------------------------------------- the pure decision model


def test_named_person_wins_over_everything():
    facts = Facts(
        "Big trades", "Team Rates", named=[cand("Nina")], oncall=[cand("Olga")], members=[cand("M")]
    )
    assert decide(facts) == Decision(cand("Nina").id, "Nina", "named in rule Big trades")


def test_named_but_informed_falls_through_to_on_call():
    facts = Facts("R", "Team Payments", named=[cand("Marta", eligible=False)], oncall=[cand("Anna")])
    assert decide(facts) == Decision(cand("Anna").id, "Anna", "on call for Team Payments")


def test_several_on_call_least_busy_then_stable_order():
    busy, idle, idle2 = cand("Anna", 3), cand("Piotr", 1), cand("Kasia", 1)
    assert decide(Facts("R", "T", oncall=[busy, idle, idle2])).name == "Piotr"
    assert decide(Facts("R", "T", oncall=[idle2, idle])).name == "Kasia"


def test_members_least_busy_when_nobody_is_on_call():
    facts = Facts("R", "Team X", members=[cand("A", 2), cand("B", 0, eligible=False), cand("C", 1)])
    assert decide(facts) == Decision(cand("C").id, "C", "least busy in Team X")


def test_ineligible_on_call_falls_through_to_members():
    facts = Facts("R", "T", oncall=[cand("Manager", eligible=False)], members=[cand("A")])
    assert decide(facts).reason == "least busy in T"


def test_nobody_eligible_is_no_decision():
    assert decide(Facts("R", "T")) is None
    assert decide(Facts("R", "T", members=[cand("informed", eligible=False)])) is None


# ---------------------------------------------------------------- against the seeded (core) org


@pytest.fixture
async def fresh_team(seeded):
    """An empty team in the Payments domain; its alerts, shifts and members are removed afterwards."""
    team_id = uuid7()
    await seeded.execute(
        "INSERT INTO teams (id, slug, name) VALUES ($1, $2, 'Team Fresh')", team_id, f"aa-{team_id.hex[:8]}"
    )
    await seeded.execute(
        "INSERT INTO team_domains (team_id, domain_id) VALUES ($1, $2)", team_id, domain("payments")
    )
    yield team_id
    await seeded.execute(
        "DELETE FROM timeline WHERE alert_id IN (SELECT id FROM alerts WHERE team_id = $1)", team_id
    )
    await seeded.execute("DELETE FROM alerts WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM shifts WHERE team_id = $1", team_id)
    await seeded.execute("DELETE FROM role_assignments WHERE scope_id = $1", team_id)
    await seeded.execute("DELETE FROM teams WHERE id = $1", team_id)


async def add_members(pool, team_id, *emails):
    for email in emails:
        await pool.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES ($1, $2)", team_id, user(email)
        )
        await pool.execute(
            "INSERT INTO role_assignments (user_id, role, scope_type, scope_id) "
            "VALUES ($1, 'member', 'team', $2)",
            user(email),
            team_id,
        )


async def add_shift(pool, team_id, email, kind="manual"):
    now = datetime.now(UTC)
    await pool.execute(
        "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind) VALUES ($1, $2, $3, $4, $5, $6)",
        uuid7(),
        team_id,
        user(email),
        now - HOUR,
        now + HOUR,
        kind,
    )


async def fresh_alert(pool, team_id, assignee=None):
    row = await pool.fetchrow(
        "INSERT INTO alerts (id, team_id, domain_id, fingerprint, title, severity, assignee_id) "
        "VALUES ($1, $2, $3, $4, 'fresh', 'error', $5) RETURNING *",
        uuid7(),
        team_id,
        domain("payments"),
        uuid.uuid4().hex,
        assignee,
    )
    return dict(row)


async def run(pool, alert, rule=None):
    async with pool.acquire() as conn, conn.transaction():
        return await aa.auto_assign(conn, alert, rule)


def rule_naming(*refs: str, name: str = "Named rule") -> dict:
    return {"name": name, "routing": {"hard": [{"kind": "user", "id": r} for r in refs], "soft": []}}


async def test_rule_naming_a_person_assigns_them_and_records_it(seeded):
    alert = await make_alert(seeded, "team-payments")
    decision = await run(seeded, alert, rule_naming("root.payments@demo"))
    assert decision.user_id == user("root.payments@demo")
    assert decision.reason == "named in rule Named rule"
    row = await seeded.fetchrow("SELECT * FROM alerts WHERE id = $1", alert["id"])
    assert row["assignee_id"] == user("root.payments@demo")
    assert row["assigned_by"] == AUTO_ACTOR and row["assign_reason"] == "named in rule Named rule"
    entry = await seeded.fetchrow("SELECT * FROM timeline WHERE alert_id = $1", alert["id"])
    assert entry["type"] == "assigned" and entry["actor"]["name"] == "auto-assign"
    assert entry["text"] == "Assigned to Paulina Payments automatically: named in rule Named rule"
    assert entry["data"] == {
        "assigneeId": str(user("root.payments@demo")),
        "reason": "named in rule Named rule",
        "auto": True,
    }
    logged = await seeded.fetchrow(
        "SELECT actor, data FROM audit_log WHERE action = 'alert.auto_assign' AND target_id = $1",
        str(alert["id"]),
    )
    assert logged["actor"]["type"] == "system" and logged["data"]["auto"] is True


async def test_informed_person_named_in_rule_is_skipped_for_the_on_call(seeded):
    alert = await make_alert(seeded, "team-payments")
    decision = await run(seeded, alert, rule_naming("manager@demo", str(uuid.uuid4())))
    assert decision.user_id == user("anna@demo")
    assert decision.reason == "on call for Team Payments"


async def test_seeded_on_call_owns_new_alerts(seeded):
    alert = await make_alert(seeded, "team-settlements")
    decision = await run(seeded, alert, {"name": "x", "routing": {"hard": [{"kind": "oncall", "id": ""}]}})
    assert decision == Decision(user("piotr@demo"), "Piotr Wisniewski", "on call for Team Settlements")


async def test_several_on_call_the_least_busy_one_is_chosen(seeded, fresh_team):
    await add_members(seeded, fresh_team, "anna@demo", "piotr@demo")
    await add_shift(seeded, fresh_team, "anna@demo")
    await add_shift(seeded, fresh_team, "piotr@demo")
    await fresh_alert(seeded, fresh_team, assignee=user("anna@demo"))
    decision = await run(seeded, await fresh_alert(seeded, fresh_team))
    assert decision.user_id == user("piotr@demo") and decision.reason == "on call for Team Fresh"


async def test_without_shifts_the_least_busy_member_and_ties_by_email(seeded, fresh_team):
    await add_members(seeded, fresh_team, "piotr@demo", "anna@demo")
    first = await run(seeded, await fresh_alert(seeded, fresh_team))
    assert first.user_id == user("anna@demo") and first.reason == "least busy in Team Fresh"
    second = await run(seeded, await fresh_alert(seeded, fresh_team))
    assert second.user_id == user("piotr@demo")


async def test_team_without_members_stays_unassigned_with_a_warning(seeded, fresh_team, caplog):
    alert = await fresh_alert(seeded, fresh_team)
    with caplog.at_level(logging.WARNING, logger="app.escalation.auto_assign"):
        assert await run(seeded, alert) is None
    assert "left unassigned" in caplog.text
    assert await seeded.fetchval("SELECT assignee_id FROM alerts WHERE id = $1", alert["id"]) is None


async def test_informed_only_people_are_never_chosen(seeded, fresh_team):
    await seeded.execute(
        "INSERT INTO role_assignments (user_id, role, scope_type, scope_id) "
        "VALUES ($1, 'informed', 'team', $2)",
        user("manager@demo"),
        fresh_team,
    )
    await add_shift(seeded, fresh_team, "manager@demo")
    assert await run(seeded, await fresh_alert(seeded, fresh_team), rule_naming("manager@demo")) is None


async def test_a_human_assignment_or_release_is_never_overwritten(seeded):
    taken = await make_alert(seeded, "team-payments")
    await seeded.execute(
        "UPDATE alerts SET assignee_id = $2 WHERE id = $1", taken["id"], user("root.payments@demo")
    )
    assert await run(seeded, taken) is None
    released = await make_alert(seeded, "team-payments")
    await seeded.execute(
        "UPDATE alerts SET assigned_by = $2 WHERE id = $1", released["id"], {"type": "user", "id": "x"}
    )
    assert await run(seeded, released) is None
    rows = await seeded.fetch(
        "SELECT assignee_id FROM alerts WHERE id = ANY($1) ORDER BY created_at", [taken["id"], released["id"]]
    )
    assert [r["assignee_id"] for r in rows] == [user("root.payments@demo"), None]


async def test_eligibility_matches_scope_and_excludes_informed(seeded):
    teams = [team("team-payments"), team("team-settlements"), team("team-security")]
    async with seeded.acquire() as conn:
        for team_id in teams:
            alert = {
                "team_id": team_id,
                "domain_id": await conn.fetchval(
                    "SELECT domain_id FROM team_domains WHERE team_id = $1", team_id
                ),
            }
            rows = await conn.fetch(
                aa._CANDIDATES, [user(e) for e in CORE_USERS], team_id, alert["domain_id"]
            )
            eligible = {r["id"]: r["eligible"] for r in rows}
            for email in CORE_USERS:
                scope = await load_scope(conn, user(email))
                informed_only = {r.role for r in scope.roles} == {"informed"}
                assert eligible[user(email)] == (scope.can_see_team(team_id) and not informed_only), email


async def test_assign_new_alert_never_fails_the_caller(seeded, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(aa, "gather_facts", boom)
    alert = await make_alert(seeded, "team-payments")
    async with seeded.acquire() as conn, conn.transaction():
        assert await aa.assign_new_alert(conn, alert, None) is None
        assert await conn.fetchval("SELECT 1") == 1  # the transaction is still usable


async def test_backfill_assigns_untouched_open_alerts_once(seeded, fresh_team):
    await add_members(seeded, fresh_team, "anna@demo")
    open_one = await fresh_alert(seeded, fresh_team)
    released = await fresh_alert(seeded, fresh_team)
    await seeded.execute(
        "UPDATE alerts SET assigned_by = $2 WHERE id = $1", released["id"], {"type": "user", "id": "x"}
    )
    resolved = await fresh_alert(seeded, fresh_team)
    await seeded.execute("UPDATE alerts SET status = 'resolved' WHERE id = $1", resolved["id"])

    assert await aa.backfill_open(seeded, [fresh_team]) == 1
    assert await aa.backfill_open(seeded, [fresh_team]) == 0
    owners = {
        r["id"]: r["assignee_id"]
        for r in await seeded.fetch("SELECT id, assignee_id FROM alerts WHERE team_id = $1", fresh_team)
    }
    assert owners == {open_one["id"]: user("anna@demo"), released["id"]: None, resolved["id"]: None}
