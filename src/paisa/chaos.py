"""
WHAT: `uv run paisa chaos` runs every failure drill, audits the money after each one, and exits with
      an error if any rule broke. About 15 minutes.
WHY: one command must prove the system is still safe after any change.
USES: the drills in experiments/ (Phases 3-7). Each one builds a disposable database, injects
      failures, then audits every paisa against the simulator's hidden truth.
CAREFUL: it stops the apps started by `paisa up` (they would mix into the drills), restarts Kafka,
         and ERASES the local Kafka topics and Redis data. Postgres data in the main database is untouched.
SAFE TO CHANGE: add a drill with one more entry in DRILLS.
"""

import json  # Reads drill results and saves the summary.
import subprocess  # Starts Kafka and Redis.
import sys  # Exit code, and the import path for experiments/.
import time  # Run time.

from paisa import stack  # Stops running apps; waiting helpers.
from paisa.adapters import postgres  # Local database.

CHAOS_PAYMENTS = 2_000  # CHANGE ME: payments in the all-faults soak. Phase 3 used 10,000 (about 19 minutes).
RESULTS = postgres.ROOT / "docs" / "results" / "chaos.json"  # The summary of the last run.


def audit_clean(report):  # Money rules: nothing unresolved, no violations, no reversals left.
    return report["unresolved"] == 0 and not any(report["violations"].values()) and report["pending_reversals"] == 0


def saved(phase, name):  # Reads a result file a drill wrote.
    return json.loads((postgres.ROOT / "experiments" / phase / "results" / name).read_text())


def soak_drill():  # Every tuning fault at once, repeated for the whole run.
    from experiments.phase3.soak import soak, temporary_database
    with temporary_database("paisa_chaos") as url:
        return soak(url, postgres.create_merchant(url)[0], payments=CHAOS_PAYMENTS)


def outage_drill():  # The busiest gateway dies mid-traffic.
    from experiments.phase3.soak import temporary_database
    from experiments.phase4.drills import outage_drill as drill
    with temporary_database("paisa_chaos") as url:
        return drill(url, postgres.create_merchant(url)[0])


def run_main(module, phase, name):  # Runs a drill's main() and returns what it saved.
    __import__(module, fromlist=["main"]).main()
    return saved(phase, name)


def kafka_ok(r):  # Kafka down, restarted, relay crashed: nothing lost, nothing out of order.
    c = r["event_check"]
    return r["caught_up_after_start"] and r["caught_up_after_restart"] and r["caught_up_after_crash"] and c["missing"] == 0 and c["out_of_order"] == 0 and c["unpublished"] == 0


def redis_ok(r):  # Redis wiped, sink crashed: features still match the batch recount, routing still works.
    return r["streaming_vs_batch"]["all_differences_are_late_events"] and r["rebuilt_vs_batch"]["all_differences_are_late_events"] and r["routing_works_with_redis_empty"] and not any(r["lake"]["violations"].values())


DRILLS = [  # (name, run it, did it pass?)
    ("all faults soak", soak_drill, audit_clean),
    ("gateway outage", outage_drill, lambda r: audit_clean(r["audit"])),
    ("learning router outage", lambda: run_main("experiments.phase7.live_drill", "phase7", "phase7_live.json"), lambda r: audit_clean(r["audit"])),
    ("Kafka stop, restart and relay crash", lambda: run_main("experiments.phase5.drills", "phase5", "phase5.json"), kafka_ok),
    ("Redis wipe and sink crash", lambda: run_main("experiments.phase6.drills", "phase6", "phase6.json"), redis_ok),
]


def main():  # `paisa chaos`
    sys.path.insert(0, str(postgres.ROOT))  # The drills live in experiments/, outside the installed package.
    stack.stop_apps()  # CAREFUL: a running relay or feature worker would mix its work into the drills.
    if not stack.docker_ready():
        raise SystemExit("Docker is not running. Start Docker Desktop, or run `uv run paisa up` once.")
    subprocess.run(["docker", "compose", "up", "-d", "kafka", "redis"], cwd=postgres.ROOT, check=True)  # Drills need both.
    postgres.start()
    stack.wait_for(stack.kafka_ready, "Kafka")
    summary, started = {}, time.time()
    for name, run, passed in DRILLS:
        print(f"\n=== {name} ===", flush=True)
        try:
            ok = bool(passed(run()))
        except Exception as error:  # A crashed drill is a failed drill.
            print(f"drill crashed: {type(error).__name__}: {error}", flush=True)
            ok = False
        summary[name] = "PASS" if ok else "FAIL"
    summary["minutes"] = round((time.time() - started) / 60, 1)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(summary, indent=2))
    print("\n" + json.dumps(summary, indent=2))
    sys.exit(0 if all(v != "FAIL" for v in summary.values()) else 1)  # CAREFUL: exit 1 is what CI and scripts check.
