---
title: "Policy: trade limits and rejected trades"
kind: policy
domain: markets
team: team-rates
---

# Policy: trade limits and rejected trades

Owner: Markets Risk with Team Rates. Applies to all desks that trade through `trade-gateway`.

## Limit types

| Limit | Scope | Typical reject reason |
| --- | --- | --- |
| Desk DV01 / notional limit | per desk, per currency | `LIMIT_BREACH` |
| Counterparty exposure limit | per counterparty | `LIMIT_BREACH` |
| Single order sanity limit | any order above 10x the desk's median order size | `NOTIONAL_SANITY` |
| Price freshness | price older than 2 s at order time | `STALE_PRICE` |

Limits are refreshed every night by `risk-limits-sync`. Risk can lower a limit intraday (for example after a volatility
spike); the desk head must be told through the desk channel.

## What generates an alert

An alert is raised for a rejected trade (`trade.rejected.*`) on the Rates desk with a notional above EUR 10,000,000
(or equivalent). Smaller rejections and other desks are visible in the gateway metrics but do not page anyone.

## Who does what

- **Team Rates on-call** acknowledges within 15 minutes (severity error), checks the reject reason and the
  current limit utilisation.
- `LIMIT_BREACH` caused by a correct limit: no technical action; the trader decides (split order, ask Risk for a
  temporary increase). Temporary increases are approved by the Head of Rates trading and Markets Risk, valid for
  one trading day, recorded in the limit register.
- `LIMIT_BREACH` while the limit looks wrong (for example Friday's reduced limits still active on Monday): check the
  last run of `risk-limits-sync`; re-run it manually (low risk) and tell the trader to re-enter the order.
- `STALE_PRICE`: look at Team Pricing alerts of the same hour (cache issues on pricing-engine-3 are the usual cause).
- `NOTIONAL_SANITY`: fat finger, confirm with the trader and re-enter.

## Escalation

If a legitimate order cannot be placed within 15 minutes because of a wrong limit, escalate to Domain Root Markets;
informed on every alert of this rule: Head of Rates trading and Head of Trading Operations.

## Record keeping

Every rejected trade above the alerting threshold must have a resolution note naming reason and decision. Notes are
reviewed monthly by Markets Risk.
