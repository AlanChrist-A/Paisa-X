# Decision 0011: Hardening (one-command start, monitoring, logs, load test, chaos, CI)

**Decision:** Paisa-X now starts and stops with one command. It has Prometheus metrics, a Grafana dashboard, JSON logs with trace IDs and a k6 load test. `paisa chaos` runs every failure drill and audits the money after each one. A GitHub Actions workflow is ready for the first push.

## What was built

| Piece | Command or place | User choice |
|---|---|---|
| One-command start and stop | `uv run paisa up`, `status`, `down` | The full stack: Postgres, Kafka, Redis, Prometheus, Grafana, simulator, API, Recovery Worker, Relay, Feature Worker |
| Metrics | API `/metrics` (port 8000); Recovery Worker on 8001; Relay on 8002 | |
| Dashboards | Grafana http://127.0.0.1:3000, 8 panels, provisioned from files | Prometheus and Grafana in Docker, always on |
| Logs | `.local/logs/*.log`, one JSON object per line | |
| Load test | `uv run python load/run.py`; writes [load-report.md](../results/load-report.md) | k6 in Docker, so nothing is installed on Windows |
| Chaos | `uv run paisa chaos`; writes [chaos.json](../results/chaos.json) | |
| CI | `.github/workflows/ci.yml` | Written now; it runs when you push. Claude never pushes |

## Measured results

**Load (this laptop, the whole stack running):** latency is flat up to 20 payments per second (p95 = 0.8 s). At 40 per second it degrades: p95 = 11 s, and 16% of payments fail or are dropped. The real ceiling is about 25-34 payments per second. The bottleneck has not been profiled yet. The likely candidates are the single API process (one Python process, 10 database connections) and the single-process simulator.

**Chaos:** all 5 drills pass in 17.6 minutes, and the money audit is clean after each one:

| Drill | Pass rule |
|---|---|
| All tuning faults at once, 2,000 payments | 0 audit violations, 0 unresolved, 0 pending reversals |
| Busiest gateway killed mid-traffic | Audit clean |
| The same with the learning router on all traffic | Audit clean |
| Kafka stopped, restarted, relay killed after sending | Nothing missing, nothing out of order, the backlog empties |
| Redis wiped, Parquet sink killed mid-write | Streaming features match the batch recount, routing still works, lake quality checks clean |

`paisa chaos` exits with 1 if any drill fails or crashes. A test proves this (`tests/test_chaos.py`).

**Tests:** 181 pass. The full suite also passed through the CI database path (`PAISA_PG_URL`) against the local server.

## Design choices

| Choice | Reason | Trade-off |
|---|---|---|
| Trace ID from the `X-Trace-Id` header, or a new one; returned on every answer | A merchant can quote it in a support ticket, and one search finds every log line of that request | Workers log by `payment_id` and `attempt_id`, not by the request's trace ID, because the ID is not stored in the database |
| Metric labels use the route template (`/v1/payments/{payment_id}`), never raw IDs | Raw IDs would create one time series per payment and exhaust Prometheus's memory | Per-payment detail lives in logs, not metrics |
| Workers serve `/metrics` on their own port. A busy port only logs a warning | Workers have no web app. Payments matter more than metrics | A second copy of a worker has no metrics |
| Apps run on the laptop; Prometheus in Docker reads them through `host.docker.internal` | The apps stay bound to 127.0.0.1, so no port is open to the network | Linux needs the `host-gateway` mapping (already in compose.yaml) |
| `PAISA_PG_URL` switches from portable Postgres to any server | CI and non-Windows users need Postgres without the Windows zip | Two ways to run Postgres |
| CI lint checks only fatal problems (syntax errors, undefined names) | The existing code has 51 style findings; fixing them all now would be a large unrelated change | Style is not enforced yet |
| `paisa chaos` reuses the Phase 3-7 drills | One copy of each drill; CI and people run the same proof | 17.6 minutes; it stops `paisa up` apps and erases local Kafka topics and Redis data |
| Docker Desktop is started automatically by `paisa up` if it is installed | One command, really | Windows user-install path only; on other systems, start Docker yourself |

## Known limits

- Grafana's admin account keeps the default first-login password. It is reachable only from this laptop, and viewers need no login.
- The load ceiling (about 30 payments per second) comes from one API process. Running several API processes would need the shared Redis health (Phase 6) for routing.
- The CI workflow has not run on GitHub yet; it runs on the first push.
