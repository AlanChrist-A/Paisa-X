"""
WHAT: the single place where a gateway result changes a payment.
WHY: API replies, webhooks and status checks can arrive in any order, so all must follow one rule set.
USES: core state and failover rules.
USED BY: services/payments.py and workers/recovery.py.
SAFE TO CHANGE: payment outcome rules. Keep the lock order: payment first, then attempt.
CAREFUL: this file never calls a gateway. Every function runs inside the caller's transaction.
"""

from datetime import UTC, datetime  # Provides the current time.

from psycopg.types.json import Jsonb  # Stores Python dictionaries as JSONB.

from paisa.core.failure_policy import can_failover  # Decides when another gateway is safe.
from paisa.core.routing.static import eligible  # Lists gateways that support a payment.
from paisa.core.states import ATTEMPT, FINAL_PAYMENT, PAYMENT, PAYMENT_FOR_ATTEMPT, move  # State rules.
from paisa.services import outbox  # Saves events in the same transaction.


def utc_now():  # The real clock. Tests can pass a virtual clock instead.
    return datetime.now(UTC)  # Timezone-aware current time.


def view(conn, payment_id):  # Builds the public JSON form of a payment.
    p = conn.execute("SELECT id, amount_paise, currency, method, bank_code, state, created_at FROM payments WHERE id = %s", (payment_id,)).fetchone()  # Payment row.
    rows = conn.execute("SELECT id, attempt_no, gateway, state, customer_prompted FROM payment_attempts WHERE payment_id = %s ORDER BY attempt_no", (payment_id,)).fetchall()  # Its attempts.
    # After a safe failure that reached the customer, only the customer can approve a new try.
    retry = p["state"] == "FAILED" and bool(rows) and rows[-1]["state"] == "FAILED_SAFE" and rows[-1]["customer_prompted"]
    return {**p, "id": str(p["id"]), "created_at": p["created_at"].isoformat(), "can_retry_with_customer": retry, "attempts": [{**r, "id": str(r["id"])} for r in rows]}  # JSON-safe values.


def set_payment(conn, payment_id, target, now):  # Safely changes a payment's state.
    # CAREFUL: FOR UPDATE locks this payment, so two workers cannot change it at the same time.
    current = conn.execute("SELECT state FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()["state"]
    if move(PAYMENT, current, target):  # Stops if the change is illegal.
        # CAREFUL: use the caller's clock. The rescue job compares this time with that same clock.
        conn.execute("UPDATE payments SET state = %s, version = version + 1, updated_at = %s WHERE id = %s", (target, now, payment_id))  # Save it.
        outbox.add(conn, "payment.state_changed", payment_id, None, {"from_state": current, "to_state": target}, now)  # Its event, same transaction.


def save_response(conn, payment_id):  # Stores the first answer, so every retry gets the same one.
    body = view(conn, payment_id)  # Current public view.
    code = 201 if body["state"] in FINAL_PAYMENT else 202  # 201 = finished, 202 = still in progress.
    conn.execute("UPDATE idempotency_keys SET response_code = %s, response_body = %s WHERE payment_id = %s AND response_code IS NULL", (code, Jsonb(body), payment_id))  # First answer wins.
    row = conn.execute("SELECT response_code, response_body FROM idempotency_keys WHERE payment_id = %s", (payment_id,)).fetchone()  # Read what is stored.
    return row["response_code"], row["response_body"], {}  # HTTP code, body and no extra headers.


def remaining_gateways(conn, payment, payment_id):  # Eligible gateways not yet tried for this payment.
    configs = conn.execute("SELECT gateway, priority, methods, skip_banks, networks FROM gateway_config").fetchall()  # Gateway rules.
    tried = {r["gateway"] for r in conn.execute("SELECT gateway FROM payment_attempts WHERE payment_id = %s", (payment_id,)).fetchall()}  # Used already.
    request = {"method": payment["method"], "bank": payment["bank_code"], "network": payment["network"]}  # Routing inputs.
    return [name for name in eligible(request, configs) if name not in tried]  # Best priority first.


def apply_result(conn, attempt_id, target, now, code=None, prompted=False):  # Applies one result and returns the payment state.
    payment_id = conn.execute("SELECT payment_id FROM payment_attempts WHERE id = %s", (attempt_id,)).fetchone()["payment_id"]  # Find the parent.
    p = conn.execute("SELECT state, method, bank_code, network, deadline_at FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()  # Lock payment first.
    a = conn.execute("SELECT state, attempt_no, gateway, customer_prompted FROM payment_attempts WHERE id = %s FOR UPDATE", (attempt_id,)).fetchone()  # Then the attempt.
    prompted = prompted or a["customer_prompted"]  # Once prompted, always prompted.
    if target not in ATTEMPT[a["state"]]:  # A stale or duplicate result, such as PENDING after SUCCEEDED.
        conn.execute("UPDATE payment_attempts SET customer_prompted = %s WHERE id = %s", (prompted, attempt_id))  # Keep this safety fact.
        return p["state"]  # Ignore the stale state.

    winner = target == "SUCCEEDED" and p["state"] not in FINAL_PAYMENT  # First success on an open payment wins.
    conn.execute("UPDATE payment_attempts SET state = %s, is_winner = %s, customer_prompted = %s, failure_class = COALESCE(%s, failure_class), updated_at = %s WHERE id = %s", (target, winner, prompted, code, now, attempt_id))  # Save it.
    conn.execute("INSERT INTO attempt_transitions (attempt_id, from_state, to_state, at) VALUES (%s, %s, %s, %s)", (attempt_id, a["state"], target, now))  # Permanent history.
    outbox.add(conn, "attempt.state_changed", payment_id, attempt_id, {"gateway": a["gateway"], "method": p["method"], "attempt_no": a["attempt_no"], "from_state": a["state"], "to_state": target, "failure_class": code}, now, version=2)  # Its event.

    if target == "SUCCEEDED" and not winner:  # CAREFUL: money moved on a payment that already finished.
        conn.execute("INSERT INTO reversal_tasks (attempt_id, next_try_at) VALUES (%s, %s) ON CONFLICT DO NOTHING", (attempt_id, now))  # Queue the refund in this same transaction.
        return p["state"]  # A final payment state never changes.
    if winner:  # This attempt completes the payment.
        set_payment(conn, payment_id, "SUCCEEDED", now)  # Mark it finished.
        conn.execute("UPDATE payment_attempts SET must_void = true, next_check_at = %s WHERE payment_id = %s AND id <> %s AND state IN ('INITIATED', 'AWAITING_CUSTOMER', 'UNKNOWN')", (now, payment_id, attempt_id))  # Stop any newer attempt.
        save_response(conn, payment_id)  # Give retries a final answer.
        return "SUCCEEDED"  # Done.
    if p["state"] in FINAL_PAYMENT:  # A leftover attempt ended after the payment finished.
        return p["state"]  # Nothing else changes.

    next_state = PAYMENT_FOR_ATTEMPT[target]  # Normal effect on the payment.
    if target == "FAILED_SAFE" and can_failover(a["attempt_no"], prompted, now, p["deadline_at"], remaining_gateways(conn, p, payment_id)):  # Proven safe.
        next_state = "PROCESSING"  # Another gateway may be tried.
    set_payment(conn, payment_id, next_state, now)  # Save the payment state.
    if next_state == "FAILED":  # The payment has ended.
        save_response(conn, payment_id)  # Give retries a final answer.
    return next_state  # Tell the caller what happens next.
