# --- loadtest ---
# Included from the root Makefile:  include loadtest/loadtest.mk
# All paths are relative to the repo root. Every knob is an environment variable
# (API_URL, RATE, DURATION, STEP_RATE, ...) documented in loadtest/README.md.

K6 ?= k6
LT_RAW ?= loadtest/results/raw
LT_PYTEST = uv run --with pytest --with matplotlib --with asyncpg --with httpx pytest loadtest/tests -q

.PHONY: t1 t2 t4 t5 t6 report loadtest-smoke loadtest-test loadtest-clean

t1: ## T1 latency: 100 ev/s for 5 min (DURATION, RATE override)
	@mkdir -p $(LT_RAW)
	RAW_DIR=$(LT_RAW) $(K6) run --summary-export $(LT_RAW)/t1.json loadtest/k6/t1_latency.js

t2: ## T2 throughput: +200 ev/s every 30 s through the batch endpoint
	@mkdir -p $(LT_RAW)
	RAW_DIR=$(LT_RAW) $(K6) run --summary-export $(LT_RAW)/t2.json loadtest/k6/t2_throughput.js

t4: ## T4 chaos: SMS outage + worker kill under 200 ev/s, then reconcile (exit 1 if LOST > 0)
	RAW_DIR=$(LT_RAW) bash loadtest/t4_chaos.sh

t5: ## T5 scaling: T2 at 1, 2 and 4 workers
	RAW_DIR=$(LT_RAW) bash loadtest/t5_scaling.sh

t6: ## T6 dedup: 500 identical events in 60 s, then verify 1 alert x500 and one hard notification
	@mkdir -p $(LT_RAW)
	RAW_DIR=$(LT_RAW) $(K6) run --summary-export $(LT_RAW)/t6.json loadtest/k6/t6_dedup.js
	uv run loadtest/reconcile.py dedup --run-file $(LT_RAW)/t6.run.json

report: ## Write loadtest/results/<date>/report.md + PNGs (REPORT_ARGS="--since 2h")
	RAW_DIR=$(LT_RAW) uv run loadtest/report.py $(REPORT_ARGS)

loadtest-smoke: ## 30 s CI smoke: T1 at 20 ev/s + T6 with 50 events, dedup verified
	@mkdir -p $(LT_RAW)
	RAW_DIR=$(LT_RAW) NAME=smoke_t1 DURATION=30s RATE=20 $(K6) run --quiet --summary-export $(LT_RAW)/smoke_t1.json loadtest/k6/t1_latency.js
	RAW_DIR=$(LT_RAW) NAME=smoke_t6 TOTAL=50 DURATION_S=10 $(K6) run --quiet --summary-export $(LT_RAW)/smoke_t6.json loadtest/k6/t6_dedup.js
	uv run loadtest/reconcile.py dedup --run-file $(LT_RAW)/smoke_t6.run.json

loadtest-test: ## Unit tests of report.py / reconcile.py (no infrastructure needed)
	$(LT_PYTEST)

loadtest-clean: ## Delete k6 raw results (reports in loadtest/results/<date>/ are kept)
	rm -f $(LT_RAW)/*.json $(LT_RAW)/*.txt $(LT_RAW)/*.killed
