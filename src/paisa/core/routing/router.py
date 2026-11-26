"""
WHAT: the routing engine: circuit breakers, rolling health, retry budget and safe fallback.
WHY: routing must avoid broken gateways and still answer if its own policy crashes.
USES: core/routing/policies.py. No database or network.
USED BY: services/payments.py.
SAFE TO CHANGE: anything marked CHANGE ME. Change the brain in policies.py.
NOTE: breakers always use this process's own results, because they must react within seconds.
      Success rates can also use `shared`: fleet-wide counts from Redis, refreshed in the background
      by services/health_snapshot.py. Routing never waits for Redis.
"""

import threading  # Protects shared state, because API requests run in several threads.
from collections import defaultdict, deque  # Holds recent results per gateway and method.
from dataclasses import dataclass  # Creates a small result object.

BREAKER_SIZE = 10  # CHANGE ME: recent results the breaker watches. Same value as Phase 0.
BREAKER_FAILURES = 7  # CHANGE ME: failures in that group that open the breaker. Lower trips faster, with more false alarms.
BREAKER_MEMORY = 60  # CHANGE ME: seconds before an old result stops counting.
BREAKER_COOLDOWN = 15  # CHANGE ME: seconds between test payments to a broken gateway.
ROLLING_WINDOW = 100  # CHANGE ME: recent results used for success rate. Phase 0 tuning chose 100.
RETRY_RATIO = 0.1  # CHANGE ME: retries earned per new payment (0.1 = about 10% of traffic).
RETRY_BURST = 10  # CHANGE ME: most retries that can be saved up.
SHARED_MIN = 20  # CHANGE ME: fleet-wide results needed before they replace this process's own rate.


class Breaker:  # Circuit breaker: stops calling a failing gateway, then tests it carefully.
    def __init__(self):  # Starts closed, meaning traffic flows normally.
        self.recent = deque(maxlen=BREAKER_SIZE)  # Recent (time, ok) results.
        self.opened_at = None  # None means closed.
        self.next_probe = 0  # When the next test payment may go out.

    @property
    def closed(self):  # Is traffic allowed normally?
        return self.opened_at is None  # True when it has not been opened.

    def probe_due(self, now):  # Half-open: is it time for one careful test?
        return not self.closed and now >= self.next_probe  # Open, and the cooldown has passed.

    def start_probe(self, now):  # Records that a test payment went out.
        self.next_probe = now + BREAKER_COOLDOWN  # Allow only one per cooldown.

    def record(self, ok, started_at, now):  # Learns from one finished gateway call.
        if not self.closed:  # While open, only a call started after opening counts.
            if started_at >= self.opened_at:  # CAREFUL: an old slow success must not reopen traffic.
                if ok:  # The gateway has recovered.
                    self.opened_at = None  # Close the breaker.
                    self.recent.clear()  # Forget outage results.
                else:  # Still broken.
                    self.next_probe = now + BREAKER_COOLDOWN  # Wait before testing again.
            return  # Finished.
        self.recent.append((now, ok))  # Remember this result.
        failures = sum(not good for at, good in self.recent if now - at <= BREAKER_MEMORY)  # Recent failures only.
        if failures >= BREAKER_FAILURES:  # Too many means it is probably down.
            self.opened_at, self.next_probe = now, now + BREAKER_COOLDOWN  # Open it.


class RetryBudget:  # Limits retries so failures cannot overload healthy gateways.
    def __init__(self, ratio=RETRY_RATIO, burst=RETRY_BURST):  # Set the limits.
        # CAREFUL: credits are whole thousandths, because adding 0.1 repeatedly drifts in decimal maths.
        self.earn, self.limit, self.credits = round(ratio * 1000), burst * 1000, burst * 1000  # Starts full.

    def admit(self, first_attempt):  # Returns True if this attempt may run.
        if first_attempt:  # New payments are never blocked.
            self.credits = min(self.limit, self.credits + self.earn)  # Each one earns a small retry credit.
            return True  # Allowed.
        if self.credits < 1000:  # Less than one full retry is available.
            return False  # Refuse the retry.
        self.credits -= 1000  # Spend one retry.
        return True  # Allowed.


@dataclass(frozen=True)
class Decision:  # The router's answer, saved for later analysis.
    chosen: str  # Gateway to call.
    ranked: list  # All options, best first.
    scores: dict  # Recent success rates that were used.
    policy_version: str  # Which policy decided.
    explored: bool  # Was this a deliberate test of a non-best gateway?
    fallback: bool  # Did the policy fail, so static priority was used?


class Router:  # Combines health checks, a policy and fallback.
    def __init__(self, policy):  # policy = any object following RoutingPolicy.
        self.policy, self.budget = policy, RetryBudget()  # Brain and retry limit.
        self.breakers = defaultdict(Breaker)  # One breaker per (gateway, method).
        self.results = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))  # Recent results per (gateway, method).
        self.wins = defaultdict(int)  # Running win counts, faster than recounting.
        self.lock = threading.Lock()  # CAREFUL: only one thread may change routing state at a time.
        self.shared = {}  # (gateway, method) -> (ok, total) from Redis. Replaced whole, so readers never see half an update.

    def admit(self, first_attempt):  # Checks the retry budget.
        with self.lock:  # Protect the shared counter.
            return self.budget.admit(first_attempt)  # Ask the budget.

    def _rate(self, gateway, method):  # Recent success rate.
        ok, total = self.shared.get((gateway, method), (0, 0))  # Fleet-wide counts, if any.
        if total >= SHARED_MIN:  # Enough evidence from all processes.
            return ok / total  # Use it.
        history = self.results[(gateway, method)]  # Find its results.
        return self.wins[(gateway, method)] / len(history) if history else 1.0  # Unknown gateways start high, so they get tried.

    def decide(self, payment_id, method, options, now, bank=None, amount_paise=0):  # Chooses a gateway from eligible options.
        with self.lock:  # No network calls happen here, so this lock is held very briefly.
            breakers = {gateway: self.breakers[(gateway, method)] for gateway in options}  # Their health.
            healthy = [gateway for gateway in options if breakers[gateway].closed] or list(options)  # If all are open, still try.
            rates = {gateway: self._rate(gateway, method) for gateway in options}  # Recent success rates.
            try:  # CAREFUL: a broken policy must never stop a payment.
                context = {"method": method, "bank": bank, "amount_paise": amount_paise, "now": now}  # What learning policies need.
                ranked, explored = self.policy.rank(payment_id, healthy, {g: rates[g] for g in healthy}, context)  # Ask the brain.
                fallback = not ranked or any(gateway not in healthy for gateway in ranked)  # Reject invalid answers.
            except Exception:  # Any policy crash.
                ranked, explored, fallback = [], False, True  # Use the fallback below.
            if fallback:  # The policy could not be trusted.
                ranked, explored = list(healthy), False  # Static priority order.
            probe = next((gateway for gateway in options if breakers[gateway].probe_due(now)), None)  # A gateway due for a test?
            if probe:  # Send this one payment to check recovery.
                breakers[probe].start_probe(now)  # Allow only one.
                ranked = [probe] + [gateway for gateway in ranked if gateway != probe]  # Try it first.
            return Decision(ranked[0], ranked, rates, self.policy.version, explored, fallback)  # Final answer.

    def record(self, gateway, method, ok, started_at, now):  # Learns from one gateway result.
        key = (gateway, method)  # This gateway and method.
        with self.lock:  # Protect shared state.
            breaker = self.breakers[key]  # Its breaker.
            was_open = not breaker.closed  # Remember whether it was open.
            breaker.record(ok, started_at, now)  # Update health.
            if was_open:  # Do not mix outage results into the rolling rate.
                if breaker.closed:  # It has just recovered.
                    self.results[key].clear()  # Start its score fresh.
                    self.wins[key] = 0  # Reset wins too.
                return  # Finished.
            history = self.results[key]  # Rolling results.
            if len(history) == history.maxlen:  # The oldest result is about to drop out.
                self.wins[key] -= history[0]  # Remove it from the count.
            history.append(ok)  # Add the new result.
            self.wins[key] += ok  # Update the count.

    def learn(self, payment_id, gateway, method, bank, ok, now, latency_ms=None):  # Passes a final result to a learning policy.
        learn = getattr(self.policy, "learn", None)  # Only some policies learn from final results.
        if learn is None:  # Rule-based policies use record() only.
            return
        with self.lock:  # Same lock as decide(), so beliefs never change mid-decision.
            try:  # CAREFUL: a learning bug must never break payments.
                learn(payment_id, gateway, method, bank, ok, now, latency_ms)
            except Exception:  # Ignore it; routing still works with the old beliefs.
                pass
