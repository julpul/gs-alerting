# payments-api (Vistula Capital, demo codebase)

The card-payments service of our demo organisation. Herald links it to the `prometheus` platform, so every
payments-api alert has a **"Fix it in the code"** card: open the code, run Claude Code with the alert as
context, or open a draft pull request.

It carries the real bug from the incident history: release **3.8.0** added a fraud-score client **without a
timeout**. When the fraud-score service is slow, card authorisations wait for it and time out upstream: Herald
raises `CardGatewayTimeouts` / `HighErrorRate` on payments-api.

```bash
cd demo/payments-api
python -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
pytest            # test_slow_fraud_score_does_not_block_authorisation fails until the bug is fixed
uvicorn payments_api.app:app --port 8300
```

The fix is to give the fraud-score call a short timeout and fall back to manual review when it does not
answer in time. A Claude Code session started from Herald finds and fixes it.
