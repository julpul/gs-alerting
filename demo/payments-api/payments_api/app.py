"""HTTP API: POST /authorise asks the fraud-score service, then the acquirer."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from payments_api.fraud import FraudScoreClient

app = FastAPI(title="payments-api", version="3.8.0")
fraud = FraudScoreClient()


class Authorisation(BaseModel):
    card_token: str
    amount_minor: int
    currency: str = "EUR"


@app.post("/authorise")
def authorise(body: Authorisation) -> dict[str, str]:
    decision = fraud.decision(body.card_token, body.amount_minor, body.currency)
    if decision == "review":
        return {"status": "held_for_review"}
    return {"status": "sent_to_acquirer"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
