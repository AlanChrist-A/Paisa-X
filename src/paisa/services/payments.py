"""
WHAT: creates a payment and runs its gateway attempts in short, safe transactions.
WHY: a crash at any point must leave a record that shows exactly what still needs checking.
USES: core rules, settlement, the gateway adapter and crash switches.
USED BY: api/app.py and workers/recovery.py.
SAFE TO CHANGE: routing can change in start_attempt().
CAREFUL: never call a gateway inside `with pool.connection()`. A slow gateway would hold locks
         and database connections, so one bad gateway could freeze every payment.
"""

import hashlib  # Fingerprints a request body.
import json  # Creates a stable text form of the request.
import os  # Reads the routing policy setting.
import uuid  # Creates payment and attempt IDs.
from datetime import timedelta  # Calculates deadlines.

from psycopg.types.json import Jsonb  # Stores routing scores as JSONB.

from paisa import observability as obs  # Metrics and logs.
from paisa.adapters import gateway as gateway_api  # Calls gateways.
from paisa.adapters.failpoints import crash_if  # Test-only crash points.
from paisa.core.failure_policy import can_failover, classify, health_signal  # Interprets answers and limits retries.
from paisa.core.routing.policies import POLICIES  # Available routing policies.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.services import outbox  # Saves events in the same transaction.
from paisa.services.settlement import apply_result, remaining_gateways, save_response, set_payment, utc_now, view  # Result handling.

PAYMENT_DEADLINE = timedelta(minutes=15)  # CHANGE ME: how long a payment may keep trying.
KEY_LIFETIME = timedelta(hours=24)  # CHANGE ME: how long an idempotency key must be remembered.
FIRST_CHECK = timedelta(seconds=5)  # CHANGE ME: when the worker first checks an attempt. Keep above the gateway timeout.
new_id = uuid.uuid4  # Creates IDs. Repeatable test runs can replace this.
router = Router(POLICIES[os.getenv("PAISA_ROUTING_POLICY", "rolling")]())  # CHANGE ME: choose a policy with this setting.


def existing_request(row, request_hash):  # Answers a request whose key was already used.
    if bytes(row["request_hash"]) != request_hash:  # Same key, different payment details.
        return 422, {"error": "idempotency_key_reused"}, {}  # This is a bug in the caller.
    if row["response_code"] is not None:  # The original request finished.
        return row["response_code"], row["response_body"], {"Idempotent-Replayed": "true"}  # Return the saved answer.
    return 409, {"error": "request_in_progress", "payment_id": str(row["payment_id"])}, {"Retry-After": "1"}  # Still running.


def start_attempt(pool, payment_id, clock):  # Saves the next attempt BEFORE any gateway call.
    now = clock()  # Current time.
    with pool.connection() as conn:  # One short transaction.
        p = conn.execute("SELECT state, amount_paise, method, bank_code, network, deadline_at FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()  # Lock the payment.
        active = conn.execute("SELECT 1 FROM payment_attempts WHERE payment_id = %s AND state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN')", (payment_id,)).fetchone()  # CAREFUL: invariant I4.
        if p["state"] not in ("CREATED", "PROCESSING") or active:  # Finished, or another attempt is unresolved.
            return None  # Do not start anything.
        count = conn.execute("SELECT count(*) AS n FROM payment_attempts WHERE payment_id = %s", (payment_id,)).fetchone()["n"]  # Attempts so far.
        options = remaining_gateways(conn, p, payment_id)  # Gateways not yet tried.
        if not can_failover(count, False, now, p["deadline_at"], options) or not router.admit(count == 0):  # No safe option, or no retry budget.
            set_payment(conn, payment_id, "FAILED", now)  # All earlier attempts are finished, so no money is in doubt.
            save_response(conn, payment_id)  # Give retries an answer.
            return None  # Stop.
        decision = router.decide(str(payment_id), p["method"], options, now.timestamp(), p["bank_code"], p["amount_paise"])  # Runs in memory, with no network call.
        attempt_id, chosen = new_id(), decision.chosen  # New attempt on the chosen gateway.
        set_payment(conn, payment_id, "PROCESSING", now)  # Advance the payment.
        conn.execute("INSERT INTO payment_attempts (id, payment_id, attempt_no, gateway, state, next_check_at, created_at, updated_at) VALUES (%s, %s, %s, %s, 'INITIATED', %s, %s, %s)", (attempt_id, payment_id, count + 1, chosen, now + FIRST_CHECK, now, now))  # Durable record.
        conn.execute("INSERT INTO attempt_transitions (attempt_id, to_state, at) VALUES (%s, 'INITIATED', %s)", (attempt_id, now))  # History.
        conn.execute("INSERT INTO routing_decisions (attempt_id, options, chosen, policy_version, at, scores, explored, fallback) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (attempt_id, decision.ranked, chosen, decision.policy_version, now, Jsonb(decision.scores), decision.explored, decision.fallback))  # Why this gateway.
        outbox.add(conn, "attempt.state_changed", payment_id, attempt_id, {"gateway": chosen, "method": p["method"], "attempt_no": count + 1, "from_state": None, "to_state": "INITIATED", "failure_class": None}, now, version=2)  # Attempt event.
        outbox.add(conn, "router.decision", payment_id, attempt_id, {"gateway": chosen, "options": decision.ranked, "policy_version": decision.policy_version, "scores": decision.scores, "explored": decision.explored, "fallback": decision.fallback}, now)  # Decision event.
    payload = {"payment_id": str(payment_id), "amount_paise": p["amount_paise"], "method": p["method"], "bank": p["bank_code"], "network": p["network"]}  # Only what a gateway needs.
    return attempt_id, chosen, payload  # Ready to send.


def drive(pool, http, payment_id, clock=utc_now):  # Runs attempts until the payment stops needing a new one.
    while started := start_attempt(pool, payment_id, clock):  # Save the next attempt, if one is allowed.
        attempt_id, chosen, payload = started  # Unpack it.
        crash_if("after_tx2")  # Crash test point.
        called_at = clock().timestamp()  # Lets the breaker ignore results from before it opened.
        reply = gateway_api.pay(http, chosen, str(attempt_id), payload)  # Call the gateway with no transaction open.
        crash_if("after_gateway")  # Crash test point: money may have moved, but we have not saved the result.
        details, target = reply or {}, classify(reply)  # Reply details and their safe meaning.
        obs.ATTEMPTS.labels(chosen, target).inc()  # Gateway health for dashboards.
        obs.log("gateway.attempt", payment_id=payment_id, attempt_id=attempt_id, gateway=chosen, result=target, code=details.get("code"))
        with pool.connection() as conn:  # Save the result.
            state = apply_result(conn, attempt_id, target, clock(), details.get("code"), bool(details.get("customer_prompted")))  # One shared rule path.
        healthy = health_signal(target, details.get("code"))  # What this says about the gateway.
        if healthy is not None:  # Skip customer-side results.
            router.record(chosen, payload["method"], healthy, called_at, clock().timestamp())  # Teach the router.
            router.learn(payload["payment_id"], chosen, payload["method"], payload["bank"], healthy, called_at, details.get("latency_ms"))  # Teach a learning policy (Phase 7).
        if state != "PROCESSING":  # PROCESSING is the only state that allows another attempt.
            return  # Stop here.


def create(pool, http, merchant_id, key, request, clock=utc_now):  # The complete payment request.
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).digest()  # Stable fingerprint.
    payment_id, now = new_id(), clock()  # New ID and current time.
    with pool.connection() as conn:  # TX1: save the key and payment together.
        # CAREFUL: the database picks one winner when identical requests arrive together.
        won = conn.execute("INSERT INTO idempotency_keys (merchant_id, key, request_hash, expires_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING key", (merchant_id, key, request_hash, now + KEY_LIFETIME)).fetchone()
        if not won:  # Another request already owns this key.
            row = conn.execute("SELECT request_hash, payment_id, response_code, response_body FROM idempotency_keys WHERE merchant_id = %s AND key = %s", (merchant_id, key)).fetchone()  # Read it.
            return existing_request(row, request_hash)  # Never start a second payment.
        conn.execute("INSERT INTO payments (id, merchant_id, amount_paise, method, network, instrument_token, bank_code, state, deadline_at, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'CREATED', %s, %s, %s)", (payment_id, merchant_id, request["amount_paise"], request["method"], request["network"], request["instrument_token"], request["bank"], now + PAYMENT_DEADLINE, now, now))
        outbox.add(conn, "payment.created", payment_id, None, {"amount_paise": request["amount_paise"], "method": request["method"], "bank": request["bank"]}, now)  # Event in TX1.
        conn.execute("UPDATE idempotency_keys SET payment_id = %s WHERE merchant_id = %s AND key = %s", (payment_id, merchant_id, key))  # Link them.
    crash_if("after_tx1")  # Crash test point.
    drive(pool, http, payment_id, clock)  # Run gateway attempts.
    with pool.connection() as conn:  # Save the answer for retries.
        result = save_response(conn, payment_id)  # First saved answer wins.
    crash_if("after_tx3")  # Crash test point.
    return result  # Reply to the caller.
