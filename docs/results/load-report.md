# Load test report

Generated 2026-11-26 18:16 by `uv run python load/run.py` on this laptop, with the full stack running (`uv run paisa up`).
k6 sends new payments at a fixed rate for 30 seconds per step. A step is degraded when p95 latency is over 2x the first step's, or over 1% of payments fail or are dropped.

Latency starts to degrade at **40 payments per second**.

| Target rate (per s) | Achieved (per s) | Median ms | p95 ms | p99 ms | Failed or dropped | Step |
|---:|---:|---:|---:|---:|---:|---|
| 5 | 5.0 | 339 | 467 | 702 | 0.00% | ok |
| 10 | 10.0 | 345 | 629 | 770 | 0.00% | ok |
| 20 | 20.0 | 391 | 798 | 935 | 0.00% | ok |
| 40 | 33.6 | 5088 | 11258 | 12057 | 15.99% | **degraded** |
| 60 | 25.0 | 18187 | 21198 | 22479 | 58.44% | **degraded** |
| 80 | 23.0 | 19570 | 25231 | 28413 | 71.89% | **degraded** |
| 120 | 21.9 | 20447 | 28593 | 29990 | 83.11% | **degraded** |

Each payment includes a real gateway call to the simulator (about 200-300 ms of simulated gateway delay), three database transactions and an outbox event.
