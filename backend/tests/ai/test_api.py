"""REST: knowledge upload/list (scoped), integrations (secrets write-only), suggestions + resolution note."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from app.ai.llm import domain_override
from app.core.errors import AppError
from app.core.security import decrypt_secret
from tests.api.alerts_helpers import domain, make_alert, team

DOC = "# Card gateway runbook\n\n" + "Failover the card gateway to the secondary acquirer. " * 40


async def upload_json(client, headers, **body):
    return await client.post("/v1/knowledge", json=body, headers=headers)


async def test_knowledge_upload_indexes_chunks_and_listing_is_scoped(client, auth_headers, seeded):
    anna = await auth_headers("anna@demo")
    marker = uuid4().hex[:8]
    resp = await upload_json(client, anna, content=f"{DOC}\nmarker {marker}", teamId="team-payments")
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["title"] == "Card gateway runbook" and doc["kind"] == "upload"
    assert doc["teamId"] == str(team("team-payments")) and doc["indexedAt"]
    chunks = await seeded.fetchval(
        "SELECT count(*) FROM knowledge_chunks WHERE doc_id = $1 AND embedding IS NOT NULL", UUID(doc["id"])
    )
    assert chunks >= 2

    mine = (await client.get("/v1/knowledge", headers=anna)).json()
    assert doc["id"] in {d["id"] for d in mine}
    piotr = await auth_headers("piotr@demo")
    theirs = (await client.get("/v1/knowledge", headers=piotr)).json()
    assert doc["id"] not in {d["id"] for d in theirs}  # Settlements cannot see Payments knowledge
    master = (await client.get("/v1/knowledge", headers=await auth_headers("master@demo"))).json()
    assert doc["id"] in {d["id"] for d in master}


async def test_knowledge_multipart_file_and_url(client, auth_headers, monkeypatch):
    anna = await auth_headers("anna@demo")
    resp = await client.post(
        "/v1/knowledge",
        data={"teamId": "team-payments", "title": "Uploaded notes"},
        files={"file": ("notes.md", b"# Notes\nRestart the consumer when lag grows.", "text/markdown")},
        headers=anna,
    )
    assert resp.status_code == 201 and resp.json()["title"] == "Uploaded notes"

    async def fake_fetch(url: str):
        return "Wiki page", "Escalate to the acquirer hotline when failover does not help."

    monkeypatch.setattr("app.api.routers.knowledge.fetch_url", fake_fetch)
    resp = await upload_json(client, anna, url="https://wiki.example/acquirer", teamId="team-payments")
    assert resp.status_code == 201
    assert resp.json()["kind"] == "link" and resp.json()["title"] == "Wiki page"

    async def broken_fetch(url: str):
        raise AppError(422, "fetch_failed", "nope")

    monkeypatch.setattr("app.api.routers.knowledge.fetch_url", broken_fetch)
    assert (
        await upload_json(client, anna, url="https://x.example", teamId="team-payments")
    ).status_code == 422


async def test_knowledge_title_from_front_matter_heading_or_filename(client, auth_headers):
    anna = await auth_headers("anna@demo")

    async def upload(name: str, body: bytes, title: str | None = None) -> str:
        data = {"teamId": "team-payments", **({"title": title} if title else {})}
        resp = await client.post(
            "/v1/knowledge", data=data, files={"file": (name, body, "text/markdown")}, headers=anna
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["title"]

    front = b'---\ntitle: "FX cache storms"\nowner: pricing\n---\n# Other heading\nRestart the cache.'
    assert await upload("x.md", front) == "FX cache storms"
    assert await upload("x.md", b"Intro line\n## Sub\n# Main heading\ntext") == "Main heading"
    sub_only = b"---\nowner: pricing\n---\n## Only a sub heading\ntext"
    assert await upload("x.md", sub_only) == "Only a sub heading"
    assert await upload("disk-full_runbook.md", b"no heading at all") == "disk full runbook"
    assert await upload("x.md", front, title="Given title") == "Given title"
    plain = await upload_json(client, anna, content="just text, no heading", teamId="team-payments")
    assert plain.json()["title"] == "Untitled document"


def test_title_from_unit():
    from app.api.routers.knowledge import title_from

    assert title_from("---\ntitle: 'Quoted'\n---\nbody") == "Quoted"
    assert title_from("---\ntitle:\n---\n# H1 after empty title") == "H1 after empty title"
    assert title_from("# Title #\n") == "Title"
    assert title_from("plain") is None


async def test_knowledge_validation_and_permissions(client, auth_headers):
    anna = await auth_headers("anna@demo")
    assert (await upload_json(client, anna, teamId="team-payments")).status_code == 422  # nothing to index
    assert (await upload_json(client, anna, content="x", teamId="no-such-team")).status_code == 422
    assert (await upload_json(client, anna, content="x")).status_code == 422  # no target
    assert (await upload_json(client, anna, content="x", domainId="payments")).status_code == 403  # not root
    assert (
        await upload_json(client, anna, content="x", teamId="team-payments", domainId="markets")
    ).status_code == 422
    assert (await upload_json(client, anna, content="x", teamId="team-settlements")).status_code == 403
    bin_file = await client.post(
        "/v1/knowledge",
        data={"teamId": "team-payments"},
        files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        headers=anna,
    )
    assert bin_file.status_code == 422 and bin_file.json()["code"] == "unsupported_type"
    huge = await client.post(
        "/v1/knowledge",
        data={"teamId": "team-payments"},
        files={"file": ("big.txt", b"x" * 1_100_000, "text/plain")},
        headers=anna,
    )
    assert huge.status_code == 422 and huge.json()["code"] == "too_large"
    informed = await auth_headers("manager@demo")
    assert (await upload_json(client, informed, content="x", teamId="team-payments")).status_code == 403
    assert (
        await client.post(
            "/v1/knowledge",
            headers=anna,
            content="raw",
        )
    ).status_code == 422
    assert (await client.post("/v1/knowledge", json=[1], headers=anna)).status_code == 422
    assert (await client.get("/v1/knowledge")).status_code == 401


async def test_domain_root_can_add_domain_wide_knowledge(client, auth_headers):
    root = await auth_headers("root.payments@demo")
    resp = await upload_json(
        client, root, content="Domain wide guidance for chargebacks.", title="Chargebacks"
    )
    assert resp.status_code == 201 and resp.json()["teamId"] is None
    anna = await auth_headers("anna@demo")
    assert resp.json()["id"] in {d["id"] for d in (await client.get("/v1/knowledge", headers=anna)).json()}


async def test_integrations_store_secrets_encrypted_and_never_return_them(client, auth_headers, seeded):
    root = await auth_headers("root.settlements@demo")
    cfg = {"mode": "own_key", "model": "anthropic/claude-haiku-5-5", "apiKey": "sk-super-secret-123"}
    resp = await client.put("/v1/integrations/llm", json={"config": cfg}, headers=root)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "kind": "llm",
        "status": "connected",
        "scopeDomainId": str(domain("settlements")),
        "mode": "own_key",
    }
    listing = await client.get("/v1/integrations", headers=root)
    assert "sk-super-secret-123" not in listing.text and "apiKey" not in listing.text
    llm_rows = [i for i in listing.json() if i["kind"] == "llm"]
    assert llm_rows[0]["mode"] == "own_key" and llm_rows[0]["scopeDomainId"] == str(domain("settlements"))
    assert {i["kind"] for i in listing.json()} == {"datadog", "github", "jira", "confluence", "llm"}

    blob = await seeded.fetchval(
        "SELECT config_encrypted FROM integrations WHERE domain_id = $1 AND kind = 'llm'",
        domain("settlements"),
    )
    assert b"sk-super-secret-123" not in bytes(blob)
    assert json.loads(decrypt_secret(bytes(blob)))["apiKey"] == "sk-super-secret-123"

    # updating without the key keeps the stored one
    resp = await client.put(
        "/v1/integrations/llm",
        json={"config": {"mode": "own_key", "model": "openai/gpt-5-mini"}},
        headers=root,
    )
    assert resp.status_code == 200
    async with seeded.acquire() as conn:
        override = await domain_override(conn, domain("settlements"))
    assert override.model == "openai/gpt-5-mini" and override.api_key == "sk-super-secret-123"

    other = await auth_headers("root.payments@demo")
    assert not [
        i
        for i in (await client.get("/v1/integrations", headers=other)).json()
        if i["scopeDomainId"] == str(domain("settlements"))
    ]

    reset = await client.put("/v1/integrations/llm", json={"config": {"mode": "default"}}, headers=root)
    assert reset.json()["mode"] == "default"
    assert (
        await seeded.fetchval(
            "SELECT count(*) FROM integrations WHERE kind = 'llm' AND domain_id = $1", domain("settlements")
        )
        == 0
    )
    audits = await seeded.fetch(
        "SELECT data FROM audit_log WHERE action = 'integration.update' ORDER BY id DESC LIMIT 3"
    )
    assert audits and "sk-super-secret-123" not in json.dumps([dict(a)["data"] for a in audits])


async def test_integration_validation_and_permissions(client, auth_headers):
    root = await auth_headers("root.payments@demo")
    put = lambda kind, body, h=root: client.put(f"/v1/integrations/{kind}", json=body, headers=h)  # noqa: E731
    assert (await put("llm", {"config": {"mode": "nope"}})).status_code == 422
    assert (await put("llm", {"config": {"mode": "own_key", "model": "m"}})).status_code == 422  # no key
    assert (await put("llm", {"config": {"mode": "own_key", "apiKey": "k"}})).status_code == 422  # no model
    bad_url = {"config": {"mode": "own_endpoint", "model": "m", "baseUrl": "ftp://x"}}
    assert (await put("llm", bad_url)).status_code == 422
    good = {"config": {"mode": "own_endpoint", "model": "llama", "baseUrl": "http://llm.internal/v1"}}
    assert (await put("llm", good)).json()["mode"] == "own_endpoint"
    assert (await put("jira", {"config": {}})).status_code == 422
    github = await put("github", {"config": {"token": "ghp_x"}})
    assert github.status_code == 200 and github.json()["mode"] is None and "ghp_x" not in github.text
    assert (await put("slack", {"config": {"a": 1}})).status_code == 422
    assert (await put("llm", {"scopeDomainId": "nowhere", "config": {"mode": "default"}})).status_code == 422
    assert (
        await put("llm", {"scopeDomainId": "settlements", "config": {"mode": "default"}})
    ).status_code == 403
    anna = await auth_headers("anna@demo")
    assert (
        await put("llm", {"config": {"mode": "default"}}, anna)
    ).status_code == 422  # no domain to default to
    master = await auth_headers("master@demo")
    assert (await put("llm", {"config": {"mode": "default"}}, master)).status_code == 422
    assert (
        await put("llm", {"scopeDomainId": "payments", "config": {"mode": "default"}}, master)
    ).status_code == 200


async def test_suggestion_endpoints_scope_decision_and_audit(client, auth_headers, seeded, llm, runbook_for):
    runbook_for(
        [{"id": "step-retry", "kind": "action", "text": "Retry the workflow", "action": "retry_workflow"}]
    )
    llm.responses = {"fast": {"diagnosis": "Stuck.", "nextStepId": "step-retry", "confidence": 0.7}}
    alert = await make_alert(seeded, knowledge_key=None)
    anna = await auth_headers("anna@demo")
    url = f"/v1/alerts/A-{alert['short_id']}/suggestion"
    assert (await client.get(url, headers=anna)).status_code == 404  # not ready yet

    from app.ai.suggest import generate_suggestion

    async with seeded.acquire() as conn:
        created = await generate_suggestion(conn, alert["id"])
    resp = await client.get(url, headers=anna)
    assert resp.status_code == 200
    body = resp.json()
    assert (
        body["id"] == str(created["id"]) and body["status"] == "draft" and body["nextStepId"] == "step-retry"
    )
    assert body["diagnosis"] == "Stuck." and body["alertId"] == str(alert["id"])

    piotr = await auth_headers("piotr@demo")
    assert (await client.get(url, headers=piotr)).status_code == 404  # Settlements cannot see it
    decision = f"{url}/{created['id']}/decision"
    assert (await client.post(decision, json={"decision": "approve"}, headers=piotr)).status_code == 404
    informed = await auth_headers("manager@demo")
    assert (await client.post(decision, json={"decision": "approve"}, headers=informed)).status_code == 403
    assert (await client.post(decision, json={"decision": "maybe"}, headers=anna)).status_code == 422
    assert (
        await client.post(f"{url}/not-a-uuid/decision", json={"decision": "approve"}, headers=anna)
    ).status_code == 404
    assert (
        await client.post(f"{url}/{uuid4()}/decision", json={"decision": "approve"}, headers=anna)
    ).status_code == 404

    accepted = await client.post(decision, json={"decision": "approve", "comment": "yes"}, headers=anna)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    again = await client.post(decision, json={"decision": "approve"}, headers=anna)
    assert again.status_code == 200 and again.json()["status"] == "accepted"
    assert (await client.post(decision, json={"decision": "reject"}, headers=anna)).status_code == 409
    assert (await client.get(url, headers=anna)).json()["status"] == "accepted"
    audit = await seeded.fetch(
        "SELECT action, data FROM audit_log WHERE target_type = 'suggestion' AND target_id = $1",
        str(created["id"]),
    )
    assert [a["action"] for a in audit] == ["suggestion.accept"]
    assert audit[0]["data"]["nextStepId"] == "step-retry" and audit[0]["data"]["comment"] == "yes"

    other = await make_alert(seeded, knowledge_key=None)
    async with seeded.acquire() as conn:
        second = await generate_suggestion(conn, other["id"])
    rejected = await client.post(
        f"/v1/alerts/{other['id']}/suggestion/{second['id']}/decision",
        json={"decision": "reject"},
        headers=anna,
    )
    assert rejected.json()["status"] == "rejected"


async def test_alert_detail_still_shows_the_suggestion_block(client, auth_headers, seeded, llm):
    llm.responses = {"fast": {"diagnosis": "Detail check.", "confidence": 0.3}}
    alert = await make_alert(seeded, knowledge_key=None)
    from app.ai.suggest import generate_suggestion

    async with seeded.acquire() as conn:
        await generate_suggestion(conn, alert["id"])
    anna = await auth_headers("anna@demo")
    detail = (await client.get(f"/v1/alerts/{alert['id']}", headers=anna)).json()
    assert detail["suggestion"]["diagnosis"] == "Detail check."


async def test_resolution_note_draft_from_the_timeline(client, auth_headers, seeded, llm):
    alert = await make_alert(seeded)
    await seeded.execute(
        "INSERT INTO timeline (id, alert_id, type, actor, text) "
        "VALUES ($1, $2, 'comment', $3, $4), ($5, $2, 'step_run', $3, $6)",
        uuid4(),
        alert["id"],
        {"type": "user", "id": "x", "name": "Anna"},
        "Gateway returned 503",
        uuid4(),
        "Ran retry_workflow: succeeded",
    )
    anna = await auth_headers("anna@demo")
    url = f"/v1/alerts/{alert['id']}/resolution-note-draft"
    llm.responses = {"smart": {"note": "The gateway returned 503; retrying the workflow fixed it."}}
    resp = await client.get(url, headers=anna)
    assert resp.status_code == 200
    assert resp.json() == {
        "note": "The gateway returned 503; retrying the workflow fixed it.",
        "aiUnavailable": False,
        "model": "fake-model",
        "source": "ai",
    }
    assert llm.calls[0]["tier"] == "smart" and "Gateway returned 503" in llm.calls[0]["user"]
    llm.fail = True
    resp = await client.get(url, headers=anna)
    body = resp.json()
    assert body["aiUnavailable"] is True and body["source"] == "heuristic" and body["model"] is None
    assert "Comments: Gateway returned 503 (Anna)." in body["note"]
    assert "Actions taken: Ran retry_workflow: succeeded (Anna)." in body["note"]
    piotr = await auth_headers("piotr@demo")
    assert (await client.get(url, headers=piotr)).status_code == 404
    quiet = await make_alert(seeded, title="Quiet one")
    note = (await client.get(f"/v1/alerts/{quiet['id']}/resolution-note-draft", headers=anna)).json()["note"]
    assert note.startswith("Quiet one (error), opened ") and "No actions were recorded" in note
