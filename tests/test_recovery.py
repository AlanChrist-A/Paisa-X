"""
WHAT: Phase 3 tests for unknown results, webhooks, failover, reversals and worker crashes.
WHY: these are the cases where real payment systems lose track of money.
USES: real Postgres, the simulator on a virtual clock, and real processes for crash tests.
SAFE TO CHANGE: add a test for every new failure you discover.
"""

import copy  # Copies the market settings for each test.
import json  # Builds raw webhook bodies.
import os  # Passes settings to real worker processes.
import subprocess  # Starts real worker processes.
import sys  # Finds the current Python program.
import time  # Waits for real processes and signs webhooks.
import uuid  # Creates IDs for direct database setup.

import httpx  # Makes real HTTP calls.
import psycopg  # Opens separate database connections.
import pytest  # Test framework.
from fastapi.testclient import TestClient  # Calls the API in-process.

from experiments.phase3.soak import World, soak  # Virtual-clock test world and soak run.
from paisa.api.app import create_app  # API under test.
from paisa.pipeline.audit import run_audit  # Money audit.
from paisa.services import webhooks  # Stores webhooks in the inbox.
from paisa.services.settlement import apply_result  # The single result-handling path.
from paisa.workers import recovery  # Recovery Worker.
from simulator.app import sign  # The simulator's webhook signature.
from simulator.engine import Fault, load_catalog  # Faults and market settings.
from tests.test_payment_integration import free_port, launch, send, start_api  # Real-process helpers.

FOREVER = 10**9  # A fault end time far beyond any test.


def calm(**method):  # A predictable market where everything works unless a test changes it.
    catalog = copy.deepcopy(load_catalog())  # Private copy.
    catalog["sim"]["in_flight_ms"] = 60_000  # Delayed requests take long enough for a void to arrive first.
    for item in catalog["methods"].values():  # Simplify customer behaviour.
        item.update(business_decline=0, abandon=0, customer_ms=[100, 100], **method)  # Tests can override.
    for gateway in catalog["gateways"].values():  # Remove random technical failures.
        gateway["success"] = {name: 1.0 for name in gateway["success"]}  # Always healthy.
    return catalog  # Ready.


@pytest.fixture
def world(database_url, db, merchant):  # Builds virtual-clock worlds and closes them afterwards.
    built = []  # Tracks open worlds.
    def build(*faults, catalog=None):  # Creates one world.
        built.append(World(database_url, merchant["id"], 1, faults, catalog or calm()))  # Start it.
        return built[-1]  # Hand it to the test.
    yield build  # The test runs here.
    for item in built:  # Clean up.
        item.close()  # Release connections.


def fault(kind, gateway="alpha", value=1.0):  # A fault active for the whole test.
    return Fault(kind, gateway, 0, FOREVER, value)  # Build it.


def test_timeout_after_charge_is_recorded_once(world):  # The classic dangerous case.
    w = world(fault("timeout_after_charge"))  # Every reply is lost after charging.
    payment = w.pay()  # Caller sees an unclear result.
    assert payment["state"] == "PENDING_CONFIRMATION"  # We do not guess.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # Recovery finds the charge.
    assert [a["gateway"] for a in w.attempts()] == ["alpha"]  # CAREFUL: no second gateway was charged.


def test_not_found_is_voided_before_failover(world):  # "Not found" alone is not proof.
    w = world(fault("timeout_in_flight"))  # Alpha's request is stuck on the network.
    w.pay()  # Start the payment.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # It completes exactly once.
    assert [(a["gateway"], a["state"]) for a in w.attempts()] == [("alpha", "FAILED_SAFE"), ("bravo", "SUCCEEDED")]  # Voided, then failover.


def test_lying_void_blocks_failover(world):  # A gateway claims a void worked, but it did not.
    w = world(fault("timeout_in_flight"), fault("void_bug"))  # Both faults on alpha.
    w.pay()  # Start the payment.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # The late charge is found.
    assert [a["gateway"] for a in w.attempts()] == ["alpha"]  # Failover never started.


def test_abandoned_customer_fails_without_failover(world):  # After a prompt, moving gateways is unsafe.
    catalog = calm()  # Start from a calm market.
    catalog["methods"]["UPI"]["abandon"] = 1  # Every customer walks away.
    w = world(catalog=catalog)  # Build the world.
    w.pay()  # Start the payment.
    final = w.settle()  # Wait for the void.
    assert final["state"] == "FAILED" and final["can_retry_with_customer"] is True and w.net() == 0  # Clear outcome.
    assert len(w.attempts()) == 1  # No automatic second attempt.


def test_business_decline_never_fails_over(world):  # Low balance is not a gateway problem.
    catalog = calm()  # Start from a calm market.
    catalog["methods"]["UPI"]["business_decline"] = 1  # Every customer is declined.
    w = world(catalog=catalog)  # Build the world.
    w.pay()  # Start the payment.
    assert w.settle()["state"] == "FAILED" and len(w.attempts()) == 1 and w.net() == 0  # One try only.


def test_safe_failure_fails_over_immediately(world):  # Connection-refused style failure.
    w = world(fault("outage"))  # Alpha is down.
    payment = w.pay()  # The API moves to the next gateway.
    assert [a["gateway"] for a in payment["attempts"]] == ["alpha", "bravo"]  # Failover happened.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # One charge.


def test_failover_stops_at_three_attempts(world):  # Limits repeated customer impact.
    w = world(*(fault("outage", name) for name in ("alpha", "bravo", "delta", "echo")))  # All UPI gateways down.
    assert len(w.pay()["attempts"]) == 3 and w.settle()["state"] == "FAILED"  # Three tries, then stop.


@pytest.mark.parametrize("kind", ["webhook_lost", "webhook_duplicate", "webhook_late", "webhook_reorder"])
def test_webhook_faults_end_correctly(world, kind):  # Bad webhooks must not corrupt state.
    w = world(fault(kind))  # Apply one webhook fault.
    w.pay()  # Start the payment.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # Correct final state.


def force_failed(w):  # Simulates our records wrongly believing no money moved.
    attempt_id = w.attempts()[0]["id"]  # The charged attempt.
    with w.pool.connection() as conn:  # Change records directly.
        conn.execute("UPDATE payment_attempts SET state = 'FAILED_SAFE' WHERE id = %s", (attempt_id,))  # Wrong belief.
        conn.execute("UPDATE payments SET state = 'FAILED' WHERE id = %s", (w.payment_id,))  # Payment ended.
    return attempt_id  # Hand it back.


def test_late_success_after_failure_is_reversed(world):  # A failed payment must not keep money.
    w = world(fault("timeout_after_charge"))  # The gateway charges silently.
    w.pay()  # Start the payment.
    attempt_id = force_failed(w)  # Records now say failed.
    with w.pool.connection() as conn:  # A late success arrives.
        apply_result(conn, attempt_id, "SUCCEEDED", w.clock())  # Use the normal path.
    assert w.settle()["state"] == "FAILED" and w.net() == 0  # Final state unchanged, money returned.
    assert w.attempts()[0]["state"] == "REVERSED"  # History shows what happened.


def test_old_attempt_winning_stops_newer_attempt(world):  # Two attempts must never both keep money.
    w = world(fault("timeout_after_charge"))  # Alpha charges silently.
    w.pay()  # Attempt 1 is unknown.
    attempt_id = w.attempts()[0]["id"]  # Keep its ID.
    with w.pool.connection() as conn:  # Simulate a wrong safe-failure record.
        conn.execute("UPDATE payment_attempts SET state = 'FAILED_SAFE' WHERE id = %s", (attempt_id,))  # Allows attempt 2.
        conn.execute("UPDATE payments SET state = 'PROCESSING' WHERE id = %s", (w.payment_id,))  # Payment continues.
    w.sim.faults.clear()  # Attempt 2 should work normally.
    w.drive()  # Start attempt 2.
    with w.pool.connection() as conn:  # Attempt 1's late success arrives.
        apply_result(conn, attempt_id, "SUCCEEDED", w.clock())  # Attempt 1 wins.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # Customer paid once.
    assert sum(a["state"] == "SUCCEEDED" for a in w.attempts()) == 1  # Only one charge remains.


def direct_payment(w, state):  # Creates an interrupted payment and its key.
    payment_id = uuid.uuid4()  # New ID.
    with w.pool.connection() as conn:  # Direct setup.
        conn.execute("INSERT INTO payments (id, merchant_id, amount_paise, method, instrument_token, bank_code, state, deadline_at, created_at, updated_at) VALUES (%s, %s, 500, 'UPI', 'tok_test', 'BANK_A', %s, %s, %s, %s)", (payment_id, w.merchant_id, state, w.clock() + recovery.timedelta(minutes=15), w.clock(), w.clock()))
        conn.execute("INSERT INTO idempotency_keys (merchant_id, key, request_hash, payment_id, expires_at) VALUES (%s, %s, 'x', %s, %s)", (w.merchant_id, str(payment_id), payment_id, w.clock()))
    w.payment_id = payment_id  # Track it.
    return payment_id  # Hand it back.


def test_stale_created_payment_fails_and_saves_response(world, db):  # Crash before any gateway call.
    w = world()  # Calm world.
    payment_id = direct_payment(w, "CREATED")  # Interrupted payment.
    assert w.settle()["state"] == "FAILED"  # Safe, because no gateway was called.
    assert db.execute("SELECT response_code FROM idempotency_keys WHERE payment_id = %s", (payment_id,)).fetchone()["response_code"] == 201  # Retry now gets an answer.


def test_stranded_processing_payment_resumes(world):  # Crash between failover steps.
    w = world()  # Calm world.
    direct_payment(w, "PROCESSING")  # No active attempt exists.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 500  # The worker continues it.


def test_failed_webhook_starts_next_gateway_immediately(world):  # Regression: found by the 10,000-payment soak.
    w = world(fault("timeout_in_flight"))  # Alpha's result is unclear.
    w.pay()  # Attempt 1 is unknown.
    event = {"event_id": "evt-failed", "gateway": "alpha", "attempt_id": w.attempts()[0]["id"], "payment_id": str(w.payment_id), "status": "FAILED", "occurred_at_ms": w.ms}  # Proof that no money moved.
    with w.pool.connection() as conn:  # Store it like the API route does.
        webhooks.store(conn, event)  # Put it in the inbox.
    w.work()  # One worker round, with no time passing.
    assert [a["gateway"] for a in w.attempts()] == ["alpha", "bravo"]  # Failover started right away.


def test_rescue_finds_payment_stopped_after_safe_failure(world):  # Regression: rescue must use the same clock.
    w = world(fault("timeout_in_flight"))  # Alpha's result is unclear.
    w.pay()  # Attempt 1 is unknown.
    attempt_id = w.attempts()[0]["id"]  # Keep its ID.
    w.sim.void("alpha", attempt_id, w.ms)  # Prove no money can move on this attempt.
    with w.pool.connection() as conn:  # Simulate a crash right after saving the safe failure.
        assert apply_result(conn, attempt_id, "FAILED_SAFE", w.clock()) == "PROCESSING"  # Ready for failover, but not started.
    assert w.settle()["state"] == "SUCCEEDED" and w.net() == 12_550  # The rescue continues it.


def test_workers_skip_rows_claimed_by_another_worker(world, database_url):  # SKIP LOCKED proof.
    w = world(fault("timeout_in_flight"))  # Both payments become unknown.
    w.pay("k1")  # First payment.
    first = w.attempts()[0]["id"]  # Its attempt.
    w.pay("k2")  # Second payment.
    second = w.attempts()[0]["id"]  # Its attempt.
    w.advance(30)  # Make both due without running the worker.
    with psycopg.connect(database_url) as other:  # Another worker holds attempt 1.
        other.execute("SELECT 1 FROM payment_attempts WHERE id = %s FOR UPDATE", (first,))  # Lock it.
        assert [str(row["id"]) for row in recovery.claim_attempts(w.pool, w.clock, 10)] == [second]  # Only the free row.


def signed(secret, event, age=0):  # Builds signed webhook headers and body.
    raw = json.dumps(event).encode()  # Exact bytes.
    stamp = str(int(time.time()) - age)  # Possibly old timestamp.
    return raw, {"X-Sim-Timestamp": stamp, "X-Sim-Signature": sign(secret, stamp, raw), "Content-Type": "application/json"}


def test_webhook_route_security_and_duplicates(database_url, db, merchant):  # Only genuine webhooks may enter.
    event = {"event_id": "evt1", "gateway": "alpha", "attempt_id": str(uuid.uuid4()), "payment_id": "p", "status": "SUCCEEDED", "occurred_at_ms": 1}  # Valid event.
    with TestClient(create_app(database_url, httpx.Client(base_url="http://sim"), webhook_secret="right")) as client:  # API with a secret.
        raw, headers = signed("right", event)  # Correctly signed.
        assert client.post("/v1/webhooks/alpha", content=raw, headers=headers).status_code == 200  # Accepted.
        assert client.post("/v1/webhooks/alpha", content=raw, headers=headers).status_code == 200  # Duplicate is harmless.
        assert db.execute("SELECT count(*) AS n FROM webhook_inbox").fetchone()["n"] == 1  # Stored once.
        assert client.post("/v1/webhooks/alpha", content=raw, headers={**headers, "X-Sim-Signature": "bad"}).status_code == 401  # Forged.
        assert client.post("/v1/webhooks/alpha", content=raw + b" ", headers=headers).status_code == 401  # Body changed.
        old, old_headers = signed("right", event, age=3_600)  # Replay attack.
        assert client.post("/v1/webhooks/alpha", content=old, headers=old_headers).status_code == 401  # Too old.
        assert client.post("/v1/webhooks/bravo", content=raw, headers=headers).status_code == 400  # Wrong gateway.
    with TestClient(create_app(database_url, httpx.Client(base_url="http://sim"))) as client:  # No secret configured.
        raw, headers = signed("anything", event)  # Any signature.
        assert client.post("/v1/webhooks/alpha", content=raw, headers=headers).status_code == 503  # Fail closed.


def start_worker(database_url, simulator, **env):  # Starts one real Recovery Worker.
    args = [sys.executable, "-c", "from paisa.cli import main; main()", "worker"]  # Same command as `paisa worker`.
    return subprocess.Popen(args, env={**os.environ, "PYTHONPATH": os.pathsep.join([".", "src"]), "DATABASE_URL": database_url, "SIMULATOR_URL": simulator, "PAISA_WORKER_SLEEP": "0.1", "PAISA_ROUTING_POLICY": "static", **env})


def wait_until(check, seconds=45):  # Polls until a condition is true.
    deadline = time.monotonic() + seconds  # Stop time.
    while time.monotonic() < deadline:  # Keep trying.
        if check():  # Finished.
            return True  # Success.
        time.sleep(0.2)  # Short pause.
    return False  # Timed out.


@pytest.mark.parametrize("point", ["worker_after_claim", "worker_after_status", "worker_after_void", "worker_after_apply", "worker_in_webhook", "worker_after_reverse"])
def test_worker_killed_at_any_point_recovers(database_url, db, merchant, simulator, point):  # Phase 3 crash proof.
    fault_kind = "timeout_after_charge" if point == "worker_after_reverse" else "timeout_in_flight"  # Choose the setup.
    httpx.post(f"{simulator}/sim/admin/faults", json={"kind": fault_kind, "gateway": "alpha", "duration_ms": 600_000}).raise_for_status()  # Apply it.
    process, base = start_api(database_url, simulator, PAISA_WEBHOOK_SECRET="test-secret")  # Real API.
    worker = None  # Set when started.
    try:  # Always stop processes.
        body = {"amount_paise": 12_550, "method": "UPI", "bank": "BANK_A", "instrument_token": "tok_test_1"}  # Valid payment.
        if point == "worker_after_reverse":  # Need a real charge that our records treat as failed.
            for number in range(40):  # Some customers decline or abandon, so try until money moves.
                reply = send(base, merchant, f"k{number}", body).json()  # Create a payment.
                if str(reply["id"]) in httpx.get(f"{simulator}/sim/admin/truth").json()["net_paise"]:  # It charged.
                    break  # Use this one.
            attempt_id = reply["attempts"][0]["id"]  # The charged attempt.
            db.execute("UPDATE payment_attempts SET state = 'FAILED_SAFE' WHERE state IN ('UNKNOWN', 'AWAITING_CUSTOMER')")  # Stop other checks.
            db.execute("UPDATE payments SET state = 'FAILED' WHERE state NOT IN ('SUCCEEDED', 'FAILED')")  # Pretend they failed.
            with psycopg.connect(database_url, row_factory=psycopg.rows.dict_row) as conn:  # Late success arrives.
                apply_result(conn, attempt_id, "SUCCEEDED", recovery.utc_now())  # Creates the reversal task.
            done = lambda: db.execute("SELECT count(*) AS n FROM reversal_tasks WHERE done_at IS NULL").fetchone()["n"] == 0  # Finished check.
        else:  # An unknown attempt that is old enough to void immediately.
            attempt_id = send(base, merchant, "k1", body).json()["attempts"][0]["id"]  # Create it.
            db.execute("UPDATE payment_attempts SET created_at = now() - interval '1 hour', next_check_at = now() WHERE id = %s", (attempt_id,))  # Make it due.
            if point == "worker_in_webhook":  # Needs an inbox row.
                raw, headers = signed("test-secret", {"event_id": "evt1", "gateway": "alpha", "attempt_id": attempt_id, "payment_id": "p", "status": "PENDING", "occurred_at_ms": 1})  # Signed event.
                assert httpx.post(f"{base}/v1/webhooks/alpha", content=raw, headers=headers).status_code == 200  # Stored.
            # A delayed request can land before the void, so any final attempt state is a valid recovery.
            done = lambda: db.execute("SELECT state FROM payment_attempts WHERE id = %s", (attempt_id,)).fetchone()["state"] in ("FAILED_SAFE", "SUCCEEDED", "DECLINED") and db.execute("SELECT count(*) AS n FROM webhook_inbox WHERE processed_at IS NULL").fetchone()["n"] == 0  # Finished check.
        worker = start_worker(database_url, simulator, PAISA_CRASH_AT=point)  # This worker will die.
        assert worker.wait(timeout=30) == 137  # Confirm it really crashed at that point.
        worker = start_worker(database_url, simulator)  # Restart normally.
        assert wait_until(done)  # Recovery finishes.
    finally:
        process.kill(), process.wait()  # Stop the API.
        if worker:  # Stop the worker if running.
            worker.kill(), worker.wait()  # Clean up.
    assert not any(run_audit(database_url, simulator)["violations"].values())  # Money rules still hold.


def test_soak_with_all_faults_passes_audit(database_url, db, merchant):  # Small version of the 10,000-payment run.
    report = soak(database_url, merchant["id"], payments=300, seed=7)  # Run with faults on.
    assert report["payments"] == 300 and report["unresolved"] == 0 and report["pending_reversals"] == 0  # All resolved.
    assert not any(report["violations"].values())  # No money rule broke.


@pytest.fixture
def simulator():  # Runs one fresh real simulator.
    port = free_port()  # Choose a port.
    process = launch("tests.sim_app:app", port, "/openapi.json", SIM_SEED="42")  # Start it.
    yield f"http://127.0.0.1:{port}"  # The test runs here.
    process.kill(), process.wait()  # Stop it.
