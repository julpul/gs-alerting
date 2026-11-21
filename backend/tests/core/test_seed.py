from app.core.ids import stable_id
from seed.seed import discover_loaders, load_fixtures, run_seed

TABLES = ("users", "domains", "teams", "team_domains", "team_members", "role_assignments")


async def _counts(pool) -> dict[str, int]:
    return {t: await pool.fetchval(f"SELECT count(*) FROM {t}") for t in TABLES}


async def test_seed_is_idempotent(seeded, client):
    before = await _counts(seeded)
    assert before["users"] >= 7 and before["teams"] >= 6 and before["domains"] >= 4
    hash_before = await seeded.fetchval("SELECT password_hash FROM users WHERE email = 'anna@demo'")
    await run_seed(seeded)
    await run_seed(seeded)
    assert await _counts(seeded) == before
    assert await seeded.fetchval("SELECT password_hash FROM users WHERE email = 'anna@demo'") == hash_before
    resp = await client.post("/v1/auth/login", json={"email": "anna@demo", "password": "demo"})
    assert resp.status_code == 200


async def test_seed_uses_deterministic_ids_and_spec_users(seeded):
    rows = {r["email"]: r for r in await seeded.fetch("SELECT id, email, phone FROM users")}
    for email in (
        "master@demo",
        "root.payments@demo",
        "root.settlements@demo",
        "root.security@demo",
        "anna@demo",
        "piotr@demo",
        "manager@demo",
    ):
        assert rows[email]["id"] == stable_id("user", email)
    assert rows["anna@demo"]["phone"] == "+48500100200"


async def test_team_domain_links(seeded):
    rows = await seeded.fetch(
        "SELECT t.slug AS team, d.slug AS domain FROM team_domains td "
        "JOIN teams t ON t.id = td.team_id JOIN domains d ON d.id = td.domain_id"
    )
    links = {(r["team"], r["domain"]) for r in rows}
    assert {
        ("team-settlements", "settlements"),
        ("team-pricing", "markets"),
        ("team-rates", "markets"),
        ("team-security", "security"),
        ("checkout-project", "payments"),
        ("checkout-project", "security"),
        ("team-payments", "payments"),
    } <= links


def test_loaders_are_discovered_in_order():
    names = [n for n, _ in discover_loaders()]
    assert names == sorted(names)
    assert "10_org" in names
    assert "org" in load_fixtures()
