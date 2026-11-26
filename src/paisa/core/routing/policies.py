"""
WHAT: interchangeable routing policies, which rank the gateways for a payment.
WHY: the router's safety rules stay fixed while the "brain" can be swapped or improved.
USES: nothing except hashing. This is pure logic with no database or network.
USED BY: core/routing/router.py.
ADD A POLICY: write a class with `version` and `rank()`, then add one line to POLICIES.
          A policy that learns from final results also has `learn()` (see bandit.py).
"""

import hashlib  # Creates repeatable random-looking numbers.
from typing import Protocol  # Describes the shape every policy must have.

EXPLORE = 0.05  # CHANGE ME: share of traffic that tests a non-best gateway. Phase 0 used 5%.


class RoutingPolicy(Protocol):  # The contract for any routing brain.
    version: str  # Saved with each decision, so results can be compared later.

    def rank(self, payment_id, options, rates, context=None):  # Returns (gateways best-first, was_this_exploration).
        ...  # context = {method, bank, amount_paise, now}, for policies that need it.  # Each policy provides its own logic.


def fraction(*parts):  # Returns a repeatable number from 0 up to 1.
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).digest()  # Hash the inputs.
    return int.from_bytes(digest[:8], "big") / 2**64  # Convert to a fraction.


class StaticPolicy:  # Keeps the fixed priority order.
    version = "static-v1"  # Policy name.

    def rank(self, payment_id, options, rates, context=None):  # Ignores live health rates.
        return list(options), False  # Options already arrive in priority order.


class RollingPolicy:  # Uses the best recent success rate, the winning baseline from Phase 0.
    version = "rolling-v1"  # Policy name.

    def __init__(self, explore=EXPLORE):  # explore = share of traffic used for testing.
        self.explore = explore  # Save it.

    def rank(self, payment_id, options, rates, context=None):  # Highest recent success first.
        ranked = sorted(options, key=lambda gateway: -rates[gateway])  # A stable sort keeps priority on ties.
        if len(ranked) > 1 and fraction(payment_id, "explore") < self.explore:  # A small share explores.
            pick = ranked[1 + int(fraction(payment_id, "pick") * (len(ranked) - 1))]  # Any non-best gateway.
            return [pick] + [gateway for gateway in ranked if gateway != pick], True  # Try it first.
        return ranked, False  # Normal case.


from paisa.core.routing import bandit  # noqa: E402  Imported last, because bandit.py imports this file.

POLICIES = {  # ADD A NEW POLICY HERE.
    "static": StaticPolicy,  # Fixed priority order.
    "rolling": RollingPolicy,  # Best recent success rate (the default).
    "thompson": lambda: bandit.ThompsonPolicy(),  # Phase 7 learning router, on all traffic. (lambda: looked up when built.)
    "shadow": lambda: bandit.ShadowPolicy(),  # Rolling routes; Thompson watches and learns.
    "canary": lambda: bandit.CanaryPolicy(),  # Thompson routes 10% of payments, with automatic rollback.
}
