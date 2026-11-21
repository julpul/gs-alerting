---
title: "Payments: region failover procedure (eu-west / eu-central / us-east)"
kind: wiki
domain: payments
team: team-payments
---

# Payments: region failover procedure

Owner: Team Payments. Last reviewed by Robert Zalewski. Applies to `payments-api` and the card/SEPA gateways.

## Topology

- Three regions: `eu-west` (primary for EU cards), `eu-central` (hot standby, also primary for SEPA), `us-east`
  (USD flows only, do not fail EU traffic over to it).
- Each region has its own payments DB (async replication eu-west -> eu-central, lag normally below 2 s),
  Redis session cache, and a link to the card acquirer.
- Traffic split is controlled by the global weights in `traffic-weights` (Argo Rollouts / mesh gateway). Weights are
  integers 0-100 per region per product.

## When to fail over

Fail over only if at least one of these holds and a rollback or restart will not fix it:

1. The acquirer link in a region is degraded (authorisation timeouts, `CardGatewayTimeouts` firing).
2. A region-wide infrastructure incident (cloud provider status page confirms).
3. `HighErrorRate` is critical for more than 10 minutes and the cause is upstream of our code.

Do not fail over for an error spike that started right after a deploy: roll back instead (see the bad-deploy
post-mortem). Do not fail over while DB replica lag is above 30 s without checking with the DB on-call first.

## Procedure

1. Announce in the incident channel and acknowledge the alert so escalation stops.
2. Check target region health: error rate below 1 %, DB replica lag below 5 s, acquirer link healthy.
3. Run the runbook action `failover_region` with `from=eu-west`, `to=eu-central`, `percent=10`. This action is high
   risk: it creates an approval for on-call and Domain Root. Approve from the UI or by SMS reply `APPROVE <id>`.
4. Watch the error rate and p99 for 3 minutes, then raise to 50 and 100 percent.
5. Open a ticket with the acquirer / provider, note the ticket number in the alert comments.

## Failing back

1. Wait for written confirmation from the provider that the issue is fixed (status page alone is not enough for the
   acquirer).
2. Fail back in steps: 10 %, 50 %, 100 %, 3 minutes apart, watching the error rate in the returning region.
3. Resolve the alert with a resolution note: cause, time of failover and fail-back, who approved.

## Known pitfalls

- Sessions in Redis are region-local: users in a failed-over region may have to re-authenticate. This is expected.
- Failing over eu-west to eu-central doubles the load on eu-central's DB connections: check `payments-db`
  connection usage (alert `PaymentsDbConnectionsExhausted`) before going beyond 50 %.
- The reporting jobs of other teams must not run against the primary during a failover (PAY-1210).
