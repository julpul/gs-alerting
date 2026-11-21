"""mockgw: fake SMS / phone / workflow / ticket gateway with a chaos toggle and a phone-like log page.

Endpoints
  POST /sms, /call                       record a message (honours `Idempotency-Key`); 503 while failing
  POST /admin/fail {channel, seconds}    make a channel fail with 503 for N seconds (0 clears); optional
                                         `count` = fail the next N calls instead. Channels: sms, phone,
                                         workflow, restart, tickets, failover, rollback, `svc:<name>`
                                         (health check), `all`
  POST /admin/reset, GET /admin/state    wipe everything / JSON dump for tests
  POST /workflows/{id}/retry             retry a workflow (fails while `workflow` is failing)
  POST /services/{name}/restart          restart a service
  GET|POST /tickets                      Jira-like tickets
  GET /svc/{name}/health                 health check (503 while `svc:<name>` is failing)
  POST /failover, /rollback              region failover / deploy rollback
  GET /                                  HTML page: SMS and calls, reply box, "Press 1 to ACK"
  POST /reply, /calls/{id}/press         forward a reply / keypress to `{BACKEND_URL}/v1/inbound/sms`
                                         (sends `X-Inbound-Secret: $INBOUND_SMS_SECRET` when set)
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from page import render_page

app = FastAPI(title="mockgw")

STATE: dict[str, list[dict[str, Any]]] = {}
FAILURES: dict[str, dict[str, Any]] = {}
clock: Callable[[], float] = time.monotonic
WORKFLOW_RETRIES: dict[str, int] = {}
SHORT_ID_RE = re.compile(r"A-(\d+)|ACK (\d+)")


def _fresh_state() -> dict[str, list[dict[str, Any]]]:
    return {
        k: []
        for k in ("sms", "calls", "tickets", "workflows", "restarts", "failovers", "rollbacks", "replies")
    }


STATE.update(_fresh_state())


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    to: str
    text: str = ""


class FailRequest(BaseModel):
    channel: str
    seconds: float | None = None
    count: int | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _check_failure(channel: str) -> None:
    """Raise 503 if `channel` (or `all`) is currently failing; consumes one call of a counted failure."""
    for name in (channel, "all"):
        entry = FAILURES.get(name)
        if entry is None:
            continue
        if entry["until"] is not None and clock() >= entry["until"]:
            del FAILURES[name]
            continue
        if entry["count"] is not None:
            if entry["count"] <= 0:
                del FAILURES[name]
                continue
            entry["count"] -= 1
        raise HTTPException(status_code=503, detail=f"{channel} gateway unavailable (chaos toggle)")


def _record(kind: str, payload: dict[str, Any], key: str | None) -> dict[str, Any]:
    if key:
        for item in STATE[kind]:
            if item.get("idempotencyKey") == key:
                return {**item, "deduplicated": True}
    item = {
        "id": f"{kind[:3]}-{len(STATE[kind]) + 1}",
        "at": _now(),
        "idempotencyKey": key,
        "status": "queued" if kind == "sms" else "ringing",
        **payload,
    }
    STATE[kind].append(item)
    return item


def _backend_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=os.environ.get("BACKEND_URL", "http://localhost:8080"), timeout=3.0)


def _inbound_headers() -> dict[str, str]:
    secret = os.environ.get("INBOUND_SMS_SECRET", "")
    return {"X-Inbound-Secret": secret} if secret else {}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sms")
async def sms(body: Message, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    _check_failure("sms")
    return _record("sms", body.model_dump(), idempotency_key)


@app.post("/call")
async def call(body: Message, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    _check_failure("phone")
    return _record("calls", body.model_dump(), idempotency_key)


@app.post("/admin/fail")
async def admin_fail(body: FailRequest) -> dict[str, Any]:
    """Fail `channel` for `seconds` (default 60) or for the next `count` calls; 0 clears the toggle."""
    if (body.seconds is not None and body.seconds <= 0) or (body.count is not None and body.count <= 0):
        FAILURES.pop(body.channel, None)
        return {"channel": body.channel, "failing": False}
    seconds = body.seconds if body.seconds is not None or body.count is not None else 60.0
    FAILURES[body.channel] = {"until": None if seconds is None else clock() + seconds, "count": body.count}
    return {"channel": body.channel, "failing": True, "seconds": seconds, "count": body.count}


@app.get("/admin/state")
async def admin_state() -> dict[str, Any]:
    failures = {
        name: {
            "remainingSeconds": None if f["until"] is None else max(round(f["until"] - clock(), 3), 0),
            "count": f["count"],
        }
        for name, f in FAILURES.items()
    }
    return {**STATE, "failures": failures}


@app.post("/admin/reset")
async def admin_reset() -> dict[str, str]:
    STATE.clear()
    STATE.update(_fresh_state())
    FAILURES.clear()
    WORKFLOW_RETRIES.clear()
    return {"status": "reset"}


@app.post("/workflows/{workflow_id}/retry")
async def workflow_retry(workflow_id: str) -> dict[str, Any]:
    try:
        _check_failure("workflow")
    except HTTPException:
        STATE["workflows"].append({"workflowId": workflow_id, "at": _now(), "result": "failed"})
        raise HTTPException(status_code=500, detail=f"workflow {workflow_id} retry failed") from None
    WORKFLOW_RETRIES[workflow_id] = WORKFLOW_RETRIES.get(workflow_id, 0) + 1
    STATE["workflows"].append({"workflowId": workflow_id, "at": _now(), "result": "retried"})
    return {"workflowId": workflow_id, "status": "retried", "attempt": WORKFLOW_RETRIES[workflow_id]}


@app.post("/services/{name}/restart")
async def service_restart(name: str) -> dict[str, Any]:
    _check_failure("restart")
    FAILURES.pop(f"svc:{name}", None)
    STATE["restarts"].append({"service": name, "at": _now()})
    return {"service": name, "status": "restarted"}


@app.get("/svc/{name}/health")
async def service_health(name: str) -> dict[str, str]:
    try:
        _check_failure(f"svc:{name}")
    except HTTPException:
        raise HTTPException(status_code=503, detail=f"{name} is down") from None
    return {"service": name, "status": "ok"}


@app.post("/tickets")
async def create_ticket(body: dict[str, Any] | None = None) -> dict[str, Any]:
    _check_failure("tickets")
    n = len(STATE["tickets"]) + 1
    ticket = {"id": f"TCK-{n}", "at": _now(), "status": "open", **(body or {})}
    STATE["tickets"].append(ticket)
    return ticket


@app.get("/tickets")
async def list_tickets() -> list[dict[str, Any]]:
    return STATE["tickets"]


@app.post("/failover")
async def failover(body: dict[str, Any] | None = None) -> dict[str, Any]:
    _check_failure("failover")
    entry = {"at": _now(), "region": (body or {}).get("region", "eu-west-2")}
    STATE["failovers"].append(entry)
    return {"status": "failed_over", **entry}


@app.post("/rollback")
async def rollback(body: dict[str, Any] | None = None) -> dict[str, Any]:
    _check_failure("rollback")
    entry = {"at": _now(), "version": (body or {}).get("version", "previous")}
    STATE["rollbacks"].append(entry)
    return {"status": "rolled_back", **entry}


async def _forward_reply(sender: str, text: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": _now(), "from": sender, "text": text}
    try:
        async with _backend_client() as client:
            resp = await client.post(
                "/v1/inbound/sms",
                json={"from": sender, "text": text},
                headers=_inbound_headers(),
            )
        entry["backendStatus"] = resp.status_code
    except httpx.HTTPError as exc:
        entry["backendStatus"] = None
        entry["error"] = repr(exc)
    STATE["replies"].append(entry)
    if entry["backendStatus"] is None:
        raise HTTPException(status_code=502, detail="backend unreachable")
    return entry


@app.post("/reply")
async def reply(body: dict[str, Any]) -> dict[str, Any]:
    return await _forward_reply(str(body.get("from") or ""), str(body.get("text") or ""))


@app.post("/calls/{call_id}/press")
async def press(call_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Keypad press on a ringing call; digit `1` acknowledges the alert named in the call text."""
    call_item = next((c for c in STATE["calls"] if c["id"] == call_id), None)
    if call_item is None:
        raise HTTPException(status_code=404, detail="no such call")
    digit = str(body.get("digit", ""))
    call_item["status"] = "answered" if digit else call_item["status"]
    call_item["pressed"] = digit
    if digit != "1":
        return {"id": call_id, "pressed": digit}
    match = SHORT_ID_RE.search(call_item.get("text", ""))
    if match is None:
        raise HTTPException(status_code=422, detail="call text names no alert")
    return await _forward_reply(call_item["to"], f"ACK {match.group(1) or match.group(2)}")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return render_page()
