"""`raise_alert` publishes through the normal ingest pipeline (same envelope as `POST /v1/alerts/raise`)."""

from __future__ import annotations

from uuid import UUID, uuid4

import orjson

from app.core.ids import stable_id

from .helpers import call, call_error, eventually


def rule_for(source_name: str, team: str = "team-payments") -> dict:
    return {
        "name": f"agent alerts {source_name}",
        "teamId": str(stable_id("team", team)),
        "priority": "critical",
        "match": {
            "op": "and",
            "conditions": [
                {"field": "_source", "operator": "eq", "value": source_name},
                {"field": "_type", "operator": "glob", "value": "security.*"},
            ],
        },
        "routing": {"hard": [], "soft": []},
        "presentation": {"titleTemplate": "{{_title}}", "bodyTemplate": "{{description}}"},
        "dedup": {"fields": ["type", "dedupKey"]},
    }


async def insert_rule(pool, definition: dict) -> UUID:
    rule_id = uuid4()
    await pool.execute(
        "INSERT INTO rules (id, team_id, name, definition) VALUES ($1, $2, $3, $4)",
        rule_id,
        UUID(definition["teamId"]),
        definition["name"],
        definition,
    )
    return rule_id


async def test_raise_alert_creates_an_alert_through_the_real_worker(make_pat, connect, seeded, start_worker):
    pat = await make_pat("anna@demo", f"pr-review-{uuid4().hex[:6]}")
    source_name = f"agent:{pat.name}"
    rule_id = await insert_rule(seeded, rule_for(source_name))
    try:
        await start_worker()
        async with connect(pat.token) as agent:
            first = await call(
                agent,
                "raise_alert",
                {
                    "type": "security.secret_leaked",
                    "severity": "critical",
                    "title": "AWS key in PR 1873",
                    "description": "pr-review-agent found a string matching an AWS access key id",
                    "attributes": {"repo": "vistula/payments-api", "pullRequest": 1873},
                    "dedup_key": "secret:payments-api:1873",
                },
            )
            assert first["source"] == source_name and first["status"] == "accepted"
            source_id = UUID(first["sourceId"])

            async def alert():
                return await seeded.fetchrow("SELECT * FROM alerts WHERE source_id = $1", source_id)

            created = await eventually(alert)
            assert created["title"] == "AWS key in PR 1873" and created["severity"] == "critical"
            assert created["team_id"] == stable_id("team", "team-payments") and created["rule_id"] == rule_id

            second = await call(
                agent,
                "raise_alert",
                {
                    "type": "security.secret_leaked",
                    "severity": "critical",
                    "title": "AWS key in PR 1873",
                    "dedup_key": "secret:payments-api:1873",
                },
            )
            assert second["sourceId"] == first["sourceId"]  # the agent source is created once

            async def deduplicated():
                row = await alert()
                return row if row["count"] == 2 else None

            assert (await eventually(deduplicated))["id"] == created["id"]
    finally:
        await seeded.execute("DELETE FROM rules WHERE id = $1", rule_id)

    source = await seeded.fetchrow("SELECT * FROM sources WHERE id = $1", source_id)
    assert source["name"] == source_name and source["team_id"] == stable_id("team", "team-payments")
    audit = await seeded.fetch(
        "SELECT actor FROM audit_log WHERE action = 'alert.raise' AND target_id = $1", str(source_id)
    )
    assert len(audit) == 2 and audit[0]["actor"]["type"] == "agent"
    assert audit[0]["actor"]["name"] == pat.name


async def test_raise_alert_with_an_existing_source_of_the_team(make_pat, connect, seeded, rabbit):
    # a private queue bound to the same exchange sees every event, whoever else consumes `events.in`
    tap = await rabbit._channel().declare_queue(exclusive=True, auto_delete=True)
    await tap.bind(await rabbit._exchange("events"), routing_key="events.in")
    pat = await make_pat("anna@demo", f"explicit-{uuid4().hex[:6]}")
    source_id = uuid4()
    await seeded.execute(
        "INSERT INTO sources (id, name, team_id, domain_id, type, key_prefix, key_hash) "
        "VALUES ($1, $2, $3, $4, 'webhook', 'sk_x', $5)",
        source_id,
        f"ci-bot-{source_id.hex[:6]}",
        stable_id("team", "team-payments"),
        stable_id("domain", "payments"),
        f"hash-{source_id}",
    )
    async with connect(pat.token) as agent:
        out = await call(
            agent,
            "raise_alert",
            {"type": "ops.note", "severity": "info", "title": "hello", "source": str(source_id)},
        )
        assert out["sourceId"] == str(source_id)
        envelopes = []
        while (message := await tap.get(fail=False, no_ack=True)) is not None:
            envelopes.append(message.body)
        mine = [e for e in map(orjson.loads, envelopes) if e["source_id"] == str(source_id)]
        assert len(mine) == 1 and mine[0]["typed"] is True
        assert mine[0]["_meta"]["type"] == "ops.note" and mine[0]["raw"]["title"] == "hello"


async def test_raise_alert_validation_and_authorization(make_pat, informed_pat, connect):
    anna = await make_pat("anna@demo", f"validate-{uuid4().hex[:6]}")
    async with connect(anna.token) as agent:
        bad = await call_error(agent, "raise_alert", {"type": "x.y", "severity": "apocalyptic", "title": "t"})
        assert "validation_error" in bad and "severity" in bad
        assert "not_found" in await call_error(
            agent,
            "raise_alert",
            {"type": "x.y", "severity": "info", "title": "t", "source": "no-such-source"},
        )
        # a source of another team is invisible to anna
        assert "not_found" in await call_error(
            agent,
            "raise_alert",
            {"type": "x.y", "severity": "info", "title": "t", "source": "pr-review-agent"},
        )
    informed = await informed_pat("team-payments")
    async with connect(informed.token) as agent:
        assert "forbidden" in await call_error(
            agent, "raise_alert", {"type": "x.y", "severity": "info", "title": "t"}
        )
