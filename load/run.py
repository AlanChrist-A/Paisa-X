"""
WHAT: runs the k6 load test in Docker against the running API, then writes docs/results/load-report.md.
WHY: the report must show where latency starts to degrade on this laptop.
RUN: uv run paisa up, then uv run python load/run.py   (about 4 minutes)
USES: Docker image grafana/k6, load/payments.js, and a new test merchant.
"""

import json  # Reads the k6 summary.
import re  # Finds the step rates in payments.js.
import subprocess  # Runs k6 in Docker.
import time  # Report date.
from pathlib import Path  # File paths.

from paisa.adapters import postgres  # Creates the test merchant.

HERE = Path(__file__).resolve().parent  # The load/ folder.
REPORT = HERE.parent / "docs" / "results" / "load-report.md"  # Where the report goes.
K6_IMAGE = "grafana/k6:0.55.0"  # Pinned, so every run is the same.
SLOWER = 2.0  # CHANGE ME: "degraded" means p95 latency is this many times the first step's p95...
ERRORS = 0.01  # CHANGE ME: ...or more than 1% of requests fail or are dropped.


def main():
    _, api_key = postgres.create_merchant()  # A fresh key for this run.
    (HERE / "out").mkdir(exist_ok=True)  # k6 writes its summary here.
    subprocess.run(["docker", "run", "--rm", "-v", f"{HERE}:/load", "-e", "BASE_URL=http://host.docker.internal:8000", "-e", f"API_KEY={api_key}",
                    K6_IMAGE, "run", "--quiet", "--summary-export=/load/out/summary.json", "/load/payments.js"], check=True)
    metrics = json.loads((HERE / "out" / "summary.json").read_text())["metrics"]
    rates = [int(r) for r in re.search(r"RATES = \[([^\]]+)\]", (HERE / "payments.js").read_text()).group(1).split(",")]
    rows, knee, base = [], None, None
    for rate in rates:
        tag = f"{{scenario:r{rate}}}"
        latency = metrics[f"http_req_duration{tag}"]
        sent = metrics[f"http_reqs{tag}"]["count"]
        dropped = metrics.get(f"dropped_iterations{tag}", {}).get("count", 0)
        bad = (metrics[f"http_req_failed{tag}"]["value"] * sent + dropped) / max(sent + dropped, 1)  # Failed or never sent.
        base = base or latency["p(95)"]
        degraded = latency["p(95)"] > SLOWER * base or bad > ERRORS
        knee = knee or (rate if degraded else None)
        rows.append(f"| {rate} | {sent / 30:.1f} | {latency['med']:.0f} | {latency['p(95)']:.0f} | {latency['p(99)']:.0f} | {100 * bad:.2f}% | {'**degraded**' if degraded else 'ok'} |")
    verdict = f"Latency starts to degrade at **{knee} payments per second**." if knee else f"No step degraded, up to {rates[-1]} payments per second."
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join([
        "# Load test report", "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `uv run python load/run.py` on this laptop, with the full stack running (`uv run paisa up`).",
        f"k6 sends new payments at a fixed rate for 30 seconds per step. A step is degraded when p95 latency is over {SLOWER:g}x the first step's, or over {ERRORS:.0%} of payments fail or are dropped.", "",
        verdict, "",
        "| Target rate (per s) | Achieved (per s) | Median ms | p95 ms | p99 ms | Failed or dropped | Step |",
        "|---:|---:|---:|---:|---:|---:|---|", *rows, "",
        "Each payment includes a real gateway call to the simulator (about 200-300 ms of simulated gateway delay), three database transactions and an outbox event.",
    ]) + "\n", encoding="utf-8")
    print(REPORT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
