"""
WHAT: automated checks for the Phase 0 routing experiment.
WHY: if the experiment has a bug, its verdict means nothing, so we test the logic first.
USES: experiments/phase0/routing_experiment.py.
SAFE TO CHANGE: add a test whenever you add a routing method or change a rule.
"""

import numpy as np  # NumPy creates small fake worlds for the tests.

# Import only the pieces being tested from the experiment file.
from experiments.phase0.routing_experiment import (
    CONFIG, Fixed, RollingBreaker, Thompson, bootstrap_ci, make_world, run,
)


def test_same_seed_builds_identical_world():  # Reproducibility: same seed, same run.
    first, second = make_world(7, CONFIG), make_world(7, CONFIG)  # Build the world twice.
    assert np.array_equal(first[0], second[0])  # True gateway health must match.
    assert np.array_equal(first[1], second[1])  # Payment luck must match too.


def test_probabilities_are_valid():  # A success chance must stay between 0% and 100%.
    probs, _, _ = make_world(3, CONFIG)  # Build a world and keep its probabilities.
    assert probs.min() >= 0 and probs.max() <= 1  # Check the full allowed range.


def test_run_uses_the_shared_payment_luck():  # Common random numbers keep comparisons fair.
    outcomes = np.array([[True, False], [False, True], [True, True]])  # Fake results.
    success, chosen = run(Fixed(np.array([0.1, 0.9])), outcomes, 1, np.random.default_rng(0))  # Always gateway 1.
    assert chosen.tolist() == [1, 1, 1]  # Fixed must choose the normally best gateway.
    assert success.tolist() == [False, True, True]  # Results must come from gateway 1's column.


def test_feedback_arrives_only_after_delay():  # The router must never see the future.
    class Spy(Fixed):  # A fake policy that records when results arrive.
        seen = []  # Stores each delivery time.
        def update(self, t, gateway, success):  # Called when a result is delivered.
            self.seen.append(t)  # Save the delivery time.
    run(Spy(np.array([1.0])), np.ones((5, 1), bool), 2, np.random.default_rng(0))  # Use a delay of 2.
    assert Spy.seen == [2, 3, 4]  # Payments 0, 1, 2 report at times 2, 3, 4.


def test_breaker_opens_then_probes_then_closes():  # Check the full circuit breaker cycle.
    policy = RollingBreaker(np.array([0.9, 0.9]), window=50)  # Two equal gateways.
    for t in range(CONFIG.breaker_size):  # Send enough failures to trip the breaker.
        policy.update(t, 0, False)  # Gateway 0 fails every time.
    assert policy.tripped[0]  # Gateway 0 must now be blocked.
    rng = np.random.default_rng(0)  # Random source for the policy.
    assert all(policy.choose(CONFIG.breaker_size, rng) == 1 for _ in range(50))  # No traffic during cooldown.
    probe_time = int(policy.open_until[0])  # The cooldown ends here.
    assert policy.choose(probe_time, rng) == 0  # One test payment goes to gateway 0.
    assert policy.choose(probe_time, rng) == 1  # A second payment must not pile on.
    policy.update(probe_time + 1, 0, True)  # The test payment succeeds.
    assert not policy.tripped[0]  # The breaker closes.


def test_thompson_forgets_old_results():  # Old evidence must fade, or outages go unnoticed.
    policy = Thompson(np.array([0.9, 0.9]), half_life=10)  # Memory halves every 10 updates.
    for t in range(100):  # Teach it that gateway 0 is great.
        policy.update(t, 0, True)  # Record a success.
    peak = policy.alpha[0]  # Remember its confidence.
    for t in range(100, 200):  # Now only gateway 1 reports.
        policy.update(t, 1, True)  # Record a success on gateway 1.
    assert policy.alpha[0] < 1.05 < peak  # Gateway 0's evidence has nearly returned to the start.


def test_thompson_moves_away_from_broken_gateway():  # The main behaviour we need.
    outcomes = np.column_stack([np.zeros(1000, bool), np.ones(1000, bool)])  # 0 always fails, 1 always works.
    _, chosen = run(Thompson(np.array([0.9, 0.9]), half_life=200), outcomes, 5, np.random.default_rng(0))  # Run it.
    assert (chosen[500:] == 1).mean() > 0.95  # By the second half, it should use gateway 1.


def test_bootstrap_interval_detects_a_clear_gain():  # The statistics helper must behave sensibly.
    low, high = bootstrap_ci(np.full(30, 0.01) + np.linspace(-0.001, 0.001, 30), seed=0)  # Small clear gain.
    assert 0 < low < 0.01 < high  # The interval is above zero and contains the true mean.
