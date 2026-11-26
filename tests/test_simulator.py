"""
WHAT: automated checks for the Gateway Simulator (Phase 1).
WHY: later phases trust this simulator as the "truth", so its failure behaviour must be proven.
USES: simulator/engine.py and simulator/app.py.
SAFE TO CHANGE: add one test for every new fault type or endpoint.
"""

import asyncio  # Runs the one async webhook-delivery test.
import copy  # Makes a private copy of the catalog for each test world.
import hashlib  # Creates a fingerprint of a whole simulation run.
import hmac  # Independently recalculates webhook signatures.
import json  # Turns run results into exact bytes for fingerprinting.

import httpx  # Provides a fake network for the webhook test.
import pytest  # Test framework.
from fastapi.testclient import TestClient  # Calls the API without opening a real port.

from simulator.app import create_app, deliver_once, sign  # Web-layer pieces under test.
from simulator.engine import FAULT_KINDS, Fault, GatewaySim, load_catalog, load_scenarios, make_traffic  # Engine.

END = 10_000_000  # A virtual time far enough ahead for every pending event to finish.


def world(*faults, seed=1, decline=0.0):  # Builds a predictable world: everything works unless a fault says otherwise.
    catalog = copy.deepcopy(load_catalog())  # Copy so one test cannot affect another.
    for method in catalog["methods"].values():  # Simplify each payment method.
        method.update(business_decline=decline, abandon=0, customer_ms=[100, 100])  # Fixed customer delay.
    for gateway in catalog["gateways"].values():  # Simplify each gateway.
        gateway["success"] = {name: 1.0 for name in gateway["success"]}  # No random technical failures.
    return GatewaySim(seed, faults, catalog)  # Return the ready simulator.


def payment(number=1, bank="BANK_A", method="UPI"):  # Builds one fake payment.
    return {"payment_id": f"p{number}", "amount_paise": 10_000, "method": method, "bank": bank, "network": None}


def final(sim, attempt="a1", gateway="alpha"):  # Reads an attempt's final state after all events finish.
    return sim.status(gateway, attempt, END)["result"]  # Move time far ahead and ask for status.


def fingerprint(seed, faults):  # Runs traffic and returns one hash of everything that happened.
    sim, replies, hooks = GatewaySim(seed, faults), [], []  # Fresh simulator and result lists.
    gateways = list(sim.catalog["gateways"])  # Names of all gateways.
    for number, item in enumerate(make_traffic(seed, 300)):  # Send 300 generated payments.
        at_ms = item.pop("at_ms")  # Separate arrival time from the payment data.
        replies.append(sim.pay(gateways[number % len(gateways)], f"a{number}", item, at_ms))  # Take turns.
        hooks += sim.due_webhooks(at_ms)  # Collect webhooks due so far.
    hooks += sim.due_webhooks(END)  # Collect all remaining webhooks.
    raw = json.dumps([replies, hooks, sim.truth()], sort_keys=True).encode()  # Exact bytes of the run.
    return hashlib.sha256(raw).hexdigest()  # A hash changes if even one byte changes.


@pytest.mark.parametrize("name", load_scenarios("tuning"))  # Repeat for every tuning scenario.
def test_same_seed_is_byte_identical(name):  # Phase 1's main reproducibility proof.
    faults = load_scenarios("tuning")[name]  # Load this scenario's faults.
    assert fingerprint(5, faults) == fingerprint(5, faults)  # Same seed gives the same bytes.
    assert fingerprint(5, faults) != fingerprint(6, faults)  # A different seed gives a different run.


def test_pay_is_idempotent_and_charges_once():  # A retry with the same ID must not charge again.
    sim = world()  # Healthy world.
    first = sim.pay("alpha", "a1", payment(), 0)  # First request.
    assert sim.pay("alpha", "a1", payment(), 1) == first  # Same ID returns the same answer.
    assert final(sim) == "SUCCEEDED" and sim.truth()["net_paise"]["p1"] == 10_000  # Charged exactly once.


def test_customer_decline_is_shared_across_gateways():  # Common random numbers keep comparisons fair.
    sim = world(decline=0.5)  # Half of customers lack funds, for example.
    for number in range(50):  # Try 50 payments on two gateways.
        sim.pay("alpha", f"a{number}", payment(number), 0), sim.pay("bravo", f"b{number}", payment(number), 0)
        assert final(sim, f"a{number}", "alpha") == final(sim, f"b{number}", "bravo")  # Same customer result.


def test_full_outage_is_safe_and_unrecorded():  # Connection refused means the gateway never saw it.
    sim = world(Fault("outage", "alpha", 0, 1_000))  # Alpha is down for 1 second.
    assert sim.pay("alpha", "a1", payment(), 10)["processed"] is False  # It promises no money moved.
    assert sim.status("alpha", "a1", 20)["result"] == "NOT_FOUND"  # It has no record.


def test_partial_outage_hits_only_one_bank():  # A gateway can fail for one bank only.
    sim = world(Fault("outage", "alpha", 0, 1_000, bank="BANK_A"))  # Only BANK_A is affected.
    assert sim.pay("alpha", "a1", payment(1, "BANK_A"), 10)["result"] == "FAILED"  # BANK_A fails.
    assert sim.pay("alpha", "a2", payment(2, "BANK_B"), 10)["result"] == "AWAITING_CUSTOMER"  # BANK_B works.


def test_brownout_slows_but_does_not_fail():  # Slow is different from broken.
    normal = world().pay("alpha", "a1", payment(), 10)  # Normal latency.
    slow = world(Fault("brownout", "alpha", 0, 1_000, value=5)).pay("alpha", "a1", payment(), 10)  # Slowed.
    assert slow["result"] == "AWAITING_CUSTOMER" and slow["latency_ms"] == pytest.approx(normal["latency_ms"] * 5, abs=5)


def test_slow_decay_gets_worse_over_time():  # Gradual failure is hard to notice.
    sim = world(Fault("decay", "alpha", 0, 10_000, value=0.0))  # Health falls from 100% to 0%.
    early = sum(sim.pay("alpha", f"e{n}", payment(n), 1)["result"] != "FAILED" for n in range(100))  # Start.
    sim = world(Fault("decay", "alpha", 0, 10_000, value=0.0))  # Same fault, fresh world.
    late = sum(sim.pay("alpha", f"l{n}", payment(n), 9_900)["result"] != "FAILED" for n in range(100))  # End.
    assert early > 90 and late < 60  # Some late failures happen after the prompt, so 60 is a safe limit.


def test_flapping_goes_down_up_down():  # A flapping gateway alternates between states.
    sim = world(Fault("flap", "alpha", 0, 1_000, value=100))  # Changes every 100 ms.
    results = [sim.pay("alpha", f"a{t}", payment(t), t)["processed"] for t in (10, 110, 210)]  # Three periods.
    assert results == [False, True, False]  # Down, up, down.


def test_timeout_after_charge_moves_money():  # The dangerous case: caller sees timeout, money moved.
    sim = world(Fault("timeout_after_charge", "alpha", 0, 1_000))  # Every reply is lost.
    assert sim.pay("alpha", "a1", payment(), 10)["result"] == "TIMEOUT"  # Caller does not know.
    assert sim.status("alpha", "a1", 11)["result"] == "SUCCEEDED"  # Gateway already took the money.
    assert sim.truth()["net_paise"]["p1"] == 10_000  # The truth ledger confirms it.


def test_in_flight_request_lands_later_unless_voided():  # Why "not found" is not proof of safety.
    fault = Fault("timeout_in_flight", "alpha", 0, 1_000)  # The request is delayed on the network.
    sim = world(fault)  # World without a void.
    assert sim.pay("alpha", "a1", payment(), 10)["result"] == "TIMEOUT"  # Caller sees a timeout.
    assert sim.status("alpha", "a1", 20)["result"] == "NOT_FOUND"  # Gateway has not seen it yet.
    assert final(sim) == "SUCCEEDED"  # But it lands later and charges.
    sim = world(fault)  # Same case, now with a void.
    sim.pay("alpha", "a1", payment(), 10)  # Request is in flight.
    assert sim.void("alpha", "a1", 20)["result"] == "VOIDED"  # Block this attempt ID forever.
    assert final(sim) == "VOIDED" and sim.truth()["net_paise"].get("p1", 0) == 0  # No money moved.


def test_void_before_request_arrives_blocks_it():  # A void can reach the gateway before the payment does.
    sim = world()  # Healthy world.
    sim.void("alpha", "a1", 0)  # Leave a tombstone for this ID.
    assert sim.pay("alpha", "a1", payment(), 1)["result"] == "VOIDED"  # The late payment is refused.


def test_void_bug_causes_late_success():  # Simulates a misbehaving gateway.
    sim = world(Fault("timeout_in_flight", "alpha", 0, 1_000), Fault("void_bug", "alpha", 0, END))  # Both faults.
    sim.pay("alpha", "a1", payment(), 10)  # Request is in flight.
    assert sim.void("alpha", "a1", 20)["result"] == "VOIDED"  # Gateway claims success at voiding.
    assert final(sim) == "SUCCEEDED"  # But it charges anyway, so Phase 3 must reverse it.


@pytest.mark.parametrize("kind", ["webhook_lost", "webhook_duplicate", "webhook_late", "webhook_reorder"])
def test_webhook_faults(kind):  # Webhooks can be lost, repeated, late or out of order.
    sim = world(Fault(kind, "alpha", 0, END))  # Apply one webhook fault.
    sim.pay("alpha", "a1", payment(), 0)  # Start a payment.
    early = sim.due_webhooks(sim.catalog["sim"]["webhook_ms"] + 1_000)  # Normal delivery period.
    hooks = early + sim.due_webhooks(END)  # Everything delivered.
    statuses = [hook["status"] for hook in hooks]  # Keep the order of statuses.
    if kind == "webhook_lost": assert hooks == []  # Nothing arrives.
    if kind == "webhook_duplicate": assert statuses == ["PENDING", "PENDING", "SUCCEEDED", "SUCCEEDED"] and hooks[0] == hooks[1]
    if kind == "webhook_late": assert early == [] and statuses == ["PENDING", "SUCCEEDED"]  # Late, but still arrives.
    if kind == "webhook_reorder": assert statuses == ["SUCCEEDED", "PENDING"]  # Final arrives before pending.


def test_overload_rejects_extra_requests():  # Retry storms overload a gateway.
    sim = world(Fault("overload", "alpha", 0, 5_000, value=2))  # Capacity: 2 requests per second.
    results = [sim.pay("alpha", f"a{n}", payment(n), 100)["processed"] for n in range(3)]  # Third is too many.
    assert results == [True, True, False]  # The extra request is refused safely.
    assert sim.pay("alpha", "a9", payment(9), 1_200)["processed"] is True  # Capacity returns next second.


def test_reversal_is_idempotent():  # Retrying a refund must not refund twice.
    sim = world()  # Healthy world.
    sim.pay("alpha", "a1", payment(), 0), final(sim)  # Complete a charge.
    assert sim.reverse("alpha", "a1", END)["result"] == "REVERSED"  # First reversal.
    assert sim.reverse("alpha", "a1", END)["result"] == "REVERSED"  # Retry gives the same answer.
    assert sim.truth()["net_paise"]["p1"] == 0 and len(sim.truth()["ledger"]) == 2  # One charge, one reversal.


def test_scenarios_cover_every_fault_and_locked_set_is_guarded():  # Tuning and test sets stay separate.
    for group, unlock in (("tuning", False), ("locked", True)):  # Validate both files.
        kinds = {fault.kind for faults in load_scenarios(group, unlock=unlock).values() for fault in faults}
        assert kinds == FAULT_KINDS  # Every fault type must appear.
    with pytest.raises(PermissionError):  # The locked set must not open by accident.
        load_scenarios("locked")


def test_webhook_signature_and_delivery():  # A receiver must be able to verify each webhook.
    seen = []  # Stores what the fake receiver gets.
    def receiver(request):  # Fake merchant-side webhook endpoint.
        seen.append(request)  # Keep the request.
        return httpx.Response(200)  # Reply OK.
    sim = world()  # Healthy world.
    sim.pay("alpha", "a1", payment(), 0)  # This creates a PENDING webhook.
    async def send():  # Async wrapper for delivery.
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:  # No real network.
            await deliver_once(sim, 1_000, client, "http://paisa/webhook", "test-secret")  # Deliver due events.
    asyncio.run(send())  # Run it.
    timestamp = seen[0].headers["X-Sim-Timestamp"]  # Timestamp that was signed.
    expected = hmac.new(b"test-secret", timestamp.encode() + b"." + seen[0].content, hashlib.sha256).hexdigest()
    assert seen[0].headers["X-Sim-Signature"] == expected == sign("test-secret", timestamp, seen[0].content)


def test_http_api_flow():  # Check that the web layer correctly exposes the engine.
    clock = [0]  # A controllable fake clock.
    client = TestClient(create_app(seed=1, clock=lambda: clock[0], use_latency=False))  # Real API, no sleeping.
    body = {"payment_id": "p1", "amount_paise": 500, "method": "UPI", "bank": "BANK_A"}  # Valid request.
    assert client.post("/sim/alpha/pay", json=body, params={"attempt_id": "a1"}).status_code in (200, 503)  # Answers.
    assert client.get("/sim/alpha/status/a1").status_code == 200  # Status endpoint works.
    assert client.post("/sim/alpha/void/a1").status_code == 200  # Void endpoint works.
    assert client.post("/sim/alpha/reverse/a1").status_code == 200  # Reverse endpoint works.
    assert client.post("/sim/nope/pay", json=body, params={"attempt_id": "x"}).status_code == 404  # Unknown gateway.
    assert client.post("/sim/alpha/pay", json={**body, "amount_paise": -1}, params={"attempt_id": "b"}).status_code == 422
    fault = {"kind": "outage", "gateway": "alpha", "duration_ms": 1_000}  # Inject an outage over HTTP.
    assert client.post("/sim/admin/faults", json=fault).status_code == 200  # Fault accepted.
    assert client.post("/sim/alpha/pay", json={**body, "payment_id": "p2"}, params={"attempt_id": "a2"}).status_code == 503
    assert "net_paise" in client.get("/sim/admin/truth").json()  # The audit can read the truth.
