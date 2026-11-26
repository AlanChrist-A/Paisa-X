"""
WHAT: measures how fast routing leaves a dead gateway, and how long each routing decision takes.
WHY: Phase 4 must prove its targets with measured numbers.
USES: the real payment flow, Router, Gateway Simulator and Postgres.
USED BY: tests/test_router.py. Full run, from the project folder: uv run python -m experiments.phase4.drills
SAFE TO CHANGE: traffic, outage times and sample size.
"""

import json  # Saves the results.
import time  # High-precision timer.
from pathlib import Path  # Finds the results folder.

import numpy as np  # Calculates percentiles.
import psycopg  # Reads attempt history.

from experiments.phase3.soak import EPOCH, World, run_traffic, temporary_database  # Virtual-clock world.
from paisa.adapters import postgres  # Local database helpers.
from paisa.core.routing.policies import RollingPolicy  # Default policy.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.pipeline.audit import audit_records  # Money audit.
from simulator.engine import Fault, make_traffic  # Planned failures and fake traffic.


def reroute_seconds(rows, gateway, start_s, end_s, window=10, limit=0.1):  # Seconds until a gateway's share stays low.
    for second in range(start_s, end_s - window + 1):  # Slide a window forward.
        inside = [name for at, name in rows if second <= at < second + window]  # First attempts in this window.
        if inside and inside.count(gateway) / len(inside) < limit:  # Under 10% still goes there.
            return second - start_s  # Time since the outage started.
    return None  # It never moved.


def outage_drill(database_url, merchant_id, seed=11, seconds=90, start_s=20, end_s=80):  # Kills the busiest gateway mid-traffic.
    victim = []  # Filled with the gateway chosen at outage time.

    def kill_busiest(world):  # CAREFUL: killing a quiet gateway would make rerouting look instant.
        if victim or world.ms < start_s * 1000:  # Already done, or too early.
            return  # Nothing to do.
        with world.pool.connection() as conn:  # Read recent first choices.
            busiest = conn.execute("SELECT gateway FROM payment_attempts WHERE attempt_no = 1 GROUP BY gateway ORDER BY count(*) DESC, gateway LIMIT 1").fetchone()["gateway"]  # Highest traffic.
        victim.append(busiest)  # Remember it.
        world.sim.faults.append(Fault("outage", busiest, world.ms, end_s * 1000))  # Full outage from now.

    world = World(database_url, merchant_id, seed, router=Router(RollingPolicy()))  # Real flow.
    try:  # Always release resources.
        run_traffic(world, make_traffic(seed, seconds * 5, 5), kill_busiest)  # Five payments per second.
        report = audit_records(database_url, world.sim.truth())  # Check the money.
    finally:
        world.close()  # Clean up.
    with psycopg.connect(database_url) as conn:  # Read what the router really did.
        rows = conn.execute("SELECT created_at, gateway FROM payment_attempts WHERE attempt_no = 1").fetchall()  # First choices.
    timeline = [((at - EPOCH).total_seconds(), name) for at, name in rows]  # Seconds since the run began.
    before = [name for at, name in timeline if at < start_s]  # Traffic before the outage.
    return {"gateway_killed": victim[0], "share_before": round(before.count(victim[0]) / len(before), 3), "reroute_seconds": reroute_seconds(timeline, victim[0], start_s, end_s), "audit": report}  # Result.


def decision_latency(rounds=100_000):  # Measures Router.decide() on this laptop.
    router, options = Router(RollingPolicy()), ["alpha", "bravo", "delta", "echo"]  # A realistic router.
    for gateway in options:  # Fill its history.
        for number in range(100):  # A full rolling window.
            router.record(gateway, "UPI", number % 10 != 0, 0.0, 0.0)  # 90% healthy.
    samples = np.empty(rounds)  # Stores each timing.
    for number in range(rounds):  # Repeat many times.
        began = time.perf_counter_ns()  # Start the timer.
        router.decide(f"p{number}", "UPI", options, 1.0)  # The code being measured.
        samples[number] = time.perf_counter_ns() - began  # Save nanoseconds.
    return {f"p{p}_microseconds": round(float(np.percentile(samples, p)) / 1000, 1) for p in (50, 99)}  # Median and slow tail.


def main():  # Runs both measurements and saves them.
    results = {"decision_latency": decision_latency()}  # Fast, in-memory measurement.
    with temporary_database("paisa_drill") as url:  # Disposable database.
        merchant_id, _ = postgres.create_merchant(url)  # Test merchant.
        results["outage"] = outage_drill(url, merchant_id)  # Real payment flow.
    out = Path(__file__).parent / "results"  # Results folder.
    out.mkdir(exist_ok=True)  # Create it if needed.
    (out / "phase4.json").write_text(json.dumps(results, indent=2))  # Save the report.
    print(json.dumps(results, indent=2))  # Show it.


if __name__ == "__main__":  # Run only when started directly.
    main()  # Start.
