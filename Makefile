COMPOSE_DEPS = docker compose -p gsalert -f infra/docker-compose.deps.yml
ENV_FILE = $(if $(wildcard .env),--env-file .env,)
COMPOSE_FULL = docker compose -p gsalert $(ENV_FILE) -f infra/docker-compose.yml
APP_SERVICES = seed api worker delivery scheduler ai connector
N ?= 2

.PHONY: infra-up infra-down up down rebuild deploy deploy-all seed-prod test lint fmt seed logs scale demo-events t1 t2 t4 t5 t6 report

infra-up:
	$(COMPOSE_DEPS) up -d --build --wait

infra-down:
	$(COMPOSE_DEPS) down

up:
	$(COMPOSE_FULL) up -d --build --wait

# Rebuild the image from this checkout and restart the app containers. Data (Postgres, Redis, RabbitMQ) is kept.
rebuild:
	$(COMPOSE_FULL) build api
	$(COMPOSE_FULL) up -d --no-deps --force-recreate api worker delivery scheduler ai connector simulator mockgw sso

# Railway without CI: `make deploy` (api + workers), `make deploy-all`, or scripts/deploy.sh <service...>
deploy:
	scripts/deploy.sh

deploy-all:
	scripts/deploy.sh all

# Re-run the seed on Railway (idempotent: org, rules, runbooks, DEMO_PASSWORD).
seed-prod:
	railway ssh --service api -- sh -c 'cd /app && .venv/bin/python -m seed.seed'

# Stops the application services only; shared dependencies keep running (stop them with `make infra-down`).
down:
	$(COMPOSE_FULL) rm -sf $(APP_SERVICES)

test:
	cd backend && uv run pytest -q --cov=app --cov-report=term-missing

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .

seed:
	cd backend && uv run python -m seed.seed

logs:
	$(COMPOSE_FULL) logs -f --tail=100

scale:
	$(COMPOSE_FULL) up -d --scale worker=$(N) --scale delivery=$(N)

demo-events:
	cd backend && uv run python scripts/demo_events.py

# --- loadtest ---
include loadtest/loadtest.mk
