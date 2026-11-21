"""Herald UI demo tools: `/api/sim/*`. They go through the real pieces, never around them.

* `samples` - the Appendix A events (`seed/fixtures/samples/*.json`) plus the names Herald's UI uses for them
* `send`    - publish into a platform through the same envelope + RabbitMQ path as `/v1/ingest`
* `mail`    - the last emails Mailpit captured (`MAILPIT_API_URL`, default `http://<SMTP_HOST>:8025`)
* `phone`   - what mockgw (`MOCKGW_URL`) sent as SMS / calls, and the replies, per person
* `gateway` - take the SMS + phone gateway down / bring it back (mockgw chaos toggle)
* `reply`   - a person answers by SMS: our inbound SMS handler, called in-process (no shared secret needed)
"""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import orjson
from pydantic import BaseModel

from app.api.deps import Pool, RabbitDep
from app.api.routers.herald_ingest import publish_events
from app.config import get_settings
from app.core.errors import AppError, not_found
from app.escalation.inbound import handle_sms
from app.herald.explain import raw_events
from app.herald.identity import Actor, user_by_person
from app.herald.platforms import TYPE_MAP, platform_source
from app.herald.routing import herald_router

router = herald_router()
SAMPLES_DIR = Path(__file__).resolve().parents[3] / "seed" / "fixtures" / "samples"
MAX_SEND = 2000
GATEWAY_CHANNELS = ("sms", "phone")
DOWN_SECONDS = 24 * 3600
UNSEEN_MARGIN_CALL = {
    "specversion": "1.0",
    "id": "evt-7f3a91",
    "source": "/risk/margin-engine",
    "type": "com.example.risk.margin_call.raised",
    "time": "2026-10-23T09:20:11Z",
    "data": {
        "account": {"id": "ACC-44120", "desk": "equities", "tier": 2},
        "shortfall": {"amount": 1850000.5, "currency": "USD"},
        "priority": "P1",
    },
}


def http_client() -> httpx.AsyncClient:
    """Replaced in tests with a mock transport."""
    return httpx.AsyncClient(timeout=3.0)


def load_samples() -> dict[str, Any]:
    ours = {p.stem: orjson.loads(p.read_bytes()) for p in sorted(SAMPLES_DIR.glob("*.json"))}
    herald: dict[str, Any] = {}
    if "prometheus_high_error_rate" in ours:
        herald["metric_breach"] = ours["prometheus_high_error_rate"]
        herald["metric_resolved"] = {
            **copy.deepcopy(ours["prometheus_high_error_rate"]),
            "status": "resolved",
        }
    if "workflow_failed" in ours:
        ok = {k: v for k, v in copy.deepcopy(ours["workflow_failed"]).items() if k != "error"}
        herald["workflow_succeeded"] = {**ok, "type": "WORKFLOW_SUCCEEDED", "severity": "INFO"}
    herald["unseen_margin_call"] = UNSEEN_MARGIN_CALL
    return {**ours, **herald}


@router.get("/api/sim/samples")
async def sim_samples(who: Actor) -> dict[str, Any]:
    return load_samples()


class SimSend(BaseModel):
    source: str
    event: Any
    count: int = 1


@router.post("/api/sim/send", status_code=202)
async def sim_send(body: SimSend, who: Actor, pool: Pool, rabbit: RabbitDep) -> dict[str, Any]:
    """Put an event in the way its platform would: same envelope, same queue, same worker."""
    async with pool.acquire() as conn:
        row = await platform_source(conn, who.scope, body.source)
    if row is None:
        raise not_found(f"platform '{body.source}'")
    source = dict(row)
    raws = raw_events(body.event, None) * max(1, min(body.count, MAX_SEND))
    envelopes = await publish_events(rabbit, source, raws)
    return {
        "sent": len(envelopes),
        "through": TYPE_MAP.get(source["type"], source["type"]),
        "source": source["name"],
        "eventIds": [e["event_id"] for e in envelopes[:50]],
    }


async def _gateway(method: str, path: str, **kwargs: Any) -> Any:
    try:
        async with http_client() as client:
            response = await client.request(
                method, f"{get_settings().mockgw_url.rstrip('/')}{path}", **kwargs
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError(
            502, "gateway_unavailable", f"the SMS gateway did not answer: {type(exc).__name__}"
        ) from exc


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _digits(phone: Any) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())


@router.get("/api/sim/phone")
async def sim_phone(who: Actor, pool: Pool) -> dict[str, Any]:
    """What the fake SMS / phone gateway has sent (and received), by person, newest first."""
    state = await _gateway("GET", "/admin/state")
    rows = await pool.fetch("SELECT email, phone FROM users WHERE phone IS NOT NULL")
    by_phone = {_digits(r["phone"]): r["email"].split("@", 1)[0] for r in rows}
    messages = []
    for kind, key, field in (("sms", "sms", "to"), ("call", "calls", "to"), ("reply", "replies", "from")):
        for m in state.get(key) or []:
            messages.append(
                {
                    "kind": kind,
                    "person": by_phone.get(_digits(m.get(field))),
                    "to": m.get(field),
                    "text": m.get("text") or "",
                    "at": _epoch(m.get("at")),
                    "status": m.get("status"),
                }
            )
    messages.sort(key=lambda m: m["at"] or 0, reverse=True)
    failures = state.get("failures") or {}
    down = any(name in failures for name in (*GATEWAY_CHANNELS, "all"))
    return {"down": down, "page": get_settings().public_mockgw_url, "messages": messages}


class SimGateway(BaseModel):
    down: bool


@router.post("/api/sim/gateway")
async def sim_gateway(body: SimGateway, who: Actor) -> dict[str, Any]:
    for channel in GATEWAY_CHANNELS:
        await _gateway(
            "POST", "/admin/fail", json={"channel": channel, "seconds": DOWN_SECONDS if body.down else 0}
        )
    return {"down": body.down}


class SimReply(BaseModel):
    person: str
    text: str


@router.post("/api/sim/reply")
async def sim_reply(body: SimReply, who: Actor, pool: Pool, rabbit: RabbitDep) -> dict[str, Any]:
    """A person replies from their phone (`ACK 17`, `APPROVE 4`, ...): our inbound SMS handler."""
    async with pool.acquire() as conn:
        user = await user_by_person(conn, body.person)
    if user is None or not user["phone"]:
        raise not_found("a phone for that person")
    reply = await handle_sms(pool, rabbit, user["phone"], body.text)
    return {"handled": True, "from": user["phone"], "text": body.text, "reply": reply}


@router.get("/api/sim/mail")
async def sim_mail(who: Actor) -> dict[str, Any]:
    """The last emails captured by Mailpit."""
    settings = get_settings()
    try:
        async with http_client() as client:
            response = await client.get(f"{settings.mailpit_api}/api/v1/messages", params={"limit": 20})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return {"page": settings.public_mailpit_url, "messages": [], "unavailable": True}
    return {
        "page": settings.public_mailpit_url,
        "messages": [
            {
                "id": m.get("ID"),
                "subject": m.get("Subject"),
                "to": [t.get("Address") for t in m.get("To") or []],
                "at": m.get("Created"),
                "snippet": m.get("Snippet"),
            }
            for m in data.get("messages") or []
        ],
    }
