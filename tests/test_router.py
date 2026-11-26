"""
WHAT: Phase 4 tests for circuit breakers, rolling routing, retry budget and fallback.
WHY: a router must keep answering safely even when gateways or its own model fail.
USES: pure router code, plus real Postgres and the simulator for full-flow checks.
SAFE TO CHANGE: add tests whenever a routing rule or policy changes.
"""

from datetime import UTC, datetime, timedelta  # Builds times for the deadline test.

import pytest  # Test framework.

from experiments.phase3.soak import World  # Virtual-clock test world.
from experiments.phase4.drills import outage_drill, reroute_seconds  # Outage measurement.
from paisa.core.failure_policy import can_failover, health_signal  # Failover and health rules.
from paisa.core.routing.policies import POLICIES, RollingPolicy, StaticPolicy  # Routing policies.
from paisa.core.routing.router import BREAKER_COOLDOWN, BREAKER_FAILURES, Breaker, RetryBudget, Router  # Router parts.
from simulator.engine import Fault  # Planned failures.
from tests.test_recovery import FOREVER, calm  # Shared test market.

OPTIONS = ["alpha", "bravo", "delta"]  # Eligible gateways, best static priority first.


def fail(router, gateway, method="UPI", now=0.0, times=BREAKER_FAILURES):  # Reports repeated failures.
    for _ in range(times):  # One result at a time.
        router.record(gateway, method, False, now, now)  # A failed call.


def test_breaker_full_cycle():  # Closed, open, one probe, then closed again.
    breaker = Breaker()  # Starts closed.
    for _ in range(BREAKER_FAILURES):  # Enough failures to trip it.
        breaker.record(False, 0, 0)  # Report one.
    assert not breaker.closed and not breaker.probe_due(BREAKER_COOLDOWN - 1)  # Open, and waiting.
    assert breaker.probe_due(BREAKER_COOLDOWN)  # Cooldown finished.
    breaker.start_probe(BREAKER_COOLDOWN)  # One test payment goes out.
    assert not breaker.probe_due(BREAKER_COOLDOWN)  # A second one must not pile on.
    breaker.record(False, BREAKER_COOLDOWN, BREAKER_COOLDOWN)  # Probe fails.
    assert not breaker.closed and breaker.probe_due(2 * BREAKER_COOLDOWN)  # Stay open and try later.
    breaker.record(True, 2 * BREAKER_COOLDOWN, 2 * BREAKER_COOLDOWN)  # Probe succeeds.
    assert breaker.closed  # Traffic may return.


def test_breaker_ignores_results_from_before_it_opened():  # A slow old success must not reopen traffic.
    breaker = Breaker()  # New breaker.
    for _ in range(BREAKER_FAILURES):  # Trip it at time 10.
        breaker.record(False, 10, 10)  # Failed call.
    breaker.record(True, 9, 11)  # This call started before the outage was detected.
    assert not breaker.closed  # It stays open.


def test_breaker_forgets_old_failures():  # Failures spread over a long time are not an outage.
    breaker = Breaker()  # New breaker.
    for second in range(BREAKER_FAILURES):  # One failure every 20 seconds.
        breaker.record(False, second * 20, second * 20)  # Failed call.
    assert breaker.closed  # Never tripped.


def test_breakers_are_per_gateway_and_method():  # A gateway's UPI can fail while its cards work.
    router = Router(StaticPolicy())  # Simple ordering.
    fail(router, "alpha", "UPI")  # Break alpha for UPI.
    assert router.decide("p1", "UPI", OPTIONS, 1).chosen == "bravo"  # UPI avoids it.
    assert router.decide("p2", "CARD", OPTIONS, 1).chosen == "alpha"  # Cards still use it.


def test_router_sends_one_probe_after_cooldown():  # Recovery is tested carefully.
    router = Router(StaticPolicy())  # Simple ordering.
    fail(router, "alpha")  # Break alpha.
    assert router.decide("p1", "UPI", OPTIONS, BREAKER_COOLDOWN).chosen == "alpha"  # One probe.
    assert router.decide("p2", "UPI", OPTIONS, BREAKER_COOLDOWN).chosen == "bravo"  # Others avoid it.


def test_all_breakers_open_still_answers():  # The router must not refuse because every signal is bad.
    router = Router(StaticPolicy())  # Simple ordering.
    for gateway in OPTIONS:  # Break all of them.
        fail(router, gateway)  # Trip this breaker.
    assert router.decide("p1", "UPI", OPTIONS, 1).chosen in OPTIONS  # Still a valid gateway.


def test_rolling_policy_prefers_best_recent_rate():  # The main routing rule.
    router = Router(RollingPolicy(explore=0))  # No exploration, for an exact answer.
    for ok in (True, False, False, True):  # Alpha is 50%.
        router.record("alpha", "UPI", ok, 0, 0)  # Report it.
    for _ in range(4):  # Bravo is 100%.
        router.record("bravo", "UPI", True, 0, 0)  # Report it.
    router.record("delta", "UPI", False, 0, 0)  # Delta is 0%.
    decision = router.decide("p1", "UPI", OPTIONS, 1)  # Ask for a route.
    assert decision.chosen == "bravo" and decision.scores == {"alpha": 0.5, "bravo": 1.0, "delta": 0.0}  # Best rate wins.


def test_exploration_is_repeatable_and_capped():  # Testing other gateways must stay small.
    policy = RollingPolicy(explore=0.05)  # 5% exploration.
    rates = {"alpha": 1.0, "bravo": 0.9, "delta": 0.8}  # Alpha is best.
    results = [policy.rank(f"p{i}", OPTIONS, rates) for i in range(5_000)]  # Many decisions.
    assert results == [policy.rank(f"p{i}", OPTIONS, rates) for i in range(5_000)]  # Same input, same result.
    assert 0.03 < sum(explored for _, explored in results) / 5_000 < 0.07  # Close to 5%.
    assert all(sorted(ranked) == sorted(OPTIONS) for ranked, _ in results)  # No gateway is lost or invented.


@pytest.mark.parametrize("answer", ["crash", [], ["ghost"]])  # A crash, an empty list, and an invalid gateway.
def test_broken_policy_falls_back(answer):  # Payments must survive a broken model.
    class Broken:  # A deliberately faulty policy.
        version = "broken-v1"  # Its name.
        def rank(self, payment_id, options, rates, context=None):  # Returns the chosen failure.
            if answer == "crash":  # First case.
                raise RuntimeError("model crashed")  # Crash.
            return answer, False  # Bad answer.
    decision = Router(Broken()).decide("p1", "UPI", OPTIONS, 1)  # Ask for a route.
    assert decision.chosen == "alpha" and decision.fallback is True  # Static priority takes over.


def test_retry_budget_limits_retries():  # A failing gateway must not create a retry storm.
    budget = RetryBudget(ratio=0.1, burst=10)  # Starts with 10 retries available.
    assert sum(budget.admit(False) for _ in range(100)) == 10  # The burst runs out.
    for _ in range(100):  # 100 new payments.
        assert budget.admit(True)  # New payments are never blocked.
    assert sum(budget.admit(False) for _ in range(100)) == 10  # They earned 10 more retries.


@pytest.mark.parametrize("target,code,signal", [  # What each result says about gateway health.
    ("SUCCEEDED", None, True), ("AWAITING_CUSTOMER", None, True), ("DECLINED", None, None),
    ("FAILED_SAFE", "GATEWAY_DOWN", False), ("UNKNOWN", None, False), ("FAILED_SAFE", "NOT_SUPPORTED", None),
])
def test_health_signal(target, code, signal):  # A customer's low balance is not the gateway's fault.
    assert health_signal(target, code) is signal  # Check the mapping.


def test_failover_stops_after_deadline():  # A payment must not retry forever.
    now = datetime(2026, 1, 1, tzinfo=UTC)  # A fixed time.
    assert can_failover(1, False, now, now + timedelta(seconds=1), ["bravo"]) is True  # Before the deadline.
    assert can_failover(1, False, now, now, ["bravo"]) is False  # At the deadline.


def test_policy_registry_builds_each_policy():  # Adding a policy should only need one registry line.
    assert all(Router(policy()).decide("p1", "UPI", OPTIONS, 1).chosen in OPTIONS for policy in POLICIES.values())  # All work.


def test_decision_is_logged(database_url, db, merchant):  # Every route must be explainable later.
    world = World(database_url, merchant["id"], 1, (), calm(), router=Router(RollingPolicy(explore=0)))  # Build a world.
    try:  # Always clean up.
        world.pay()  # One payment.
    finally:
        world.close()  # Release resources.
    row = db.execute("SELECT chosen, options, policy_version, explored, fallback, scores FROM routing_decisions").fetchone()  # Read it.
    assert row["chosen"] == "alpha" and row["policy_version"] == "rolling-v1" and row["explored"] is False and row["fallback"] is False  # Correct record.
    assert row["options"][0] == "alpha" and row["scores"]["alpha"] == 1.0  # Inputs are saved.


def test_retry_storm_stays_within_budget(database_url, db, merchant):  # Phase 4 requirement.
    faults = [Fault("outage", name, 0, FOREVER) for name in ("alpha", "bravo", "delta", "echo")]  # All UPI gateways down.
    world = World(database_url, merchant["id"], 1, faults, calm(), router=Router(RollingPolicy(explore=0)))  # Build a world.
    try:  # Always clean up.
        for number in range(50):  # 50 payments during a total outage.
            world.ms += 200  # Five per second.
            world.pay(f"storm-{number}")  # Every first attempt fails.
    finally:
        world.close()  # Release resources.
    total = db.execute("SELECT count(*) AS n FROM payment_attempts").fetchone()["n"]  # All gateway calls.
    assert 0 < total - 50 <= 10 + 5  # Retries happen, but stay within burst + 10% of traffic.


def test_reroute_measure():  # The drill's timing calculation must be correct.
    rows = [(s, "alpha") for s in range(5)] + [(s, "bravo") for s in range(5, 30)]  # Traffic leaves alpha at second 5.
    assert reroute_seconds(rows, "alpha", 0, 30) == 5  # Measured from the outage start.


def test_full_outage_moves_traffic_within_target(database_url, db, merchant):  # Phase 4 requirement.
    result = outage_drill(database_url, merchant["id"])  # Run the real flow.
    assert result["share_before"] >= 0.25  # The drill must kill a gateway that really carried traffic.
    assert result["reroute_seconds"] is not None and result["reroute_seconds"] < 30  # Architecture target.
    assert not any(result["audit"]["violations"].values())  # Money rules still hold.
