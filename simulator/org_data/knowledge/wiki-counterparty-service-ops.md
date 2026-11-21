---
title: "counterparty-service: operations page"
kind: wiki
domain: settlements
team: team-settlements
---

# counterparty-service: operations

Owner: Team Settlements. On-call: see the Settlements rotation (weekly, handover Monday 09:00 Europe/Warsaw).

## What it does

Reference data and eligibility checks for counterparties (BIC, LEI, limits, sanctions flags). It is called
synchronously by step `validate-counterparty` of the `order-settlement` workflow with a **5000 ms client timeout**
(flag `counterparty.timeoutMs`). A slow counterparty-service therefore shows up as `WORKFLOW_FAILED` with
`error.code = TIMEOUT`.

## Dependencies

| Dependency | Why it matters |
| --- | --- |
| Postgres `counterparty-db` | Eligibility lookup by BIC (index `counterparty_eligibility_bic_idx` must exist) |
| Redis `eligibility-cache` | 15 min TTL cache; cold cache means 10x more DB calls |
| Sanctions list sync (nightly job) | If older than 24 h the service refuses to validate (VALIDATION_ERROR) |

## Health and signals

- `GET /health` returns status plus `eligibility_cache_hit_ratio`. Healthy: ratio above 0.85, status `ok`.
- p99 of `/v1/eligibility` should be under 400 ms. Above 2 s the workflow engine starts timing out.
- Typical failure signatures:
  - cache ratio below 0.3 right after a flush or deploy: warm the cache (`eligibility-preload` job);
  - high DB CPU and seq scans: missing index or vacuum, see the post-mortem about counterparty timeouts;
  - rolling restart in progress: connections dropped for about 90 s, wait for the rollout to finish.

## Standard response to a TIMEOUT alert

1. `http_get /health` on counterparty-service (runbook step 1).
2. If cache ratio is low run `eligibility-preload`; if DB is slow check `pg_stat_statements`.
3. If the situation will last more than 15 minutes, raise `counterparty.timeoutMs` to 8000 (needs approval).
4. After health is green: `retry_workflow` for the failed workflows (idempotent, safe to run for all).
5. Resolve with a note naming the real cause.

## Escalation

Settlements on-call, then Domain Root Settlements (Ilona Szymanska), then the Master. For vendor-side network or
provider incidents note the provider ticket in the alert comments.

## Known follow-ups

SET-512 (preStop drain), SET-530 (warm cache on deploy), SET-541 (separate DB pool for reconciliation).
