"""
WHAT: Phase 6 drills with real Kafka and Redis in Docker.
WHY: proves (1) streaming health in Redis matches a batch recount from Parquet, with every
     difference explained by late events, (2) a sink crash mid-write corrupts nothing,
     (3) wiping Redis does not stop routing, and (4) Redis rebuilds from Kafka to the same numbers.
RUN: docker compose up -d, then uv run python -m experiments.phase6.drills
CAREFUL: this ERASES the local Kafka topics and Redis data first, so the comparison is exact.
"""

import json  # Saves results.
import os  # Passes settings to the crashing sink.
import shutil  # Removes the temporary data folder.
import subprocess  # Starts a sink that crashes, and reads Docker stats.
import sys  # Path to this Python.
import tempfile  # Temporary data folder.
from pathlib import Path  # Results folder.

from experiments.phase3.soak import World, repeated_faults, run_traffic, temporary_database  # Phase 3 harness.
from experiments.phase5.drills import drain  # Relay helper.
from paisa.adapters import kafka, postgres, redis_store  # Helpers.
from paisa.core.routing.policies import RollingPolicy  # Default policy.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.pipeline.lake import build  # Silver and gold.
from paisa.workers import features, parquet_sink  # Workers.
from simulator.engine import make_traffic  # Repeatable traffic.


def compare(streaming, batch, late):  # Streaming may miss only late events, never add or lose others.
    over = sum(max(0, s - b) for k in streaming.keys() | batch.keys() for s, b in zip(streaming.get(k, (0, 0)), batch.get(k, (0, 0))))  # Streaming above batch: must be 0.
    under = sum(max(0, b - s) for k in streaming.keys() | batch.keys() for s, b in zip(streaming.get(k, (0, 0)), batch.get(k, (0, 0))))  # Batch above streaming.
    return {"signals_batch": sum(a + b for a, b in batch.values()), "streaming_above_batch": over, "batch_above_streaming": under, "late_events": late, "all_differences_are_late_events": over == 0 and under == late}


def main():
    kafka.reset_topics()  # CAREFUL: empty topics, so only this run's events exist.
    r = redis_store.client()
    r.flushdb()  # CAREFUL: local Redis only.
    data = Path(tempfile.mkdtemp(prefix="paisa_lake_"))  # Temporary lake.
    out = {}  # Results.
    try:
        with temporary_database("paisa_phase6") as url:
            merchant, _ = postgres.create_merchant(url)
            traffic = make_traffic(21, 2_000, 5)  # 2,000 payments.
            world = World(url, merchant, 21, repeated_faults(traffic[-1]["at_ms"]), router=Router(RollingPolicy()))  # With gateway faults.
            try:
                run_traffic(world, traffic)
            finally:
                world.close()
            out["relayed"] = drain(url)  # Postgres -> Kafka.
            out["feature_worker"] = features.run(group="drill-features", idle_polls=5, r=r)  # Kafka -> Redis.
            crashed = subprocess.run([sys.executable, "-c", "from paisa.workers.parquet_sink import run; run(group='drill-sink', idle_polls=5)"], env={**os.environ, "PAISA_CRASH_AT": "sink_before_rename", "PAISA_DATA_DIR": str(data)})
            out["sink_crash_exit_code"] = crashed.returncode  # 137 = died mid-write on purpose.
            out["tmp_files_left_by_crash"] = len(list(data.rglob("*.tmp")))  # Half-written files.
            out["sink_files_written"] = parquet_sink.run(data, group="drill-sink", idle_polls=5)  # Restarted sink redoes the batch.
            lake = build(data, url)  # Bronze -> silver -> gold.
            out["lake"] = {k: lake[k] for k in ("bronze_rows", "bronze_duplicates", "silver_payments", "silver_attempts", "violations")}
            late = int(r.hget("features:stats", "late") or 0)  # Events too late to count live.
            streaming = redis_store.all_health(r)  # Live numbers.
            out["streaming_vs_batch"] = compare(streaming, lake["gold_health"], late)
            r.flushdb()  # Drill: Redis wiped.
            router = Router(RollingPolicy())  # A router whose snapshot is now empty.
            router.shared = {}
            out["routing_works_with_redis_empty"] = router.decide("p", "UPI", ["alpha", "beta"], 0).chosen in ("alpha", "beta")
            out["rebuild"] = features.run(group="drill-features", rebuild=True, idle_polls=5, r=r)  # Replay Kafka from the start.
            rebuilt = redis_store.all_health(r)
            out["rebuilt_vs_batch"] = compare(rebuilt, lake["gold_health"], int(r.hget("features:stats", "late") or 0))
    finally:
        shutil.rmtree(data, ignore_errors=True)  # Remove the temporary lake.
    out["ram"] = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", "paisa-kafka", "paisa-redis"], capture_output=True, text=True).stdout.strip().splitlines()
    folder = Path(__file__).parent / "results"
    folder.mkdir(exist_ok=True)
    (folder / "phase6.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":  # Run only when started directly.
    main()
