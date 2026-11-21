# Presentation: slide copy and speaker notes

Every number here was measured, not estimated: see `README.md` → Measured performance and `loadtest/results/`.

---

## Slide: Performance (measured, not promised)

**Headline:** 100 events/s for 5 minutes. Alert on screen in **5.6 ms p50 / 52 ms p99**. Email, SMS and phone in **< 80 ms p99**.

| Test | Result | Target |
| --- | --- | --- |
| Ingest → UI latency (100 ev/s, 5 min, 6,404 alerts) | **p50 5.6 ms · p99 52 ms** | < 300 ms |
| Ingest → email / SMS / phone, p99 | **73 / 78 / 78 ms** | < 1 s |
| Sustained throughput, 4 workers | **1,500–2,500 events/s** | ≥ 1,000 |
| Chaos: SMS gateway down 60 s + worker killed (`SIGKILL`) | **0 lost** (6,454 / 6,454) | 0 |
| Alert storm: 500 identical errors | **1 alert ×500**, 1 phone call | 1 |

**Speaker note:** "All of this ran on one laptop, with the load generator, the database, the broker and the AI on the same 10 cores. The numbers are a floor, not a ceiling."

---

## Slide: How it scales horizontally

**Headline:** Stateless services, durable queues, one image. Add replicas and throughput grows.

- **Stateless roles.** The api, worker, delivery, scheduler, AI and connector roles all run from **one Docker image**. All state lives in Postgres, Redis and RabbitMQ, so any role can run N copies.
- **The queue absorbs spikes.** Ingest returns `202` as soon as RabbitMQ confirms the message. Processing catches up afterwards. Priority queues put critical alerts first.
- **No leader election.** Scheduler jobs use `FOR UPDATE SKIP LOCKED`, so any number of schedulers is safe.
- **The database is off the hot path.** We do not write each event to Postgres. Duplicates collapse into one row update, and the AI runs asynchronously, never in the event path.
- **Measured scaling.** One worker process handles ≈ 1,250 ev/s. On a laptop, 2 workers reach 1,750 ev/s, and beyond that the shared host CPU is the limit, not our code.

**Speaker note:** "The worker is the scaling knob. In the cloud each worker is its own container, so the laptop's CPU limit disappears."

---

## Slide: Production in the cloud (Terraform, AWS)

**Headline:** `terraform apply` gives you a multi-AZ deployment that scales itself.

```
Route 53 → ALB (HTTPS, WAF) → ECS Fargate: api 2–20 ─┐
                                                   ├─ Amazon MQ (RabbitMQ, 3-node cluster)
                 ECS Fargate: worker 4–40, delivery 2–20, ai 1–4, scheduler ×2, connector ×1
                                                   ├─ RDS PostgreSQL 16 + pgvector (Multi-AZ)
                                                   └─ ElastiCache Redis (Multi-AZ)
```

- **What scales on which signal:**
  - api: requests per target and CPU;
  - worker and delivery: **RabbitMQ queue depth** and CPU;
  - ai: CPU;
  - scheduler: 2 copies, for availability;
  - connector: 1 copy.
- **Surge mode.** When more than 50,000 events are waiting, the workers scale up by +50% or +100% in one step. An alarm fires if anything lands in the dead-letter queue.
- **Losing an availability zone** loses no alerts. Publisher confirms, the outbox and the dead-letter queue keep `LOST = 0`, the property our chaos test showed.
- **Secrets** live in AWS Secrets Manager. Logs go to CloudWatch.
- **Code:** `infra/terraform/` (validated with `terraform validate`, not applied: no AWS account for the hackathon). Baseline cost is about **$1,950 per month** in eu-central-1 (estimate ±30%), with a dev variant at about $1,050. Details are in `infra/terraform/README.md`.

**Speaker note (be honest if asked):** the RabbitMQ queues are classic priority queues, so a broker node failure pauses the queues on that node until it returns. Persistent messages are not lost. The SES email adapter is provisioned but not wired up yet.

**Speaker note:** "Today it runs on Railway for the demo. The same image goes to ECS without code changes; only the configuration differs."

---

## Slide: Sign-in and security

- **Single sign-on (OpenID Connect).** The company identity provider (Entra ID, Okta, Keycloak) signs people in, and Herald keeps no passwords. In the demo, **Vistula ID**, our own OpenID Connect provider with PKCE, signed tokens and an account picker, stands in for the corporate one; swapping in Entra ID is configuration only.
- **Emergency login** with username and password, for when the identity provider is down during an incident. It is rate-limited and audited.
- **Sessions are server-side** (HttpOnly cookie, 8 h). In production, identity headers from clients are ignored.
- **Least privilege by scope.** Every query is filtered by the person's domain and team.
- **Hash-chained audit log** of every login, action and confirmation.
- **Risky actions need a human confirm.** Failover and rollback run only after a person confirms in the alert. AI agents (MCP) can only ask.
- **AI safety.** Event content is redacted and wrapped as untrusted before any model call. Prompt injection is detected and shown in the UI, and the AI never runs actions itself.

---

## Slide: AI where you already work

- **Inside the alert:** an *AI proposed solution* card with diagnosis, the next runbook step, and **Fixed before** (the past alert and how it was fixed). Accept or Reject.
- **Inside the alert:** *Ask about this alert*, and on Resolve, a drafted resolution note.
- **Rule editor:** describe the rule in words, and the AI fills in the form and runs a dry-run before you save.
- **Runbooks:** upload a YAML, Markdown or free-text file. It becomes a draft that a person accepts.
- **Agents via MCP:** Claude Code or any MCP client can list alerts, acknowledge them and ask to run steps, with personal tokens.
- **Automatic owner:** every new alert gets a person in charge. The rule is a simple, explainable chain: the person named in the rule, else the team's on-call person, else the least busy team member.
