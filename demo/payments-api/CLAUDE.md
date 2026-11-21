# payments-api

Card authorisation service (FastAPI). Fraud-score client: `payments_api/fraud.py`.

- Test: `pip install -e '.[test]' && pytest` (Python 3.12).
- Herald alerts for this service (`CardGatewayTimeouts`, `HighErrorRate`) can be read through the Herald MCP
  server (`get_alert`, `get_codebase`) when it is connected.
- A fix must keep authorisations under the acquirer budget (0.5 s) and hold the payment for review when the
  fraud score is not available in time. Add or update a test, then open a pull request naming the Herald alert (A-n).
