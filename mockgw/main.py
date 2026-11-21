"""Stub SMS / phone gateway. P2-DELIVERY extends this with chaos toggles, workflows, tickets, HTML log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="mockgw")

STATE: dict[str, list[dict[str, Any]]] = {"sms": [], "calls": []}


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    to: str
    text: str = ""


def _record(kind: str, payload: dict[str, Any], key: str | None) -> dict[str, Any]:
    if key:
        for item in STATE[kind]:
            if item.get("idempotencyKey") == key:
                return item
    item = {
        "id": f"{kind[:3]}-{len(STATE[kind]) + 1}",
        "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "idempotencyKey": key,
        **payload,
    }
    STATE[kind].append(item)
    return item


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sms")
async def sms(body: Message, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    return _record("sms", body.model_dump(), idempotency_key)


@app.post("/call")
async def call(body: Message, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
    return _record("calls", body.model_dump(), idempotency_key)


@app.get("/admin/state")
async def admin_state() -> dict[str, list[dict[str, Any]]]:
    return STATE


@app.post("/admin/reset")
async def admin_reset() -> dict[str, str]:
    for items in STATE.values():
        items.clear()
    return {"status": "reset"}
