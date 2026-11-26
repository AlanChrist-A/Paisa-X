"""
WHAT: tests for Phase 2 money rules, state rules, database guards and the payment API.
WHY: these rules protect customers from wrong or duplicate charges.
USES: fake gateways through httpx.MockTransport, so each failure is exact and repeatable.
SAFE TO CHANGE: add a test whenever a payment rule changes.
"""

import uuid  # Creates IDs for direct database tests.

import httpx  # Provides the fake gateway network.
import psycopg  # Opens a second connection and catches database errors.
import pytest  # Test framework.
from fastapi.testclient import TestClient  # Calls the API in-process.
from hypothesis import given  # Runs one test against many generated inputs.
from hypothesis import strategies as st  # Describes the generated inputs.

from paisa.api.app import create_app  # The API under test.
from paisa.core.failure_policy import classify  # Gateway-result rules.
from paisa.core.money import require_paise  # Money rules.
from paisa.core.routing.static import choose_gateway  # Simple Phase 2 router.
from paisa.core.states import ATTEMPT, PAYMENT, FINAL_PAYMENT, IllegalTransition, move  # State rules.
from paisa.pipeline.audit import audit  # Money audit.
from simulator.engine import load_catalog  # The simulated market.

BODY = {"amount_paise": 12_550, "method": "UPI", "bank": "BANK_A", "instrument_token": "tok_test_1"}  # Valid payment.


def gateway_reply(result, status=200, **extra):  # Builds a fake gateway handler.
    calls = []  # Records every request it receives.
    def handler(request):  # Runs for each fake HTTP call.
        calls.append(request)  # Count it.
        return httpx.Response(status, json={"result": result, "processed": result != "FAILED", "customer_prompted": result == "AWAITING_CUSTOMER", **extra})
    handler.calls = calls  # Let the test read the call list.
    return handler  # Hand it back.


def api(database_url, handler):  # Builds the real API connected to a fake gateway.
    return TestClient(create_app(database_url, httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sim")))


def post(client, merchant, key="key-1", body=BODY):  # Sends one payment request.
    return client.post("/v1/payments", json=body, headers={**merchant["headers"], "Idempotency-Key": key})


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "100", 10**12])  # Unsafe amounts.
def test_money_rejects_unsafe_values(bad):  # Money must be positive whole paise within the limit.
    with pytest.raises(ValueError):  # Each one must be refused.
        require_paise(bad)  # Try it.


def test_money_accepts_whole_paise():  # A normal amount passes unchanged.
    assert require_paise(12_550) == 12_550  # Rs 125.50.


@pytest.mark.parametrize("machine,start", [(PAYMENT, "CREATED"), (ATTEMPT, "INITIATED")])  # Test both state machines.
@given(targets=st.lists(st.sampled_from(sorted(set(PAYMENT) | set(ATTEMPT))), max_size=40))  # Random event orders.
def test_random_events_never_create_an_illegal_path(machine, start, targets):  # Property test.
    current = start  # Begin at the first state.
    for target in targets:  # Apply each random event.
        before = current  # Remember the state.
        try:  # Illegal moves should raise an error.
            if move(machine, current, target):  # A real change happened.
                current = target  # Accept it.
                assert target in machine[before]  # It must be an allowed path.
        except IllegalTransition:  # The event was refused.
            assert current == before  # State must stay untouched.
        if machine is PAYMENT and before in FINAL_PAYMENT:  # A finished payment.
            assert current == before  # Must never change.


@pytest.mark.parametrize("reply,state", [  # Every gateway answer maps to one safe meaning.
    ({"result": "SUCCEEDED"}, "SUCCEEDED"), ({"result": "AWAITING_CUSTOMER"}, "AWAITING_CUSTOMER"),
    ({"result": "DECLINED"}, "DECLINED"), ({"result": "FAILED", "processed": False}, "FAILED_SAFE"),
    ({"result": "UNSUPPORTED", "processed": False}, "FAILED_SAFE"), ({"result": "VOIDED"}, "FAILED_SAFE"),
    ({"result": "FAILED"}, "UNKNOWN"), ({"result": "TIMEOUT", "processed": True}, "UNKNOWN"),
    ({"result": "SOMETHING_NEW"}, "UNKNOWN"), (None, "UNKNOWN"),
])
def test_unclear_gateway_answers_are_unknown(reply, state):  # If in doubt, assume money may have moved.
    assert classify(reply) == state  # Check the mapping.


def test_static_router_respects_support_and_priority():  # The router may only pick a valid gateway.
    configs = [{"gateway": "b", "priority": 2, "methods": ["UPI"], "skip_banks": [], "networks": []},
               {"gateway": "a", "priority": 1, "methods": ["UPI"], "skip_banks": ["BANK_A"], "networks": []}]
    assert choose_gateway({"method": "UPI", "bank": "BANK_A", "network": None}, configs) == "b"  # a skips this bank.
    assert choose_gateway({"method": "UPI", "bank": "BANK_B", "network": None}, configs) == "a"  # a has priority.
    assert choose_gateway({"method": "CARD", "bank": "BANK_B", "network": "NET_X"}, configs) is None  # No support.


@pytest.mark.parametrize("payments,transitions,truth,name", [  # Each case should trigger one audit rule.
    ([{"id": "p", "amount_paise": 10, "state": "AWAITING_CUSTOMER"}], [], {"p": 20}, "overcharged"),
    ([{"id": "p", "amount_paise": 10, "state": "SUCCEEDED"}], [], {}, "success_without_charge"),
    ([{"id": "p", "amount_paise": 10, "state": "FAILED"}], [], {"p": 10}, "failed_with_money"),
    ([], [], {"ghost": 10}, "charge_without_payment"),
    ([], [{"from_state": "DECLINED", "to_state": "INITIATED"}], {}, "illegal_transitions"),
])
def test_audit_catches_each_problem(payments, transitions, truth, name):  # The judge must not miss a bad case.
    assert audit(payments, transitions, truth)["violations"][name] == 1  # Exactly one problem found.


def test_audit_passes_clean_records():  # Correct records produce no false alarm.
    payments = [{"id": "s", "amount_paise": 10, "state": "SUCCEEDED"}, {"id": "f", "amount_paise": 9, "state": "FAILED"}]
    assert not any(audit(payments, [{"from_state": None, "to_state": "INITIATED"}], {"s": 10})["violations"].values())


def test_payment_is_saved_and_can_be_read(database_url, merchant):  # Normal path.
    with api(database_url, gateway_reply("AWAITING_CUSTOMER")) as client:  # Start the API.
        created = post(client, merchant)  # Create a payment.
        assert created.status_code == 202 and created.json()["state"] == "AWAITING_CUSTOMER"  # Still in progress.
        read = client.get(f"/v1/payments/{created.json()['id']}", headers=merchant["headers"])  # Read it back.
        assert read.json()["attempts"][0]["gateway"] == "alpha" and read.json()["amount_paise"] == 12_550  # Saved.


def test_same_key_replays_without_second_gateway_call(database_url, merchant):  # Core idempotency proof.
    gateway = gateway_reply("SUCCEEDED")  # Fake gateway.
    with api(database_url, gateway) as client:  # Start the API.
        first, second = post(client, merchant), post(client, merchant)  # Same key twice.
    assert first.json() == second.json() and second.headers["Idempotent-Replayed"] == "true"  # Same saved answer.
    assert len(gateway.calls) == 1  # CAREFUL: the gateway was called once.


def test_same_key_different_body_is_rejected(database_url, merchant):  # This is a caller bug.
    with api(database_url, gateway_reply("SUCCEEDED")) as client:  # Start the API.
        post(client, merchant)  # First request.
        assert post(client, merchant, body={**BODY, "amount_paise": 999}).status_code == 422  # Changed amount.


@pytest.mark.parametrize("result,status,payment_state,attempt_state", [  # How each outcome is stored.
    ("SUCCEEDED", 200, "SUCCEEDED", "SUCCEEDED"), ("DECLINED", 200, "FAILED", "DECLINED"),
    ("FAILED", 503, "FAILED", "FAILED_SAFE"), ("TIMEOUT", 504, "PENDING_CONFIRMATION", "UNKNOWN"),
])
def test_gateway_outcomes(database_url, merchant, result, status, payment_state, attempt_state):  # One call only.
    gateway = gateway_reply(result, status)  # Fake gateway.
    with api(database_url, gateway) as client:  # Start the API.
        body = post(client, merchant).json()  # Run the payment.
    assert (body["state"], body["attempts"][0]["state"]) == (payment_state, attempt_state)  # Correct states.
    assert len(gateway.calls) == (3 if attempt_state == "FAILED_SAFE" else 1)  # Only a proven-safe failure fails over.


def test_lost_connection_after_send_is_unknown(database_url, merchant):  # The request may have been processed.
    def handler(request):  # Fake network failure after sending.
        raise httpx.ReadError("connection lost", request=request)  # Simulate it.
    with api(database_url, handler) as client:  # Start the API.
        assert post(client, merchant).json()["attempts"][0]["state"] == "UNKNOWN"  # Never assume failure.


def test_connection_refused_is_safe(database_url, merchant):  # The request never left us.
    def handler(request):  # Fake refused connection.
        raise httpx.ConnectError("refused", request=request)  # Simulate it.
    with api(database_url, handler) as client:  # Start the API.
        assert post(client, merchant).json()["attempts"][0]["state"] == "FAILED_SAFE"  # No money could move.


def test_no_eligible_gateway_fails_without_call(database_url, merchant):  # Nothing supports this bank.
    gateway = gateway_reply("SUCCEEDED")  # Fake gateway.
    with api(database_url, gateway) as client:  # Start the API.
        body = post(client, merchant, body={**BODY, "method": "CARD", "network": "NET_NOPE"}).json()  # Unknown network.
    assert body["state"] == "FAILED" and body["attempts"] == [] and not gateway.calls  # No attempt made.


def test_gateway_config_matches_simulator_catalog(db):  # Stops the database and market from drifting apart.
    rows = {r["gateway"]: r for r in db.execute("SELECT * FROM gateway_config").fetchall()}  # Database copy.
    catalog = load_catalog()["gateways"]  # Simulator copy.
    assert rows.keys() == catalog.keys()  # Same gateways.
    for name, cfg in catalog.items():  # Compare each gateway.
        assert set(rows[name]["methods"]) == set(cfg["success"]) and set(rows[name]["skip_banks"]) == set(cfg["skip_banks"])  # Methods and banks.
        assert set(rows[name]["networks"]) == set(cfg["networks"])  # Card networks.


def test_attempt_is_saved_and_no_lock_is_held_during_gateway_call(database_url, merchant):  # Two key design rules.
    seen = {}  # Stores what another connection could see during the call.
    def handler(request):  # Runs while the API is waiting for the gateway.
        attempt_id = request.url.params["attempt_id"]  # The attempt being sent.
        with psycopg.connect(database_url) as other:  # A separate database connection.
            # NOWAIT raises an error immediately if the API is holding this row's lock.
            seen["state"] = other.execute("SELECT state FROM payment_attempts WHERE id = %s FOR UPDATE NOWAIT", (attempt_id,)).fetchone()[0]
        return httpx.Response(200, json={"result": "SUCCEEDED"})  # Then succeed.
    with api(database_url, handler) as client:  # Start the API.
        assert post(client, merchant).status_code == 201  # Finished.
    assert seen["state"] == "INITIATED"  # The attempt was durable before the money call.


def test_login_and_merchant_isolation(database_url, merchant, db):  # One merchant cannot read another's payment.
    from paisa.adapters import postgres  # Used to create the second merchant.
    other = {"Authorization": f"Bearer {postgres.create_merchant(database_url)[1]}"}  # Second merchant.
    with api(database_url, gateway_reply("SUCCEEDED")) as client:  # Start the API.
        assert client.post("/v1/payments", json=BODY, headers={"Idempotency-Key": "x"}).status_code == 401  # No key.
        payment_id = post(client, merchant).json()["id"]  # Merchant 1 creates a payment.
        assert client.get(f"/v1/payments/{payment_id}", headers=other).status_code == 404  # Merchant 2 cannot see it.


@pytest.mark.parametrize("change", [  # Inputs the API must refuse.
    {"amount_paise": -1}, {"amount_paise": 10.5}, {"amount_paise": "100"}, {"method": "CASH"},
    {"instrument_token": "4111111111111111"}, {"unexpected": "field"},
])
def test_invalid_requests_are_rejected(database_url, merchant, change):  # Includes a real-looking card number.
    gateway = gateway_reply("SUCCEEDED")  # Fake gateway.
    with api(database_url, gateway) as client:  # Start the API.
        assert post(client, merchant, body={**BODY, **change}).status_code == 422  # Rejected.
    assert not gateway.calls  # It never reached a gateway.


def insert_payment(db, merchant, **change):  # Directly inserts a payment, skipping the app.
    row = {"id": uuid.uuid4(), "merchant_id": merchant["id"], "amount_paise": 100, "state": "PROCESSING", **change}  # Values.
    db.execute("INSERT INTO payments (id, merchant_id, amount_paise, method, instrument_token, bank_code, state, deadline_at) VALUES (%(id)s, %(merchant_id)s, %(amount_paise)s, 'UPI', 'tok_test', 'BANK_A', %(state)s, now())", row)
    return row["id"]  # Hand back its ID.


def insert_attempt(db, payment_id, number, state, winner=False):  # Directly inserts an attempt.
    db.execute("INSERT INTO payment_attempts (id, payment_id, attempt_no, gateway, state, is_winner) VALUES (%s, %s, %s, 'alpha', %s, %s)", (uuid.uuid4(), payment_id, number, state, winner))


def test_database_blocks_duplicate_idempotency_key(db, merchant):  # Invariant I1.
    sql = "INSERT INTO idempotency_keys (merchant_id, key, request_hash, expires_at) VALUES (%s, 'k', 'x', now())"  # Same key.
    db.execute(sql, (merchant["id"],))  # First insert works.
    with pytest.raises(psycopg.errors.UniqueViolation):  # Second is blocked.
        db.execute(sql, (merchant["id"],))  # Try it.


def test_database_blocks_second_active_attempt(db, merchant):  # Invariant I2.
    payment_id = insert_payment(db, merchant)  # One payment.
    insert_attempt(db, payment_id, 1, "UNKNOWN")  # First attempt is unresolved.
    with pytest.raises(psycopg.errors.UniqueViolation):  # Another active attempt is blocked.
        insert_attempt(db, payment_id, 2, "INITIATED")  # Try it.


def test_database_blocks_second_winner(db, merchant):  # Invariant I3.
    payment_id = insert_payment(db, merchant)  # One payment.
    insert_attempt(db, payment_id, 1, "SUCCEEDED", True)  # First winner.
    with pytest.raises(psycopg.errors.UniqueViolation):  # Second winner is blocked.
        insert_attempt(db, payment_id, 2, "SUCCEEDED", True)  # Try it.


@pytest.mark.parametrize("amount", [0, -5])  # Invalid money.
def test_database_blocks_bad_amount(db, merchant, amount):  # Invariant I7.
    with pytest.raises(psycopg.errors.CheckViolation):  # Database refuses it.
        insert_payment(db, merchant, amount_paise=amount)  # Try it.


def test_database_blocks_changing_final_payment(db, merchant):  # Invariant I5.
    payment_id = insert_payment(db, merchant, state="SUCCEEDED")  # A finished payment.
    with pytest.raises(psycopg.errors.RaiseException):  # The trigger refuses the change.
        db.execute("UPDATE payments SET state = 'FAILED' WHERE id = %s", (payment_id,))  # Try it.


@pytest.mark.parametrize("sql", ["UPDATE attempt_transitions SET to_state = 'FAILED_SAFE'", "DELETE FROM attempt_transitions"])
def test_history_is_append_only(database_url, merchant, db, sql):  # Audit history cannot be rewritten.
    with api(database_url, gateway_reply("SUCCEEDED")) as client:  # Start the API.
        post(client, merchant)  # This writes history rows.
    with pytest.raises(psycopg.errors.RaiseException):  # The trigger refuses the edit.
        db.execute(sql)  # Try it.
