"""
WHAT: real-process tests: concurrent requests, hard crashes and the money audit.
WHY: fake objects cannot prove what survives when a real server process suddenly dies.
USES: real Postgres, a real Paisa API process and a real Gateway Simulator process.
SAFE TO CHANGE: add a crash point here whenever a new step can leave partial work.
"""

import os  # Passes settings to server processes.
import socket  # Finds a free network port.
import subprocess  # Starts and stops real server processes.
import sys  # Finds the current Python program.
import time  # Waits for servers to become ready.
from concurrent.futures import ThreadPoolExecutor  # Sends many requests at the same time.

import httpx  # Makes real HTTP calls.
import pytest  # Test framework.

from paisa.pipeline.audit import run_audit  # The money audit.
from simulator.engine import make_traffic  # Repeatable fake payments.

BODY = {"amount_paise": 12_550, "method": "UPI", "bank": "BANK_A", "instrument_token": "tok_test_1"}  # Valid payment.


def free_port():  # Asks Windows for an unused local port.
    with socket.socket() as sock:  # Temporary socket.
        sock.bind(("127.0.0.1", 0))  # Port 0 means "pick one for me".
        return sock.getsockname()[1]  # Return the chosen port.


def launch(target, port, ready_path, factory=False, **env):  # Starts one real server and waits for it.
    args = [sys.executable, "-m", "uvicorn", *(["--factory"] if factory else []), target, "--host", "127.0.0.1", "--port", str(port), "--log-level", "critical"]
    process = subprocess.Popen(args, env={**os.environ, "PYTHONPATH": os.pathsep.join([".", "src"]), **env})  # Start it.
    for _ in range(150):  # Wait up to about 15 seconds.
        try:  # The server may not be listening yet.
            if httpx.get(f"http://127.0.0.1:{port}{ready_path}", timeout=1).status_code == 200:  # Ready.
                return process  # Hand it back.
        except httpx.HTTPError:  # Not ready.
            pass  # Keep waiting.
        time.sleep(0.1)  # Short pause.
    process.kill()  # Clean up after a failed start.
    raise RuntimeError(f"{target} did not start")  # Fail clearly.


@pytest.fixture
def simulator():  # Runs one fresh real simulator for a test.
    port = free_port()  # Choose a port.
    process = launch("tests.sim_app:app", port, "/openapi.json", SIM_SEED="42")  # Start it.
    yield f"http://127.0.0.1:{port}"  # The test runs here.
    process.kill(), process.wait()  # Stop it.


def start_api(database_url, simulator, **env):  # Runs one real Paisa API process.
    port = free_port()  # Choose a port.
    process = launch("paisa.api.app:create_app", port, "/healthz", factory=True, DATABASE_URL=database_url, SIMULATOR_URL=simulator, PAISA_ROUTING_POLICY="static", **env)  # Static keeps these crash tests predictable.
    return process, f"http://127.0.0.1:{port}"  # Process and address.


def send(base, merchant, key="key-1", body=BODY):  # Sends one payment over real HTTP.
    return httpx.post(f"{base}/v1/payments", json=body, headers={**merchant["headers"], "Idempotency-Key": key}, timeout=30)


def test_100_concurrent_identical_requests_create_one_payment(database_url, db, merchant, simulator):  # Phase 2 proof.
    process, base = start_api(database_url, simulator)  # Start the API.
    try:  # Always stop the server.
        with ThreadPoolExecutor(100) as pool:  # 100 parallel callers.
            replies = list(pool.map(lambda _: send(base, merchant), range(100)))  # Same key, same body.
    finally:
        process.kill(), process.wait()  # Stop it.
    assert {reply.status_code for reply in replies} <= {201, 202, 409}  # No server errors.
    assert db.execute("SELECT count(*) AS n FROM payments").fetchone()["n"] == 1  # Exactly one payment.
    assert db.execute("SELECT count(*) AS n FROM payment_attempts").fetchone()["n"] == 1  # Exactly one attempt.
    assert not any(run_audit(database_url, simulator)["violations"].values())  # Money agrees.


@pytest.mark.parametrize("point,attempts,retry_codes", [  # Crash at each dangerous step.
    ("after_tx1", 0, {409}),  # Payment saved, no attempt yet.
    ("after_tx2", 1, {409}),  # Attempt saved, gateway not called.
    ("after_gateway", 1, {409}),  # Gateway called, result not saved.
    ("after_tx3", 1, {201, 202}),  # Result saved, reply lost.
])
def test_hard_crash_leaves_recoverable_state(database_url, db, merchant, simulator, point, attempts, retry_codes):
    process, base = start_api(database_url, simulator, PAISA_CRASH_AT=point)  # This server will die at that point.
    with pytest.raises(httpx.HTTPError):  # The caller sees a broken connection.
        send(base, merchant)  # Trigger the crash.
    process.wait(timeout=10)  # Confirm the process really died.
    assert db.execute("SELECT count(*) AS n FROM payments").fetchone()["n"] == 1  # The payment survived.
    before = db.execute("SELECT count(*) AS n FROM payment_attempts").fetchone()["n"]  # Attempts after the crash.
    assert before == attempts or (point == "after_tx3" and before > attempts)  # after_tx3 may include a safe failover.
    process, base = start_api(database_url, simulator)  # Restart normally.
    try:  # Always stop the server.
        retry = send(base, merchant)  # The caller safely retries.
    finally:
        process.kill(), process.wait()  # Stop it.
    assert retry.status_code in retry_codes  # Correct answer for this crash point.
    assert db.execute("SELECT count(*) AS n FROM payments").fetchone()["n"] == 1  # Still one payment.
    assert db.execute("SELECT count(*) AS n FROM payment_attempts").fetchone()["n"] == before  # No new attempt.
    assert not any(run_audit(database_url, simulator)["violations"].values())  # Money agrees.


def test_audit_is_clean_with_timeouts_and_real_simulator(database_url, db, merchant, simulator):  # End-to-end check.
    for kind, value in (("timeout_after_charge", 0.3), ("timeout_in_flight", 0.2)):  # Dangerous gateway faults.
        httpx.post(f"{simulator}/sim/admin/faults", json={"kind": kind, "gateway": "alpha", "duration_ms": 120_000, "value": value}).raise_for_status()
    process, base = start_api(database_url, simulator)  # Start the API.
    try:  # Always stop the server.
        for number, item in enumerate(make_traffic(11, 300)):  # Send 300 generated payments.
            body = {"amount_paise": item["amount_paise"], "method": item["method"], "bank": item["bank"], "network": item["network"], "instrument_token": "tok_test_1"}
            assert send(base, merchant, f"key-{number}", body).status_code in (201, 202)  # Handled safely.
    finally:
        process.kill(), process.wait()  # Stop it.
    report = run_audit(database_url, simulator)  # Compare our records with the hidden truth.
    assert report["payments"] == 300 and not any(report["violations"].values())  # No money rule was broken.
    assert report["unresolved"] > 0  # Unknown attempts remain, and Phase 3 will resolve them.
