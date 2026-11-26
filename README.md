# Paisa-X

A simulated payment orchestrator that routes each payment to the healthiest gateway, handles failures safely, and measures what smart routing saves.

**Status:** Phases 0-9 are finished: routing experiment ([verdict](docs/decisions/0002-phase0-verdict.md)), Gateway Simulator ([notes](docs/decisions/0003-gateway-simulator.md)), safe payment core ([notes](docs/decisions/0004-safe-payment-core.md)), recovery ([notes](docs/decisions/0005-recovery-and-webhooks.md)), rule-based router ([notes](docs/decisions/0006-rule-based-router.md)), outbox + Kafka ([notes](docs/decisions/0007-outbox-and-kafka.md)), streaming features + data lake ([notes](docs/decisions/0008-streaming-features-and-lake.md)) and the learning router with shadow and canary rollout ([notes](docs/decisions/0009-learning-router.md)), and the Replay Lab with the final locked-scenario results ([report](docs/results/replay-report.md), [notes](docs/decisions/0010-replay-lab.md)), and hardening: one-command start, metrics, dashboards, trace-ID logs, load test, chaos drills and CI ([notes](docs/decisions/0011-hardening.md)). Rolling + breaker stays the default router.

## Run it

**You need:** [uv](https://docs.astral.sh/uv/), [Docker Desktop](https://www.docker.com/products/docker-desktop/) and PostgreSQL 16. About 2 GB of free RAM.

**Postgres.** Pick one of these:
- **Windows, no install:** unzip `postgresql-16.13-1-windows-x64-binaries.zip` from EnterpriseDB so that `.local/pgsql/bin/pg_ctl.exe` exists. Paisa-X starts and stops it for you, on port 55432.
- **Any OS:** point Paisa-X at a Postgres 16 you already run: `PAISA_PG_URL=postgresql://user:password@127.0.0.1:5432/{database}`. Keep `{database}` exactly as written.

Then:

```
uv sync                         # install Python packages
uv run paisa up                 # start Postgres, Kafka, Redis, Prometheus, Grafana, simulator, API and workers
uv run paisa create-merchant    # prints a test API key
uv run paisa status             # what is running
uv run paisa down               # stop everything and free RAM
```

**Open these:**
- API docs: http://127.0.0.1:8000/docs
- Grafana dashboard: http://127.0.0.1:3000
- Prometheus: http://127.0.0.1:9090

Logs are JSON lines in `.local/logs/`. Each API answer carries an `X-Trace-Id` header, and every log line of that request has the same `trace_id`.

**Check it:**

| Command | What it does |
|---|---|
| `uv run pytest -q` | All tests |
| `uv run paisa chaos` | Every failure drill, then a money audit after each one. Exits 1 if any rule broke (about 15 minutes). **Erases the local Kafka topics and Redis data.** |
| `uv run python load/run.py` | k6 load test in Docker; writes [load-report.md](docs/results/load-report.md) |
| `uv run python -m paisa.replay` | Replay Lab on the tuning scenarios |

All payments, gateways, banks and customers are simulated. This project handles no real money or real card data.

## Start here

1. [Architecture and system design](docs/ARCHITECTURE.md)
2. [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
3. [Build rules](docs/BUILD_RULES.md), including the rule to comment nearly every code line in simple terms
