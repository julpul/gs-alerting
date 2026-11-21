---
title: "Pricing engine: cache latency runbook (free text, as the team wrote it)"
kind: runbook
domain: markets
team: team-pricing
---

Pricing engine latency warnings - what to do

If you get latency WARN lines from pricing-engine (usually pricing-engine-3, the one that does the rates curves),
first look at the curve cache hit ratio in the Grafana board "Pricing / Curve cache". If the miss ratio is above
0.5 the cache is cold or was flushed, and that is almost always the reason. Log lines look like "Quote latency
900ms exceeds SLO 250ms, curve cache miss ratio 0.78".

Then run the warm-up: `curve-cache warm --all`. It takes about two or three minutes and latency should be under 40 ms
again afterwards. If it fails with an authentication error the market data token expired (this has happened, see
PRC-88), rotate the token in the vault first and run the warm-up again.

If the cache hit ratio is fine but latency is still high, check whether Redis is resharding (maintenance window in
the team calendar) - in that case just wait about ten minutes, nothing to do. Check the pod memory too: if the
container is close to its limit the garbage collector is the problem, then restart the pod (ask on-call of the Domain
before restarting more than one instance, restarting is a high risk action because quotes go stale for about 20
seconds).

If nothing helps after 30 minutes, tell Team Rates, because trades can get rejected for STALE_PRICE while the engine
is slow, and escalate to the Markets Domain Root.

After the incident write a resolution note with the real cause: cold cache after nightly rebuild, expired warm-up
token, Redis maintenance, or memory. Next time somebody will look for exactly this.
