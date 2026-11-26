"""
WHAT: tests for the Phase 7 learning router: lessons, learning, forgetting, shadow and canary.
WHY: a learning brain must stay safe: it may never block a payment, and a bad one must roll back.
"""

import pytest  # Test framework.

from paisa.core.routing.bandit import CanaryPolicy, ShadowPolicy, ThompsonPolicy, outcome  # Code under test.
from paisa.core.routing.policies import RollingPolicy  # Baseline brain.
from paisa.core.routing.router import Router  # Routing engine.

OPTIONS = ["alpha", "bravo"]  # Two gateways.
CTX = {"method": "UPI", "bank": "BANK_A", "amount_paise": 10_000, "now": 100.0}  # A payment's details.


@pytest.mark.parametrize("state,prompted,code,lesson", [  # How each final attempt teaches the bandit.
    ("SUCCEEDED", True, None, True), ("REVERSED", True, None, True),  # Money moved: healthy.
    ("DECLINED", True, None, None), ("FAILED_SAFE", False, "NOT_SUPPORTED", None),  # Not the gateway's fault.
    ("FAILED_SAFE", False, "GATEWAY_DOWN", False), ("FAILED_SAFE", True, None, False),  # Gateway failures.
    ("FAILED_SAFE", True, "VOIDED", None), ("FAILED_SAFE", False, "VOIDED", False),  # Customer left / gateway silent.
    ("UNKNOWN", False, None, False), ("UNKNOWN", True, None, None), ("AWAITING_CUSTOMER", True, None, None),
])
def test_outcome_lessons(state, prompted, code, lesson):  # Abandonment and declines never punish a gateway.
    assert outcome(state, prompted, code) is lesson


def test_thompson_learns_the_better_gateway():  # After evidence, the healthy gateway wins most choices.
    policy = ThompsonPolicy(seed=1, explore_cap=None)  # No cap, so this tests pure sampling.
    for i in range(200):  # alpha fails often, bravo works.
        policy.learn(f"p{i}", "alpha", "UPI", "BANK_A", i % 2 == 0, 100.0)  # 50% success.
        policy.learn(f"p{i}", "bravo", "UPI", "BANK_A", True, 100.0)  # 100% success.
    firsts = [policy.rank(f"q{i}", OPTIONS, {}, CTX)[0][0] for i in range(200)]
    assert firsts.count("bravo") > 190  # Nearly always bravo.


def test_old_evidence_fades():  # After many half-lives, a belief is back near "no opinion".
    policy = ThompsonPolicy(half_life=10)
    for i in range(100):
        policy.learn(f"p{i}", "alpha", "UPI", "BANK_A", False, 0.0)  # A burst of failures.
    alpha, beta = policy._belief("alpha", "UPI", "BANK_A", 200.0)  # 20 half-lives later.
    assert alpha == pytest.approx(1, abs=0.01) and beta == pytest.approx(1, abs=0.01)


def test_shadow_never_changes_the_decision():  # The challenger only watches.
    live, shadow = RollingPolicy(), ShadowPolicy()
    rates = {"alpha": 0.5, "bravo": 0.9}
    assert all(shadow.rank(f"p{i}", OPTIONS, rates, CTX) == live.rank(f"p{i}", OPTIONS, rates, CTX) for i in range(500))
    assert shadow.asked == 500


def test_canary_rolls_back_a_worse_challenger():  # Clearly worse results stop the canary.
    canary = CanaryPolicy(share=0.5, min_results=50)
    for i in range(1000):  # Route, then report: the challenger's payments fail, the base's succeed.
        canary.rank(f"p{i}", OPTIONS, {"alpha": 1, "bravo": 1}, CTX)
        canary.learn(f"p{i}", "alpha", "UPI", "BANK_A", canary.served[f"p{i}"] == "base", 100.0)
    assert canary.rolled_back
    assert {canary.rank(f"x{i}", OPTIONS, {"alpha": 1, "bravo": 1}, CTX) and canary.served[f"x{i}"] for i in range(200)} == {"base"}  # All traffic back on base.


def test_crashing_challenger_never_blocks_a_payment():  # A crash falls back to the base brain.
    class Crashing(ThompsonPolicy):
        def rank(self, *args, **kwargs):
            raise RuntimeError("model crashed")
        def learn(self, *args, **kwargs):
            raise RuntimeError("learning crashed")
    router = Router(CanaryPolicy(challenger=Crashing(), share=1.0))  # Every payment tries the challenger.
    decisions = [router.decide(f"p{i}", "UPI", OPTIONS, 1) for i in range(50)]
    router.learn("p0", "alpha", "UPI", "BANK_A", True, 1)  # Must not raise.
    assert all(d.chosen in OPTIONS and not d.fallback for d in decisions) and router.policy.errors >= 51
