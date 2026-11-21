---
title: "Post-mortem: order-settlement failures at validate-counterparty (counterparty TIMEOUT)"
kind: postmortem
domain: settlements
team: team-settlements
---

# Post-mortem: order-settlement failures at validate-counterparty

Incident: counterparty TIMEOUT wave, 23 workflows affected (see alert history)
Authors: Piotr Lis, Kasia Zielinska. Reviewed by Ilona Szymanska (Head of Settlements Technology).
Status: closed, follow-ups tracked in SET-498, SET-512, SET-530.

## Summary

For 1 h 35 min the `order-settlement` workflow engine failed at step `validate-counterparty` with
`error.code = TIMEOUT` ("Counterparty service did not respond in 5000 ms"). 23 workflows were affected. 21 were
retried successfully with `retry_workflow`, 2 needed a manual settlement date correction by operations. No
settlement was lost or settled twice, but two instructions missed the 10:30 cut-off and settled one hour late.

## Impact

- 23 workflows failed, all environment `prod`, all in `validate-counterparty`.
- Hard page to on-call (phone + SMS per the Settlements rule); acknowledged after 6 minutes, inside the 15-minute
  ack window for severity error, so no escalation. Domain Root Settlements joined the thread anyway.
- Trading Operations were informed by the soft notification and asked twice for a status update.

## Timeline (UTC+1)

| Time | Event |
| --- | --- |
| 09:02 | First TIMEOUT from `validate-counterparty`, alert opened, on-call paged |
| 09:08 | Piotr acknowledges (6 min, no escalation needed); Domain Root joins the thread |
| 09:16 | `pg_stat_statements` on counterparty-service DB shows a sequential scan on `counterparty_eligibility` (5.8 s avg) |
| 09:54 | Ticket SET-498 opened for the index migration |
| 10:20 | `CREATE INDEX CONCURRENTLY ... ON counterparty_eligibility (bic)` finished, p99 drops from 6 s to 120 ms |
| 10:37 | 23 workflows retried, 21 ok, 2 handed to ops |

## Root cause

The `counterparty_eligibility` table doubled in size after the quarterly data load, the lookup by `bic` had no index,
and an autovacuum on the same table started at 09:00. Each eligibility call took 5-8 s, longer than the 5000 ms client
timeout of `validate-counterparty`, so every settlement attempt timed out.

## What went well

- The alert showed the failing step and error code immediately, so nobody started debugging the wrong service.
- `retry_workflow` is idempotent: retrying all 23 workflows was safe.

## What went badly

- The 5000 ms timeout is the same for a cold and a warm cache, so any slowdown of counterparty-service becomes a
  settlement failure.
- No alert on counterparty-service latency itself; we only learned about it through failing settlements.

## Follow-ups

1. SET-498 (done): index on `counterparty_eligibility(bic)`, added to the migration set.
2. SET-512: counterparty-service gets a preStop drain hook so rolling restarts do not drop connections.
3. SET-530: warm the eligibility cache on deploy. A cold cache caused the next TIMEOUT incident 14 days later.
4. Make the timeout configurable without a deploy: flag `counterparty.timeoutMs` (default 5000, raised to 8000 during
   incidents with cold caches).

## Lessons for the next TIMEOUT alert

Check in this order: (1) `http_get` on counterparty-service `/health`, look at `eligibility_cache_hit_ratio`;
(2) DB slow queries on counterparty-service; (3) recent deploys or node events; (4) the cloud provider status
page. Retry workflows only after the health check is green.
