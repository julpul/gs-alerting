---
title: "Post-mortem: payments-api 3.8.0 error spike in eu-west (HighErrorRate)"
kind: postmortem
domain: payments
team: team-payments
---

# Post-mortem: payments-api 3.8.0 error spike in eu-west

Authors: Anna Kowalczyk, Marta Nowak. Reviewed by Robert Zalewski (Head of Payments Engineering).
Status: closed, follow-ups PAY-1187, PAY-1190.

## Summary

Roughly 8 minutes after the rollout of `payments-api` 3.8.0 finished in eu-west, the Prometheus alert `HighErrorRate`
(critical, `service=payments-api`, `region=eu-west`) fired with 12 % errors over 5 minutes. We rolled back to 3.7.9
with `rollback_deploy` (approved by the Domain Root) and the error rate returned to normal four minutes later. The
incident lasted 31 minutes from alert to resolution.

## Impact

- About 12 % of payment confirmations in eu-west failed with HTTP 502/504 for 19 minutes.
- Clients retried automatically; no duplicated payments were detected (idempotency keys worked).
- Anna was paged by phone and SMS (her personal rule matches critical `payments-api`), acknowledged after 2 minutes.

## Root cause

3.8.0 introduced a client for the new fraud-score service without a timeout. Under normal load the call took 40 ms,
but the fraud-score service was running a model refresh and answered in 6-9 s. Request threads piled up until the
pods stopped answering health checks.

## Detection and response

- Alert fired 4 minutes after the first 5xx (5-minute window in the rule).
- Anna correlated the start of the 5xx with the end of the rollout (09:41), decided to roll back instead of
  forward-fixing, which saved time.
- The rollback needed an approval (risk high). root.payments approved from the approvals inbox within 2 minutes.

## What went well

- Rolling back was a single runbook step with an approval, not a manual pipeline hunt.
- Idempotency keys prevented double charges during client retries.

## What went badly

- A new outbound dependency shipped without a timeout and without a circuit breaker; code review missed it.
- Canary stage was skipped because the release was marked as "low risk".

## Follow-ups

1. PAY-1187 (done): 3.8.1 sets an 800 ms timeout and a circuit breaker on the fraud-score client.
2. PAY-1190 (done): release checklist now requires a canary for any change that adds an outbound HTTP call.
3. Lint rule: HTTP clients must set a timeout explicitly.

## Lessons for the next HighErrorRate alert

If the error start coincides with a deploy in the same region, roll back first and investigate afterwards. If no
deploy happened, check upstream dependencies (acquirer, fraud-score, Redis) before touching our pods. See the
"Payments region failover" wiki page for the failover procedure.
