"""
WHAT: the Recovery Worker: processes webhooks, resolves unknown attempts and reverses extra charges.
WHY: gateways and servers fail midway, so something must finish every interrupted payment.
USES: core recovery rules, settlement and the gateway adapter.
USED BY: run with `uv run paisa worker`.
SETTINGS: DATABASE_URL, SIMULATOR_URL and PAISA_WORKER_SLEEP.
SAFE TO CHANGE: batch size and timing values marked CHANGE ME.
CAREFUL: claims commit BEFORE gateway calls. A crashed worker's work simply becomes due again.
"""

import logging  # Warning level for logs.
import os  # Reads settings.
import time  # Pauses between rounds.
from datetime import timedelta  # Calculates future check times.

import httpx  # Calls gateways.
import psycopg  # Detects database connection problems.
from psycopg.rows import dict_row  # Returns rows as dictionaries.
from psycopg_pool import ConnectionPool  # Reuses database connections.

from paisa import observability as obs  # Logs and metrics.
from paisa.adapters import gateway as gateway_api, postgres  # Gateway calls and local database.
from paisa.adapters.failpoints import crash_if  # Test-only crash points.
from paisa.core.recovery import next_delay, next_step  # Pure recovery rules.
from paisa.services.payments import drive  # Continues a safe failover.
from paisa.services.settlement import apply_result, save_response, set_payment, utc_now  # Result handling.

METRICS_PORT = int(os.getenv("PAISA_RECOVERY_METRICS_PORT", "8001"))  # CHANGE ME: where Prometheus reads this worker.
BATCH_SIZE = 100  # CHANGE ME: rows claimed per round. Higher clears backlogs faster, but makes rounds longer.
STALE_PAYMENT = timedelta(seconds=15)  # CHANGE ME: how long a payment may sit between steps before rescue.
REVERSAL_RETRY = timedelta(seconds=5)  # CHANGE ME: wait before retrying a reversal.
WEBHOOK_TARGET = {"PENDING": "AWAITING_CUSTOMER", "SUCCEEDED": "SUCCEEDED", "DECLINED": "DECLINED", "FAILED": "FAILED_SAFE"}  # Webhook meaning.
PROMPTED_RESULTS = {"PENDING", "AWAITING_CUSTOMER", "SUCCEEDED", "DECLINED"}  # These prove the customer saw a prompt.


def process_webhooks(pool, http, clock, limit=BATCH_SIZE):  # Applies stored webhooks one transaction at a time.
    for _ in range(limit):  # Limit work per round.
        with pool.connection() as conn:  # One event, one transaction.
            # SKIP LOCKED: if another worker holds this row, take the next one instead of waiting.
            row = conn.execute("SELECT id, attempt_id, status FROM webhook_inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if not row:  # Inbox is empty.
                return  # Done.
            attempt = conn.execute("SELECT payment_id FROM payment_attempts WHERE id = %s", (row["attempt_id"],)).fetchone()  # Do we know this attempt?
            state = apply_result(conn, row["attempt_id"], WEBHOOK_TARGET[row["status"]], clock(), prompted=row["status"] in PROMPTED_RESULTS) if attempt else None  # Shared rules.
            crash_if("worker_in_webhook")  # Crash test point: this transaction will roll back.
            conn.execute("UPDATE webhook_inbox SET processed_at = now() WHERE id = %s", (row["id"],))  # Saved with the result.
        if state == "PROCESSING":  # The webhook proved failover is safe.
            drive(pool, http, attempt["payment_id"], clock)  # Start it after the transaction closes.


def claim_attempts(pool, clock, limit=BATCH_SIZE):  # Reserves due attempts and schedules their next check.
    now = clock()  # Current time.
    with pool.connection() as conn:  # Short transaction.
        rows = conn.execute("SELECT a.id, a.payment_id, a.gateway, a.check_count, a.created_at, a.must_void, p.method FROM payment_attempts a JOIN payments p ON p.id = a.payment_id WHERE a.state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN') AND a.next_check_at <= %s ORDER BY a.next_check_at LIMIT %s FOR UPDATE OF a SKIP LOCKED", (now, limit)).fetchall()  # Lock only free rows.
        for row in rows:  # Push each one into the future, acting as a lease.
            conn.execute("UPDATE payment_attempts SET next_check_at = %s, check_count = check_count + 1 WHERE id = %s", (now + timedelta(seconds=next_delay(row["check_count"])), row["id"]))  # If we crash, it becomes due again.
    return rows  # Hand back the claimed work.


def void_attempt(http, gateway, attempt_id):  # Tries to block an attempt, then verifies that it worked.
    result = (gateway_api.void(http, gateway, attempt_id) or {}).get("result")  # Send the void.
    crash_if("worker_after_void")  # Crash test point: the gateway acted, but we saved nothing.
    if result == "VOIDED":  # The gateway claims success.
        # CAREFUL: trust but verify. A buggy gateway can say VOIDED and still charge later.
        confirmed = (gateway_api.status(http, gateway, attempt_id) or {}).get("result") == "VOIDED"  # Read it back.
        return "FAILED_SAFE" if confirmed else None  # Without proof, stay unknown.
    return {"ALREADY_SUCCEEDED": "SUCCEEDED", "DECLINED": "DECLINED", "FAILED": "FAILED_SAFE"}.get(result)  # Other clear answers.


def check_attempt(pool, http, row, clock):  # Resolves one claimed attempt.
    crash_if("worker_after_claim")  # Crash test point.
    attempt_id = str(row["id"])  # The ID in text form for gateway calls.
    status = (gateway_api.status(http, row["gateway"], attempt_id) or {}).get("result")  # Ask the gateway.
    crash_if("worker_after_status")  # Crash test point.
    if status is None:  # The gateway could not answer.
        return  # The claim already scheduled another check.
    prompted = status in PROMPTED_RESULTS  # Did the customer see a prompt?
    step = next_step(status, row["method"], (clock() - row["created_at"]).total_seconds(), row["must_void"])  # Pure decision.
    if step == "WAIT":  # Too early to act.
        if prompted:  # Still record this safety fact.
            with pool.connection() as conn:  # Short transaction.
                apply_result(conn, row["id"], "AWAITING_CUSTOMER", clock(), prompted=True)  # Stale moves are ignored.
        return  # Check again later.
    if step == "VOID":  # Time is up, or another attempt won.
        with pool.connection() as conn:  # Record the uncertainty first.
            apply_result(conn, row["id"], "UNKNOWN", clock(), prompted=prompted)  # Treat money as possibly moved.
        step = void_attempt(http, row["gateway"], attempt_id)  # Try to block it.
        if step is None:  # Still not proven.
            return  # Stay unknown, and check again later.
    with pool.connection() as conn:  # Save the final result.
        state = apply_result(conn, row["id"], step, clock(), prompted=prompted)  # Shared rules.
    obs.log("attempt.resolved", attempt_id=attempt_id, gateway=row["gateway"], gateway_said=status, result=step)  # Searchable by attempt ID.
    crash_if("worker_after_apply")  # Crash test point.
    if state == "PROCESSING":  # Failover is proven safe.
        drive(pool, http, row["payment_id"], clock)  # Try the next gateway.


def resume_payments(pool, http, clock, limit=BATCH_SIZE):  # Rescues payments stopped between steps.
    now = clock()  # Current time.
    with pool.connection() as conn:  # Short transaction.
        rows = conn.execute("SELECT p.id, p.state FROM payments p WHERE p.state IN ('CREATED', 'PROCESSING') AND p.updated_at <= %s AND NOT EXISTS (SELECT 1 FROM payment_attempts a WHERE a.payment_id = p.id AND a.state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN')) ORDER BY p.updated_at LIMIT %s FOR UPDATE OF p SKIP LOCKED", (now - STALE_PAYMENT, limit)).fetchall()  # Interrupted payments.
        for row in rows:  # Handle each one.
            if row["state"] == "CREATED":  # No gateway was ever called, so ending it is safe.
                set_payment(conn, row["id"], "FAILED", now)  # End it.
                save_response(conn, row["id"])  # Retries now get an answer.
            else:  # A safe failover was interrupted.
                conn.execute("UPDATE payments SET updated_at = %s WHERE id = %s", (now, row["id"]))  # Lease, so others leave it for now.
    for row in rows:  # After the transaction closes.
        if row["state"] == "PROCESSING":  # Needs a next attempt.
            drive(pool, http, row["id"], clock)  # Continue.


def reverse_charges(pool, http, clock, limit=BATCH_SIZE):  # Returns money from non-winning charges.
    now = clock()  # Current time.
    with pool.connection() as conn:  # Short transaction.
        # CAREFUL: NOT a.is_winner guarantees we never refund the customer's one valid charge.
        rows = conn.execute("SELECT r.attempt_id, a.gateway FROM reversal_tasks r JOIN payment_attempts a ON a.id = r.attempt_id WHERE r.done_at IS NULL AND r.next_try_at <= %s AND NOT a.is_winner ORDER BY r.next_try_at LIMIT %s FOR UPDATE OF r SKIP LOCKED", (now, limit)).fetchall()  # Due tasks.
        for row in rows:  # Lease each task.
            conn.execute("UPDATE reversal_tasks SET tries = tries + 1, next_try_at = %s WHERE attempt_id = %s", (now + REVERSAL_RETRY, row["attempt_id"]))  # Retry later if we crash.
    for row in rows:  # After the transaction closes.
        result = (gateway_api.reverse(http, row["gateway"], str(row["attempt_id"])) or {}).get("result")  # Stable ID makes retries safe.
        crash_if("worker_after_reverse")  # Crash test point: money returned, but not yet recorded.
        if result == "REVERSED":  # Confirmed by the gateway.
            with pool.connection() as conn:  # Record both facts together.
                apply_result(conn, row["attempt_id"], "REVERSED", clock())  # Update the attempt.
                conn.execute("UPDATE reversal_tasks SET done_at = %s WHERE attempt_id = %s", (clock(), row["attempt_id"]))  # Close the task.


def run_once(pool, http, clock=utc_now):  # One full recovery round.
    process_webhooks(pool, http, clock)  # Webhooks first, because they may avoid a status call.
    for row in claim_attempts(pool, clock):  # Check due attempts.
        check_attempt(pool, http, row, clock)  # Resolve each one.
    resume_payments(pool, http, clock)  # Rescue interrupted payments.
    reverse_charges(pool, http, clock)  # Return extra charges.


def main():  # Runs the worker until stopped.
    obs.setup("recovery", METRICS_PORT)  # JSON logs, and /metrics on its own port.
    address = os.getenv("DATABASE_URL") or postgres.url()  # Choose the database.
    pause = float(os.getenv("PAISA_WORKER_SLEEP", "0.5"))  # CHANGE ME: lower reacts faster, but uses more CPU.
    with ConnectionPool(address, min_size=1, max_size=3, kwargs={"row_factory": dict_row}) as pool, httpx.Client(base_url=os.getenv("SIMULATOR_URL", "http://127.0.0.1:9000"), timeout=3) as http:  # Shared connections.
        while True:  # Keep working.
            try:  # A database restart should not kill the worker.
                run_once(pool, http)  # One round.
                obs.RECOVERY_ROUNDS.labels("ok").inc()
            except psycopg.OperationalError as error:  # Temporary database problem.
                obs.RECOVERY_ROUNDS.labels("error").inc()
                obs.log("database.unavailable", logging.WARNING, error=type(error).__name__)  # Log without secrets.
            time.sleep(pause)  # Short pause.
