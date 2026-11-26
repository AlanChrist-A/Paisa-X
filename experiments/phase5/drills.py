"""
WHAT: Phase 5 drills with real Kafka in Docker.
WHY: proves (1) payments continue while Kafka is stopped, (2) events catch up afterwards,
     (3) a Kafka restart and (4) a relay crash after sending lose nothing,
     (5) each payment's events stay in order, and records real RAM use.
RUN: docker compose up -d, then uv run python -m experiments.phase5.drills
"""

import json  # Saves results.
import os  # Passes settings to the crashing relay.
import subprocess  # Controls Docker and starts a relay that crashes.
import sys  # Path to this Python.
import time  # Measures catch-up time.
from pathlib import Path  # Results folder.

import psycopg  # Reads counts.
from psycopg.rows import dict_row  # Rows as dictionaries.
from psycopg_pool import ConnectionPool  # Relay connections.

from experiments.phase3.soak import World, run_traffic, temporary_database  # Phase 3 harness.
from paisa.adapters import kafka, postgres  # Helpers.
from paisa.pipeline.event_check import check  # Kafka vs outbox.
from paisa.workers.relay import KafkaUnavailable, relay_once  # The relay.
from simulator.engine import make_traffic  # Repeatable traffic.


def docker(*args):  # Runs one docker command and returns its output.
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def drain(url, seconds=120):  # Relays until the outbox is empty. Returns True if it emptied in time.
    end = time.monotonic() + seconds  # Give up after this.
    producer = kafka.producer()  # Fresh producer.
    with ConnectionPool(url, min_size=1, max_size=1, kwargs={"row_factory": dict_row}) as pool:  # One connection.
        while time.monotonic() < end:  # Keep trying.
            try:
                if relay_once(pool, producer) == 0:  # Nothing left to send.
                    return True
            except KafkaUnavailable:  # Kafka still starting. Try again.
                time.sleep(1)
    return False  # Did not finish in time.


def more_payments(world, seed, count):  # Sends extra payments after the current virtual time.
    for n, t in enumerate(make_traffic(seed, count, 5)):  # Repeatable payments.
        world.ms += 200  # 5 per virtual second.
        world.pay(f"extra-{seed}-{n}", t["amount_paise"], t["method"], t["bank"], t["network"])  # Unique key.
    world.settle()  # Let them finish.


def waiting(url):  # Events still unpublished.
    with psycopg.connect(url) as c:
        return c.execute("SELECT count(*) FROM outbox WHERE published_at IS NULL").fetchone()[0]


def main():
    kafka.ensure_topics()  # Topics must exist.
    r = {}  # Results.
    with temporary_database("paisa_phase5") as url:  # Disposable database.
        merchant, _ = postgres.create_merchant(url)  # Test merchant.
        world = World(url, merchant, seed=11)  # In-process gateways, virtual clock.
        try:
            docker("stop", "paisa-kafka")  # Drill 1: Kafka is down for the whole run.
            run_traffic(world, make_traffic(11, 1_000, 5))  # 1,000 payments. They never touch Kafka.
            with psycopg.connect(url) as c:  # How did those payments end?
                r["payments_while_kafka_down"] = dict(c.execute("SELECT state, count(*) FROM payments GROUP BY state").fetchall())
            r["events_waiting_while_down"] = waiting(url)  # Safely stored in Postgres.
            docker("start", "paisa-kafka")  # Drill 2: Kafka is back.
            began = time.monotonic()  # Time the catch-up.
            r["caught_up_after_start"] = drain(url)
            r["catch_up_seconds"] = round(time.monotonic() - began, 1)
            more_payments(world, 12, 300)  # Drill 3: new events wait while Kafka restarts.
            docker("restart", "paisa-kafka")
            r["caught_up_after_restart"] = drain(url)
            more_payments(world, 13, 200)  # Drill 4: a relay crashes after sending, before marking.
            crashed = subprocess.run([sys.executable, "-c", "from paisa.workers.relay import main; main()"], env={**os.environ, "PAISA_CRASH_AT": "relay_after_send", "DATABASE_URL": url})
            r["relay_crash_exit_code"] = crashed.returncode  # 137 means it died at the crash point.
            r["unmarked_after_crash"] = waiting(url)  # Sent, but not marked.
            r["caught_up_after_crash"] = drain(url)  # A new relay sends them again.
        finally:
            world.close()
        r["event_check"] = check(url)  # missing and out_of_order must be 0; duplicates are expected.
    r["kafka_ram"] = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", "paisa-kafka")  # Real RAM use.
    out = Path(__file__).parent / "results"  # Results folder.
    out.mkdir(exist_ok=True)
    (out / "phase5.json").write_text(json.dumps(r, indent=2))  # Save.
    print(json.dumps(r, indent=2))  # Show.


if __name__ == "__main__":  # Run only when started directly.
    main()
