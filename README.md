# gs-alerting — Flexible Alerting & Notification Service

Goldman Sachs Hackathon 2026 · Software Engineering track.

> Work in progress. Spec: [`docs/TECH_SPEC.md`](docs/TECH_SPEC.md) · API contract: [`docs/openapi.yaml`](docs/openapi.yaml) · Plan: [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · Progress: [`docs/PROGRESS.md`](docs/PROGRESS.md)

## Quick start
```bash
cp .env.example .env     # add an LLM key for AI features (optional)
make up                  # full stack: api, workers, postgres, redis, rabbitmq, mailpit, mockgw
make demo-events         # send the sample events from the brief
```
