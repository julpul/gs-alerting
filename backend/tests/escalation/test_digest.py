"""Digests: suppressed deliveries are grouped per recipient into one email and marked `digested`."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.ids import uuid7
from app.delivery.channels import registry
from app.delivery.channels.base import ChannelError
from app.escalation import jobs
from tests.delivery.helpers import MAILPIT, mailpit_search
from tests.escalation.helpers import make_alert


@pytest.fixture
async def recipient(seeded):
    """A user nobody else notifies, so the Mailpit search below sees only this test's mail."""
    token = uuid.uuid4().hex[:10]
    user_id = uuid7()
    await seeded.execute(
        "INSERT INTO users (id, email, display_name, password_hash) VALUES ($1, $2, 'Dana Digest', 'x')",
        user_id,
        f"digest-{token}@test.local",
    )
    yield {"id": user_id, "email": f"digest-{token}@test.local", "token": token}
    await seeded.execute("DELETE FROM deliveries WHERE recipient_user_id = $1", user_id)
    await seeded.execute("DELETE FROM users WHERE id = $1", user_id)


async def suppressed(pool, alert, user_id, channel="sms", status="suppressed_digest"):
    delivery_id = uuid7()
    await pool.execute(
        "INSERT INTO deliveries (id, alert_id, recipient_user_id, channel, kind, idempotency_key, status) "
        "VALUES ($1, $2, $3, $4, 'soft', $5, $6)",
        delivery_id,
        alert["id"],
        user_id,
        channel,
        f"digest-test-{delivery_id}",
        status,
    )
    return delivery_id


async def test_digest_groups_suppressed_deliveries_into_one_email(seeded, recipient):
    first = await make_alert(seeded, None, plan=False)
    second = await make_alert(seeded, None, plan=False)
    ids = [
        await suppressed(seeded, first, recipient["id"], "sms"),
        await suppressed(seeded, first, recipient["id"], "email"),
        await suppressed(seeded, second, recipient["id"], "sms"),
    ]
    untouched = await suppressed(seeded, first, recipient["id"], "sms", status="pending")

    assert await jobs.send_digests(seeded) >= 1

    (message,) = await mailpit_search(recipient["email"])
    assert message["Subject"] == "Alert digest: 3 notification(s) on 2 alert(s)"
    async with httpx.AsyncClient(timeout=5) as client:
        full = (await client.get(f"{MAILPIT}/message/{message['ID']}")).json()
    for alert in (first, second):
        assert alert["title"] in full["Text"]
    assert "(2 held back)" in full["Text"] and "(1 held back)" in full["Text"]
    rows = await seeded.fetch("SELECT id, status FROM deliveries WHERE id = ANY($1)", [*ids, untouched])
    status = {r["id"]: r["status"] for r in rows}
    assert {status[i] for i in ids} == {"digested"}
    assert status[untouched] == "pending"

    assert await jobs.send_digests(seeded) == 0  # nothing left, no second email
    assert len(await mailpit_search(recipient["email"])) == 1


async def test_failed_digest_email_keeps_deliveries_queued(seeded, recipient):
    alert = await make_alert(seeded, None, plan=False)
    delivery = await suppressed(seeded, alert, recipient["id"])

    class Broken:
        name = "email"

        async def send(self, msg, key):
            raise ChannelError("smtp down")

    restore = registry.override("email", Broken())
    try:
        assert await jobs.send_digests(seeded) == 0
    finally:
        restore()
    assert (
        await seeded.fetchval("SELECT status FROM deliveries WHERE id = $1", delivery) == "suppressed_digest"
    )
    assert await jobs.send_digests(seeded) >= 1  # the next round delivers it
    assert await seeded.fetchval("SELECT status FROM deliveries WHERE id = $1", delivery) == "digested"
