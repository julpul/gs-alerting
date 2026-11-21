"""Fraud-score client (added in 3.8.0).

Every card authorisation asks the fraud-score service for a risk score before it goes to the acquirer.
"""

from __future__ import annotations

import os

import httpx

FRAUD_SCORE_URL = os.environ.get("FRAUD_SCORE_URL", "http://fraud-score.internal/score")
REVIEW_THRESHOLD = 0.7


class FraudScoreClient:
    def __init__(self, url: str = FRAUD_SCORE_URL, transport: httpx.BaseTransport | None = None) -> None:
        # BUG (3.8.0): no timeout. httpx's default is 5 s per phase, so a slow fraud-score service holds
        # every authorisation for seconds and the acquirer gateway times out (CardGatewayTimeouts).
        self._client = httpx.Client(transport=transport, timeout=None)
        self._url = url

    def score(self, card_token: str, amount_minor: int, currency: str) -> float:
        response = self._client.post(
            self._url, json={"card": card_token, "amount": amount_minor, "currency": currency}
        )
        response.raise_for_status()
        return float(response.json()["score"])

    def decision(self, card_token: str, amount_minor: int, currency: str) -> str:
        """`approve`, or `review` for risky payments."""
        risk = self.score(card_token, amount_minor, currency)
        return "review" if risk >= REVIEW_THRESHOLD else "approve"
