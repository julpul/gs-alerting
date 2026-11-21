"""Trust ladder: defaults, promotion after 5 approved successes, demotion on failure, caps, endpoints."""

from __future__ import annotations

from app.core.ids import stable_id
from app.runbooks import trust

from .helpers import make_alert

SETTLEMENTS = stable_id("domain", "settlements")


async def test_defaults_by_risk(seeded):
    assert (await trust.get_trust(seeded, SETTLEMENTS, "retry_workflow")).level == "auto"
    assert (await trust.get_trust(seeded, SETTLEMENTS, "http_get")).level == "auto"
    high = await trust.get_trust(seeded, SETTLEMENTS, "restart_service")
    assert high.level == "run_after_approval" and not high.stored


async def test_promotion_after_five_consecutive_approved_successes(seeded):
    await trust.set_trust(
        seeded, SETTLEMENTS, "retry_workflow", level="run_after_approval", cap=None, cap_given=False
    )
    for i in range(4):
        t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=True, approved=True)
        assert (t.level, t.successes) == ("run_after_approval", i + 1)
    t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=True, approved=True)
    assert (t.level, t.successes) == ("auto", 0) and t.changed_at is not None


async def test_unapproved_runs_do_not_count_towards_promotion(seeded):
    await trust.set_trust(seeded, SETTLEMENTS, "retry_workflow", level="suggest", cap=None, cap_given=False)
    for _ in range(6):
        t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=True, approved=False)
    assert (t.level, t.successes) == ("suggest", 0)


async def test_failure_demotes_one_level_and_resets_the_streak(seeded):
    await trust.set_trust(seeded, SETTLEMENTS, "retry_workflow", level="auto", cap=None, cap_given=False)
    await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=True, approved=True)
    t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=False, approved=True)
    assert (t.level, t.successes, t.failures) == ("run_after_approval", 0, 1)
    t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=False, approved=False)
    assert (t.level, t.failures) == ("suggest", 2)
    t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=False, approved=False)
    assert t.level == "suggest"  # floor


async def test_never_above_cap_and_high_risk_capped_at_run_after_approval(seeded):
    await trust.set_trust(
        seeded,
        SETTLEMENTS,
        "retry_workflow",
        level="run_after_approval",
        cap="run_after_approval",
        cap_given=True,
    )
    for _ in range(7):
        t = await trust.record_outcome(seeded, SETTLEMENTS, "retry_workflow", success=True, approved=True)
    assert t.level == "run_after_approval" and t.cap == "run_after_approval"

    for _ in range(6):
        t = await trust.record_outcome(seeded, SETTLEMENTS, "restart_service", success=True, approved=True)
    assert t.level == "run_after_approval"
    t = await trust.set_trust(seeded, SETTLEMENTS, "restart_service", level="auto", cap=None, cap_given=False)
    assert t.level == "run_after_approval"


async def test_read_only_actions_are_not_counted(seeded):
    t = await trust.record_outcome(seeded, SETTLEMENTS, "http_get", success=False, approved=False)
    assert (t.level, t.failures, t.stored) == ("auto", 0, False)
    t = await trust.record_outcome(seeded, SETTLEMENTS, "no_such_action", success=False, approved=False)
    assert t.failures == 0


async def test_lowering_the_cap_clamps_the_level(seeded):
    await trust.set_trust(seeded, SETTLEMENTS, "retry_workflow", level="auto", cap=None, cap_given=False)
    t = await trust.set_trust(
        seeded, SETTLEMENTS, "retry_workflow", level=None, cap="suggest", cap_given=True
    )
    assert (t.level, t.cap) == ("suggest", "suggest")
    t = await trust.set_trust(seeded, SETTLEMENTS, "retry_workflow", level=None, cap=None, cap_given=True)
    assert t.cap is None


async def test_five_approved_runs_through_the_api_promote_retry_workflow(client, auth_headers, seeded, gw):
    root = await auth_headers("root.settlements@demo")
    put = await client.put(
        "/v1/action-trust/settlements/retry_workflow", headers=root, json={"level": "run_after_approval"}
    )
    assert put.status_code == 200 and put.json()["level"] == "run_after_approval"
    alert = await make_alert(seeded)
    for i in range(5):
        url = f"/v1/alerts/{alert['id']}/runbook/steps/s2/run"
        assert (await client.post(url, headers=root)).status_code == 409, i
        step = (await client.post(url + "?confirmed=true", headers=root)).json()
        assert step["state"] == "succeeded", i
    listed = (await client.get("/v1/action-trust?domainId=settlements", headers=root)).json()
    retry = next(t for t in listed if t["action"] == "retry_workflow")
    assert retry["level"] == "auto" and retry["promoteAfter"] == 5 and retry["successfulApprovedRuns"] == 0
    assert retry["lastChangedAt"] is not None
    step = (await client.post(f"/v1/alerts/{alert['id']}/runbook/steps/s2/run", headers=root)).json()
    assert step["state"] == "succeeded"  # now runs without approval


async def test_trust_endpoints_permissions_and_validation(client, auth_headers):
    root = await auth_headers("root.settlements@demo")
    everything = (await client.get("/v1/action-trust", headers=root)).json()
    assert {t["domainId"] for t in everything} == {str(SETTLEMENTS)}  # only the caller's domain
    assert {"retry_workflow", "escalate", "http_get"} <= {t["action"] for t in everything}
    master = (await client.get("/v1/action-trust", headers=await auth_headers("master@demo"))).json()
    assert len({t["domainId"] for t in master}) == 4

    body = {"level": "auto"}
    piotr = await auth_headers("piotr@demo")
    assert (
        await client.put("/v1/action-trust/settlements/retry_workflow", headers=piotr, json=body)
    ).status_code == 403
    assert (
        await client.put("/v1/action-trust/payments/retry_workflow", headers=root, json=body)
    ).status_code == 404
    assert (
        await client.put("/v1/action-trust/settlements/not_an_action", headers=root, json=body)
    ).status_code == 404
    assert (
        await client.put("/v1/action-trust/settlements/retry_workflow", headers=root, json={"level": "yolo"})
    ).status_code == 422
    anna = await auth_headers("anna@demo")
    assert (await client.get("/v1/action-trust?domainId=settlements", headers=anna)).status_code == 404

    capped = await client.put(
        "/v1/action-trust/settlements/retry_workflow",
        headers=root,
        json={"level": "auto", "cap": "run_after_approval"},
    )
    assert capped.json()["level"] == "run_after_approval" and capped.json()["cap"] == "run_after_approval"
    assert await client.put("/v1/action-trust/settlements/retry_workflow", headers=root, json={"cap": None})
    audited = await client.get("/v1/action-trust?domainId=settlements", headers=root)
    assert next(t for t in audited.json() if t["action"] == "retry_workflow")["cap"] is None
