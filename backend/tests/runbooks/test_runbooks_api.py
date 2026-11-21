"""Runbook REST endpoints: parse (every format), create, list scoping, alert detail wiring."""

from __future__ import annotations

from .helpers import make_alert
from .test_parsers import MARKDOWN_SAMPLE, TEXT_SAMPLE, YAML_SAMPLE


async def parse(client, auth_headers, content, fmt=None):
    body = {"content": content}
    if fmt:
        body["format"] = fmt
    return await client.post("/v1/runbooks/parse", headers=await auth_headers("anna@demo"), json=body)


async def test_parse_endpoint_handles_every_format_with_auto_detection(client, auth_headers):
    r = await parse(client, auth_headers, YAML_SAMPLE)
    body = r.json()
    assert r.status_code == 200 and body["format"] == "yaml" and body["name"] == "settlement-timeout"
    assert body["attached"]["matches"]["error.code"] == "TIMEOUT"
    assert body["steps"][2]["guard"] == "previous.failures >= 2" and body["source"] == YAML_SAMPLE

    body = (await parse(client, auth_headers, MARKDOWN_SAMPLE, "markdown")).json()
    assert body["format"] == "markdown" and body["steps"][1]["action"] == "failover_region"
    assert body["steps"][1]["suggested"] is True and body["parsedBy"] == "heuristic"

    body = (await parse(client, auth_headers, TEXT_SAMPLE, "text")).json()
    assert body["format"] == "text" and body["parsedBy"] == "heuristic"

    body = (await parse(client, auth_headers, "https://wiki.example.com/runbooks/payments", "link")).json()
    assert body["format"] == "link" and body["steps"][0]["url"].startswith("https://wiki")

    unseen = "- [ ] open the dashboard\n- [ ] roll back the deploy\n"
    body = (await parse(client, auth_headers, unseen, "auto")).json()
    assert body["format"] == "markdown" and body["steps"][1]["action"] == "rollback_deploy"

    body = (await parse(client, auth_headers, '{"steps": ["one", "two"]}')).json()
    assert body["format"] == "yaml" and len(body["steps"]) == 2


async def test_parse_endpoint_validation(client, auth_headers):
    assert (await parse(client, auth_headers, "  ")).status_code == 422
    assert (await parse(client, auth_headers, "x", "docx")).status_code == 422
    assert (await client.post("/v1/runbooks/parse", json={"content": "x"})).status_code == 401
    assert (
        await client.post("/v1/runbooks/parse", headers=await auth_headers("anna@demo"), json={})
    ).status_code == 422


async def test_create_from_pasted_content_and_list_scoping(client, auth_headers, seeded):
    piotr = await auth_headers("piotr@demo")
    r = await client.post(
        "/v1/runbooks",
        headers=piotr,
        json={
            "source": (
                "steps:\n  - check: Is it plugged in?\n  - action: open_ticket\n    summary: Not plugged in\n"
            )
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["format"] == "yaml" and created["steps"][1]["action"] == "open_ticket"
    assert created["teamId"] and created["name"] == "Runbook"
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'runbook.create' AND target_id = $1", created["id"]
        )
        == 1
    )

    mine = {rb["id"] for rb in (await client.get("/v1/runbooks", headers=piotr)).json()}
    assert created["id"] in mine
    anna = {
        rb["id"] for rb in (await client.get("/v1/runbooks", headers=await auth_headers("anna@demo"))).json()
    }
    assert created["id"] not in anna
    master = {
        rb["id"]
        for rb in (await client.get("/v1/runbooks", headers=await auth_headers("master@demo"))).json()
    }
    assert created["id"] in master and len(master) >= 4  # three seeded + created


async def test_create_from_steps_with_attachment_and_validation(client, auth_headers):
    piotr = await auth_headers("piotr@demo")
    body = {
        "name": "Manual steps",
        "format": "markdown",
        "teamId": "team-settlements",
        "attached": {"matches": {"type": "MANUAL_ONLY"}},
        "steps": [
            {
                "kind": "action",
                "action": "retry_workflow",
                "text": "Retry",
                "parameters": {"workflowId": "{{workflowId}}"},
            }
        ],
    }
    r = await client.post("/v1/runbooks", headers=piotr, json=body)
    assert r.status_code == 201
    assert r.json()["steps"][0]["id"] == "s1" and r.json()["steps"][0]["risk"] == "medium"
    assert r.json()["attached"] == {"matches": {"type": "MANUAL_ONLY"}}

    body["steps"][0]["action"] = "launch_missiles"
    assert (await client.post("/v1/runbooks", headers=piotr, json=body)).status_code == 422
    assert (await client.post("/v1/runbooks", headers=piotr, json={"name": "empty"})).status_code == 422
    assert (
        await client.post("/v1/runbooks", headers=piotr, json={**body, "teamId": "no-such-team"})
    ).status_code == 422


async def test_create_requires_a_role_on_the_team(client, auth_headers):
    payload = {"source": "1. Do a thing", "teamId": "team-settlements"}
    assert (
        await client.post("/v1/runbooks", headers=await auth_headers("anna@demo"), json=payload)
    ).status_code == 403
    assert (
        await client.post("/v1/runbooks", headers=await auth_headers("manager@demo"), json=payload)
    ).status_code == 403
    assert (
        await client.post("/v1/runbooks", headers=await auth_headers("manager@demo"), json={"source": "1. x"})
    ).status_code == 422
    root = await client.post(
        "/v1/runbooks", headers=await auth_headers("root.settlements@demo"), json=payload
    )
    assert root.status_code == 201
    master = await client.post(
        "/v1/runbooks", headers=await auth_headers("master@demo"), json={"source": "1. org wide"}
    )
    assert master.status_code == 201 and master.json().get("teamId") is None


async def test_alert_detail_embeds_the_runbook(client, auth_headers, seeded):
    alert = await make_alert(seeded)
    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=await auth_headers("piotr@demo"))).json()
    rb = detail["runbook"]
    assert rb["name"] == "settlement-timeout"
    assert [s["state"] for s in rb["steps"]] == ["not_started"] * 3
    assert rb["steps"][1]["text"] == "Retry workflow WF-20261023-00042"
