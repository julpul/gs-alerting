"""The full Vistula Capital organization (SEED_PROFILE=full) in its own database."""

import time
from datetime import UTC, datetime

from app.core.ids import ORG_ID, stable_id
from app.core.scope import load_scope
from app.core.security import verify_password
from app.delivery.channel_select import _from_prefs
from app.escalation.oncall import oncall_from_shifts
from seed.seed import load_fixtures, run_seed

ORIGINAL = {
    "master@demo": "+48500100100",
    "root.payments@demo": "+48500100110",
    "root.settlements@demo": "+48500100120",
    "root.security@demo": "+48500100130",
    "anna@demo": "+48500100200",
    "piotr@demo": "+48500100201",
    "manager@demo": "+48500100300",
}
TABLES = (
    "users",
    "domains",
    "teams",
    "team_domains",
    "team_members",
    "role_assignments",
    "preferences",
    "alerts",
    "timeline",
    "resolutions",
    "knowledge_docs",
    "knowledge_chunks",
    "rules",
    "sources",
)


async def _counts(pool) -> dict[str, int]:
    return {t: await pool.fetchval(f"SELECT count(*) FROM {t}") for t in TABLES}


async def test_seed_twice_gives_identical_counts(full_pool):
    before = await _counts(full_pool)
    assert before["users"] >= 25 and before["teams"] == 6 and before["domains"] == 4
    started = time.perf_counter()
    await run_seed(full_pool)
    assert time.perf_counter() - started < 3
    await run_seed(full_pool)
    assert await _counts(full_pool) == before


async def test_original_demo_identities_are_intact(full_pool):
    rows = {r["email"]: r for r in await full_pool.fetch("SELECT id, email, phone, display_name FROM users")}
    for email, phone in ORIGINAL.items():
        assert rows[email]["id"] == stable_id("user", email)
        assert rows[email]["phone"] == phone
    assert rows["anna@demo"]["display_name"] == "Anna Kowalska"
    assert rows["piotr@demo"]["display_name"] == "Piotr Wisniewski"
    slugs = {r["slug"] for r in await full_pool.fetch("SELECT slug FROM teams")}
    assert slugs == {
        "team-payments",
        "team-settlements",
        "team-pricing",
        "team-rates",
        "team-security",
        "checkout-project",
    }
    domains = {r["slug"] for r in await full_pool.fetch("SELECT slug FROM domains")}
    assert domains == {"payments", "settlements", "security", "markets"}


async def test_everybody_logs_in_with_password_demo(full_pool):
    hashes = await full_pool.fetch("SELECT email, password_hash FROM users")
    assert len(hashes) >= 25
    assert all(verify_password("demo", r["password_hash"]) for r in hashes[:3])
    assert all(r["password_hash"] for r in hashes)


async def test_every_team_has_at_least_two_members(full_pool):
    rows = await full_pool.fetch(
        "SELECT t.slug, count(m.user_id) AS n FROM teams t LEFT JOIN team_members m ON m.team_id = t.id "
        "GROUP BY t.slug"
    )
    assert len(rows) == 6
    assert all(r["n"] >= 2 for r in rows)


async def test_roles_and_hierarchy(full_pool):
    roles = {
        (r["email"], r["role"], r["scope_type"])
        for r in await full_pool.fetch(
            "SELECT u.email, ra.role, ra.scope_type FROM role_assignments ra "
            "JOIN users u ON u.id = ra.user_id"
        )
    }
    assert ("master@demo", "master", "org") in roles
    assert ("root.markets@demo", "domain_root", "domain") in roles
    assert ("sophie.bennett@demo", "informed", "team") in roles
    assert await full_pool.fetchval(
        "SELECT count(*) FROM role_assignments WHERE role = 'master' AND scope_id = $1", ORG_ID
    )
    manager = {
        r["email"]: r["manager"]
        for r in await full_pool.fetch(
            "SELECT u.email, m.email AS manager FROM users u LEFT JOIN users m ON m.id = u.manager_id"
        )
    }
    assert manager["master@demo"] is None
    assert manager["marta.nowak@demo"] == "root.payments@demo"
    assert manager["anna@demo"] == "manager@demo"
    assert all(v is not None for k, v in manager.items() if k != "master@demo")


async def test_original_scopes_are_unchanged(full_pool):
    async with full_pool.acquire() as conn:
        anna = await load_scope(conn, stable_id("user", "anna@demo"))
        piotr = await load_scope(conn, stable_id("user", "piotr@demo"))
        manager = await load_scope(conn, stable_id("user", "manager@demo"))
    assert anna.team_ids == {stable_id("team", "team-payments")}
    assert not anna.can_see_team(stable_id("team", "checkout-project"))
    assert piotr.team_ids == {stable_id("team", "team-settlements")}
    assert {r.role for r in manager.roles} == {"informed"}
    assert {stable_id("team", s) for s in ("team-payments", "team-settlements", "team-pricing")} <= (
        manager.team_ids
    )


async def test_timezones_and_preferences(full_pool):
    zones = {r["email"]: r["timezone"] for r in await full_pool.fetch("SELECT email, timezone FROM users")}
    assert zones["james.hartley@demo"] == "Europe/London"
    assert zones["daniel.reyes@demo"] == "America/New_York"
    assert zones["anna@demo"] == "Europe/Warsaw"
    prefs = {
        r["email"]: r
        for r in await full_pool.fetch(
            "SELECT u.email, p.by_severity, p.working_hours FROM preferences p "
            "JOIN users u ON u.id = p.user_id"
        )
    }
    assert len(prefs) == await full_pool.fetchval("SELECT count(*) FROM users")
    assert prefs["anna@demo"]["by_severity"]["critical"] == ["phone", "sms"]
    assert prefs["tomasz.kaminski@demo"]["by_severity"]["critical"] == ["sms", "phone"]
    assert prefs["manager@demo"]["by_severity"]["warning"] == ["ui"]
    assert prefs["james.hartley@demo"]["working_hours"] == {
        "from": "08:30",
        "to": "18:30",
        "timezone": "Europe/London",
    }


async def test_preferences_work_with_channel_selection(full_pool):
    row = await full_pool.fetchrow(
        "SELECT p.by_severity, p.working_hours FROM preferences p WHERE p.user_id = $1",
        stable_id("user", "james.hartley@demo"),
    )
    prefs = dict(row)
    # 20:00 London is outside James's 08:30-18:30: only non-interrupting channels remain for errors...
    evening = datetime(2026, 3, 3, 20, 0, tzinfo=UTC)
    assert _from_prefs(prefs, "error", evening) == ["email"]
    # ...inside the window he gets SMS first, and critical alerts ignore working hours.
    midday = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
    assert _from_prefs(prefs, "error", midday) == ["sms", "email"]
    assert _from_prefs(prefs, "critical", evening) == ["phone", "sms"]


async def test_fixture_has_no_unknown_scope_references():
    org = load_fixtures()["org"]
    teams = {t["slug"] for t in org["teams"]}
    domains = {d["slug"] for d in org["domains"]}
    emails = {u["email"] for u in org["users"]}
    for user in org["users"]:
        assert not user.get("manager") or user["manager"] in emails
        for role in user["roles"]:
            assert role["scope"] in (teams | domains | {None})


async def test_fixture_rotation_order_puts_anna_and_piotr_on_call_now(full_pool):
    async with full_pool.acquire() as conn:
        for slug, email in (("team-payments", "anna@demo"), ("team-settlements", "piotr@demo")):
            assert await oncall_from_shifts(conn, stable_id("team", slug)) == [stable_id("user", email)]


async def test_reseed_moves_a_rotation_left_in_email_order_once(full_pool):
    team_id = stable_id("team", "team-payments")
    rotation_id = stable_id("rotation", "team-payments")
    legacy = [
        r["id"]
        for r in await full_pool.fetch(
            "SELECT u.id FROM team_members m JOIN users u ON u.id = m.user_id WHERE m.team_id = $1 "
            "ORDER BY u.email",
            team_id,
        )
    ]
    await full_pool.execute("DELETE FROM shifts WHERE rotation_id = $1", rotation_id)
    await full_pool.execute("UPDATE rotations SET member_ids = $2 WHERE id = $1", rotation_id, legacy)
    await full_pool.execute(
        "INSERT INTO shifts (id, team_id, user_id, start_at, end_at, kind, rotation_id) "
        "VALUES ($1, $2, $3, now() - interval '1 day', now() + interval '6 days', 'rotation', $4)",
        stable_id("shift", "legacy"),
        team_id,
        legacy[0],
        rotation_id,
    )
    async with full_pool.acquire() as conn:
        assert await oncall_from_shifts(conn, team_id) == [stable_id("user", "agnieszka.krol@demo")]
    await run_seed(full_pool)
    shifts = await full_pool.fetchval("SELECT count(*) FROM shifts WHERE rotation_id = $1", rotation_id)
    async with full_pool.acquire() as conn:
        assert await oncall_from_shifts(conn, team_id) == [stable_id("user", "anna@demo")]
    await run_seed(full_pool)
    assert (
        await full_pool.fetchval("SELECT count(*) FROM shifts WHERE rotation_id = $1", rotation_id) == shifts
    )


async def test_prometheus_source_has_codebase_and_links_and_reseed_restores_them(full_pool):
    sql = "SELECT codebase, observability FROM sources WHERE name = 'prometheus'"
    row = await full_pool.fetchrow(sql)
    assert row["codebase"]["repo"] == "https://github.com/julpul/gs-alerting"
    assert row["codebase"]["path"] == "demo/payments-api"
    assert [x["label"] for x in row["observability"]["links"]] == ["Logs in Datadog", "Service dashboard"]
    await full_pool.execute("UPDATE sources SET codebase = NULL WHERE name = 'prometheus'")
    await run_seed(full_pool)
    assert (await full_pool.fetchrow(sql))["codebase"]["branch"] == "main"
