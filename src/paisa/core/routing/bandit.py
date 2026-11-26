"""
WHAT: the Phase 7 learning router: discounted Thompson Sampling, plus shadow and canary wrappers.
WHY: a learning brain can adapt to gateway health faster than fixed rules, but it must be proven
     safely: first in the shadows, then on a small share of traffic that rolls back on its own.
USES: core/routing/policies.py for the repeatable hash. No database or network.
USED BY: core/routing/policies.py (POLICIES), services/payments.py (via Router.learn) and experiments/phase7.
SAFE TO CHANGE: anything marked CHANGE ME. Results in docs/decisions/0009 show which settings won.
CAREFUL: beliefs live in memory only. A restart forgets them, and the bandit relearns in minutes.
"""

import random  # Draws from Beta distributions, seeded so runs repeat.
from collections import OrderedDict  # Remembers which canary arm served a payment, oldest first.

from paisa.core.routing.policies import RollingPolicy, fraction  # Baseline policy and repeatable hash.

HALF_LIFE = 60  # CHANGE ME: seconds until old evidence counts half. Phase 7 tuning chose 60 (of 30, 60, 120).
EXPLORE_CAP = 0.10  # CHANGE ME: most share of payments that may explore. Kept in Phase 7: fewer slow detours.
PRIOR = (1.0, 1.0)  # Beta(1, 1): "no opinion yet", every success rate equally believable.
BANK_STRENGTH = 10  # CHANGE ME: how many results a new bank segment borrows from its gateway + method.
CANARY_SHARE = 0.10  # CHANGE ME: share of payments the challenger routes during a canary.
CANARY_MIN = 200  # CHANGE ME: results each arm needs before the canary judges anything.
CANARY_MARGIN = 0.02  # CHANGE ME: how much worse (2 points) the challenger may look before rollback.


def outcome(state, prompted, code=None):  # Turns a FINAL attempt into a lesson: True, False or None (ignore).
    # Used by the Phase 7 delayed-feedback test. Live payments learn from the fast health signal instead,
    # because waiting for final results measured slightly worse (docs/decisions/0009).
    if state in ("SUCCEEDED", "REVERSED"):  # Money moved, so the gateway worked (a reversal is our choice).
        return True
    if state == "DECLINED" or code == "NOT_SUPPORTED":  # The customer's bank said no, or a wrong route.
        return None  # CAREFUL: never punish a gateway for these.
    if code == "VOIDED":  # We gave up on it and blocked it.
        return None if prompted else False  # Prompted = the customer walked away. Not prompted = the gateway never answered.
    if state == "UNKNOWN" and prompted:  # The customer was asked and has not finished: likely walking away.
        return None
    if state in ("FAILED_SAFE", "UNKNOWN", "INITIATED"):  # A technical failure, or no answer in time.
        return False
    return None  # AWAITING_CUSTOMER: still waiting for the customer, so nothing to learn yet.


class ThompsonPolicy:  # Discounted Thompson Sampling, one belief per gateway + method (+ bank, optionally).
    version = "thompson-v1"  # Policy name.

    def __init__(self, half_life=HALF_LIFE, seed=0, by_bank=False, explore_cap=EXPLORE_CAP, costs=None, latency_weight=0.0):
        self.half_life = half_life  # Memory length in seconds.
        self.by_bank = by_bank  # Learn separately per bank too?
        self.explore_cap = explore_cap  # Most share of payments allowed to explore (None = no cap).
        self.costs = costs or {}  # (gateway, method) -> (fee_bps, flat_fee_paise). Empty = success only.
        self.latency_weight = latency_weight  # Utility lost per second of gateway delay.
        self.rng = random.Random(seed)  # Seeded, so the same inputs give the same choices.
        self.beliefs = {}  # key -> (alpha, beta, last_update_seconds).
        self.latency = {}  # (gateway, method) -> recent average latency in ms.

    def _faded(self, key, now, prior):  # A belief after fading toward its prior for the time passed.
        alpha, beta, last = self.beliefs.get(key, (*prior, now))  # Unknown keys start at the prior.
        keep = 0.5 ** (max(0.0, now - last) / self.half_life)  # Share of evidence still remembered.
        return prior[0] + (alpha - prior[0]) * keep, prior[1] + (beta - prior[1]) * keep  # Fade only the evidence.

    def _belief(self, gateway, method, bank, now):  # The (alpha, beta) to sample for one option.
        base = self._faded((gateway, method), now, PRIOR)  # Gateway + method belief.
        if not self.by_bank:  # Segments off.
            return base
        mean = base[0] / (base[0] + base[1])  # The wider average success rate.
        prior = (1 + BANK_STRENGTH * mean, 1 + BANK_STRENGTH * (1 - mean))  # A rare bank starts from it.
        return self._faded((gateway, method, bank), now, prior)  # Bank belief, faded toward that start.

    def _utility(self, gateway, method, p, amount):  # What one option is worth, per rupee of payment.
        fee_bps, flat = self.costs.get((gateway, method), (0, 0))  # Its fee terms, if known.
        fee_share = fee_bps / 10_000 + (flat / amount if amount else 0)  # Fee as a share of the payment.
        delay = self.latency.get((gateway, method), 0) / 1000  # Recent latency in seconds.
        return p * (1 - fee_share) - self.latency_weight * delay  # Expected money kept, minus a delay penalty.

    def rank(self, payment_id, options, rates, context=None):  # Best gateway first, and whether this explored.
        c = context or {}  # Method, bank, amount and time from the router.
        method, bank, now, amount = c.get("method"), c.get("bank"), c.get("now", 0.0), c.get("amount_paise", 0)
        beliefs = {g: self._belief(g, method, bank, now) for g in options}  # Current beliefs.
        sampled = {g: self._utility(g, method, self.rng.betavariate(*beliefs[g]), amount) for g in options}  # One guess each.
        mean = {g: self._utility(g, method, a / (a + b), amount) for g, (a, b) in beliefs.items()}  # Best guess each.
        ranked = sorted(options, key=lambda g: -sampled[g])  # Highest sampled worth first.
        explored = ranked[0] != max(options, key=lambda g: mean[g])  # Did luck pick a non-best option?
        if explored and self.explore_cap is not None and fraction(payment_id, "cap") >= self.explore_cap:  # Over the cap.
            return sorted(options, key=lambda g: -mean[g]), False  # Use the best guess instead.
        return ranked, explored

    def learn(self, payment_id, gateway, method, bank, ok, now, latency_ms=None):  # Adds one final result.
        if latency_ms is not None:  # Track recent delay (moving average, 10% weight on the newest).
            old = self.latency.get((gateway, method), latency_ms)  # First value starts the average.
            self.latency[(gateway, method)] = 0.9 * old + 0.1 * latency_ms
        if ok is None:  # Nothing to learn about health.
            return
        keys = [((gateway, method), PRIOR)]  # Always update the gateway + method belief.
        if self.by_bank:  # Also the bank segment, starting from the wider average.
            a, b = self._faded((gateway, method), now, PRIOR)  # Current wider belief.
            keys.append(((gateway, method, bank), (1 + BANK_STRENGTH * a / (a + b), 1 + BANK_STRENGTH * b / (a + b))))
        for key, prior in keys:  # Update each belief.
            alpha, beta = self._faded(key, now, prior)  # Fade first...
            self.beliefs[key] = (alpha + ok, beta + (not ok), now)  # ...then add this result.


class ShadowPolicy:  # Routes with the live policy; the challenger only watches and learns.
    def __init__(self, live=None, shadow=None):  # Defaults: rolling routes, Thompson watches.
        self.live, self.shadow = live or RollingPolicy(), shadow or ThompsonPolicy()  # The two brains.
        self.version = f"shadow({self.shadow.version})-{self.live.version}"  # Saved with each decision.
        self.asked = self.agreed = self.errors = 0  # How often the shadow would have chosen the same.

    def rank(self, payment_id, options, rates, context=None):
        ranked, explored = self.live.rank(payment_id, options, rates, context)  # The real decision.
        try:  # CAREFUL: a broken shadow must never affect payments.
            self.agreed += self.shadow.rank(payment_id, options, rates, context)[0][0] == ranked[0]  # Same first choice?
            self.asked += 1
        except Exception:  # Shadow crashed.
            self.errors += 1  # Count it, and carry on.
        return ranked, explored

    def learn(self, *args, **kwargs):  # The shadow learns from real results.
        try:
            self.shadow.learn(*args, **kwargs)
        except Exception:  # Never let learning break anything.
            self.errors += 1


class CanaryPolicy:  # Gives the challenger a small share of traffic, and rolls it back if it does worse.
    def __init__(self, base=None, challenger=None, share=CANARY_SHARE, min_results=CANARY_MIN, margin=CANARY_MARGIN):
        self.base, self.challenger = base or RollingPolicy(), challenger or ThompsonPolicy()  # The two brains.
        self.share, self.min_results, self.margin = share, min_results, margin  # Canary settings.
        self.served = OrderedDict()  # payment_id -> arm, so results credit the arm that routed them.
        self.score = {"base": [0, 0], "challenger": [0, 0]}  # Arm -> [successes, results].
        self.rolled_back, self.errors = False, 0  # Has the canary been stopped? Challenger crashes.
        self.version = self.base.version  # Updated per decision.

    def rank(self, payment_id, options, rates, context=None):
        arm = "challenger" if not self.rolled_back and fraction(payment_id, "canary") < self.share else "base"  # Same payment, same arm.
        if arm == "challenger":
            try:
                ranked, explored = self.challenger.rank(payment_id, options, rates, context)  # Challenger decides.
            except Exception:  # CAREFUL: a crash must never block a payment.
                self.errors += 1
                arm = "base"  # Fall back to the base brain for this payment.
        if arm == "base":
            ranked, explored = self.base.rank(payment_id, options, rates, context)  # Base decides.
        self.served[payment_id] = arm  # Remember who routed it.
        while len(self.served) > 100_000:  # Keep memory bounded.
            self.served.popitem(last=False)  # Forget the oldest.
        self.version = f"canary-{self.challenger.version}" if arm == "challenger" else self.base.version  # Saved with the decision.
        return ranked, explored

    def learn(self, payment_id, gateway, method, bank, ok, now, latency_ms=None):
        try:  # The challenger keeps learning from ALL traffic, so it is ready if promoted.
            self.challenger.learn(payment_id, gateway, method, bank, ok, now, latency_ms)
        except Exception:
            self.errors += 1
        arm = self.served.get(payment_id)  # Who routed this payment?
        if ok is None or arm is None:  # Nothing to score.
            return
        self.score[arm][0] += ok  # Count a success.
        self.score[arm][1] += 1  # Count a result.
        (bw, bn), (cw, cn) = self.score["base"], self.score["challenger"]  # Unpack.
        if not self.rolled_back and min(bn, cn) >= self.min_results and cw / cn < bw / bn - self.margin:  # Clearly worse.
            self.rolled_back = True  # Automatic rollback: all traffic goes back to the base brain.
