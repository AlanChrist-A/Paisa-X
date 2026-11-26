"""
WHAT: runs real payment code against a simulated world on a virtual clock.
WHY: minutes of customer waiting can be tested in seconds, while still using real Postgres.
USES: the Paisa payment service, Recovery Worker, Gateway Simulator and money audit.
USED BY: tests/test_recovery.py. For the full run, from the project folder: uv run python -m experiments.phase3.soak
SAFE TO CHANGE: payment count, seed and traffic rate.
"""

import itertools  # Creates a repeatable ID counter.
import json  # Saves the final report.
import os  # Creates a unique temporary database name.
import time  # Measures how long the run takes.
import uuid  # Creates repeatable UUIDs.
from contextlib import contextmanager  # Builds the temporary-database helper.
from dataclasses import replace  # Copies a fault with new times.
from datetime import UTC, datetime, timedelta  # Implements the virtual clock.
from pathlib import Path  # Finds the results folder.

import psycopg  # Creates the temporary database.
from fastapi.testclient import TestClient  # Lets the HTTP client call the simulator in-process.
from psycopg.rows import dict_row  # Returns rows as dictionaries.
from psycopg_pool import ConnectionPool  # Reuses database connections.

from paisa.adapters import postgres  # Local database helpers.
from paisa.core.routing.policies import RollingPolicy  # The default routing policy.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.pipeline.audit import audit_records  # Money audit.
from paisa.services import payments, webhooks  # Payment flow and webhook storage.
from paisa.services.settlement import view  # Public payment view.
from paisa.workers.recovery import run_once  # One Recovery Worker round.
from simulator.app import create_app as create_simulator  # Gateway Simulator.
from simulator.engine import load_scenarios, make_traffic  # Fault plans and fake traffic.

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # Virtual time zero.
CYCLE_MS = 300_000  # The tuning faults span 5 minutes, so they repeat on this cycle.


class World:  # One simulated environment with a controllable clock.
    def __init__(self, database_url, merchant_id, seed, faults=(), catalog=None, router=None):  # Build it.
        self.ms, self.merchant_id, self.payment_id = 0, merchant_id, None  # Clock, owner and latest payment.
        app = create_simulator(seed=seed, clock=lambda: self.ms, use_latency=False)  # Simulator using our clock.
        self.sim = app.state.sim  # Direct access to its webhooks and truth.
        self.sim.faults = list(faults)  # Install the fault plan.
        self.sim.catalog = catalog or self.sim.catalog  # Optional custom market.
        self.http = TestClient(app, base_url="http://sim")  # Real HTTP client interface, with no real network.
        self.http.__enter__()  # Keep one background loop open, which is faster.
        self.pool = ConnectionPool(database_url, min_size=1, max_size=3, open=True, kwargs={"row_factory": dict_row})  # Database connections.
        counter = itertools.count()  # Counts generated IDs.
        self._real_id, payments.new_id = payments.new_id, lambda: uuid.uuid5(uuid.NAMESPACE_OID, f"{seed}:{next(counter)}")  # Repeatable IDs give repeatable gateway luck.
        self._real_router, payments.router = payments.router, router or payments.router  # Optional private router.

    def close(self):  # Releases resources.
        payments.new_id, payments.router = self._real_id, self._real_router  # Restore normal IDs and router.
        self.pool.close(), self.http.__exit__(None, None, None)  # Close connections.

    def clock(self):  # The current virtual time.
        return EPOCH + timedelta(milliseconds=self.ms)  # Convert it to a datetime.

    def pay(self, key="key-1", amount_paise=12_550, method="UPI", bank="BANK_A", network=None):  # Creates one payment.
        request = {"amount_paise": amount_paise, "method": method, "bank": bank, "network": network, "instrument_token": "tok_test_1"}  # Same shape as the API.
        _, body, _ = payments.create(self.pool, self.http, self.merchant_id, key, request, self.clock)  # Real payment flow.
        self.payment_id = uuid.UUID(body["id"])  # Remember it.
        return body  # Hand back the response.

    def drive(self):  # Starts the next attempt for the latest payment.
        payments.drive(self.pool, self.http, self.payment_id, self.clock)  # Real flow.

    def current(self):  # Reads the latest payment.
        with self.pool.connection() as conn:  # Open a connection.
            return view(conn, self.payment_id)  # Public view.

    def attempts(self):  # Reads the latest payment's attempts.
        return self.current()["attempts"]  # Just the attempts.

    def net(self):  # Real money taken for the latest payment.
        return self.sim.truth()["net_paise"].get(str(self.payment_id), 0)  # Zero if never charged.

    def advance(self, seconds):  # Moves the clock without running anything.
        self.ms += int(seconds * 1000)  # Add the time.

    def work(self):  # Delivers due webhooks, then runs one worker round.
        events = self.sim.due_webhooks(self.ms)  # Webhooks due now.
        if events:  # Save them all together.
            with self.pool.connection() as conn:  # One short transaction.
                for event in events:  # Each webhook.
                    webhooks.store(conn, event)  # Same storage as the API route.
        run_once(self.pool, self.http, self.clock)  # Real worker logic.

    def resolved(self):  # Has every payment and reversal finished?
        with self.pool.connection() as conn:  # Open a connection.
            return conn.execute("SELECT NOT EXISTS (SELECT 1 FROM payments WHERE state NOT IN ('SUCCEEDED', 'FAILED')) AND NOT EXISTS (SELECT 1 FROM reversal_tasks WHERE done_at IS NULL) AND NOT EXISTS (SELECT 1 FROM payment_attempts WHERE state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN')) AS done").fetchone()["done"]  # All clear?

    def settle(self, max_seconds=1_800):  # Advances time until everything resolves.
        for _ in range(max_seconds):  # At most this many virtual seconds.
            self.advance(1), self.work()  # One second, one round.
            if self.resolved():  # Finished.
                break  # Stop early.
        for _ in range(35):  # Let late webhooks arrive, so hidden late charges can be caught.
            self.advance(1), self.work()  # One second, one round.
        return self.current() if self.payment_id else None  # Latest payment.


def repeated_faults(until_ms):  # Repeats every tuning fault for the whole run.
    faults = [fault for group in load_scenarios("tuning").values() for fault in group]  # All tuning faults.
    return [replace(f, start_ms=f.start_ms + cycle * CYCLE_MS, end_ms=f.end_ms + cycle * CYCLE_MS) for cycle in range(until_ms // CYCLE_MS + 1) for f in faults]  # Shift each cycle.


def run_traffic(world, traffic, before_payment=None):  # Sends payments at their times, with one worker round per virtual second.
    next_round = 0  # Virtual time of the next worker round.
    for number, item in enumerate(traffic):  # Send each payment at its time.
        while next_round <= item["at_ms"]:  # Run all worker rounds due before it.
            world.ms = next_round  # Move the clock.
            world.work()  # Worker round.
            next_round += 1_000  # One round per virtual second.
        world.ms = item["at_ms"]  # Payment arrival time.
        if before_payment:  # Optional hook, so a drill can react to live traffic.
            before_payment(world)  # Run it.
        world.pay(f"soak-{number}", item["amount_paise"], item["method"], item["bank"], item["network"])  # Real flow.
    world.settle()  # Let everything finish.


def soak(database_url, merchant_id, payments=10_000, seed=7, per_second=5):  # Runs traffic with faults, then audits.
    traffic = make_traffic(seed, payments, per_second)  # Repeatable payments.
    world = World(database_url, merchant_id, seed, repeated_faults(traffic[-1]["at_ms"]), router=Router(RollingPolicy()))  # Build the world.
    try:  # Always release resources.
        run_traffic(world, traffic)  # Run everything.
        return audit_records(database_url, world.sim.truth())  # Compare with the hidden truth.
    finally:
        world.close()  # Clean up.


@contextmanager
def temporary_database(prefix):  # Creates a migrated, disposable database and always removes it.
    started_db = postgres.start()  # Start Postgres if needed.
    name = f"{prefix}_{os.getpid()}"  # Unique name.
    with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:  # Admin connection.
        admin.execute(f'CREATE DATABASE "{name}"')  # Create it.
        # This data is disposable, so skip waiting for disk on commit to keep the run fast.
        admin.execute(f'ALTER DATABASE "{name}" SET synchronous_commit = off')  # Applies to this database only.
    try:  # Always remove the temporary database.
        url = postgres.url(name)  # Its address.
        postgres.migrate(url)  # Build tables.
        yield url  # The caller runs here.
    finally:
        with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:  # Admin connection.
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')  # CAREFUL: only this temporary database.
        if started_db:  # Leave Postgres as we found it.
            postgres.stop()  # Free its memory.


def main():  # Runs the full 10,000-payment check in a temporary database.
    with temporary_database("paisa_soak") as url:  # Disposable database.
        merchant_id, _ = postgres.create_merchant(url)  # Test merchant.
        began = time.monotonic()  # Start timing.
        report = soak(url, merchant_id)  # Run it.
        report["runtime_seconds"] = round(time.monotonic() - began)  # Record the duration.
        out = Path(__file__).parent / "results"  # Results folder.
        out.mkdir(exist_ok=True)  # Create it if needed.
        (out / "soak_10000.json").write_text(json.dumps(report, indent=2))  # Save the report.
        print(json.dumps(report, indent=2))  # Show it.


if __name__ == "__main__":  # Run only when started directly.
    main()  # Start the soak.
