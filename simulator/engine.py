"""
WHAT: the Gateway Simulator's pure logic: payments, faults, webhooks and the true money ledger.
WHY: Paisa-X needs a realistic, repeatable outside world that can fail on command.
USES: catalog.toml (market settings) and scenarios/*.toml (fault plans).
USED BY: simulator/app.py (HTTP layer), tests, and later the Replay Lab.
SAFE TO CHANGE: market numbers in catalog.toml and scenarios. To add a fault type, add it to
                FAULT_KINDS, then read it with _hit() or _match() where it should take effect.
CAREFUL: this file has no real clock, network or Python `random`. Time is always passed in,
         which is what makes every run byte-for-byte repeatable.
"""

import hashlib  # Creates stable random-looking numbers from text.
import heapq  # A priority queue: always gives the earliest scheduled event first.
import tomllib  # Reads the TOML settings files.
from collections import defaultdict, deque  # Helpers for per-gateway request history.
from dataclasses import dataclass  # Creates small, clear data objects.
from itertools import count  # Produces 0, 1, 2... to keep equal-time events in order.
from pathlib import Path  # Builds file paths that work on any operating system.

HERE = Path(__file__).parent  # The folder containing this file.
FAULT_KINDS = {  # Every fault the simulator understands.
    "outage", "brownout", "decay", "flap", "timeout_after_charge", "timeout_in_flight", "void_bug",
    "webhook_lost", "webhook_duplicate", "webhook_late", "webhook_reorder", "overload",
}


def load_catalog():  # Reads the simulated market from its settings file.
    return tomllib.loads((HERE / "catalog.toml").read_text("utf-8"))  # Returns plain dictionaries.


def load_scenarios(group, unlock=False):  # Loads a named set of fault scenarios.
    if group == "locked" and not unlock:  # CAREFUL: protects the final exam from accidental use.
        raise PermissionError("Locked scenarios are for the final Phase 8 evaluation only.")  # Stop.
    data = tomllib.loads((HERE / "scenarios" / f"{group}.toml").read_text("utf-8"))  # Read the file.
    return {name: [Fault(**fault) for fault in faults] for name, faults in data.items()}  # Build faults.


def draw(seed, *parts):  # Returns a repeatable "random" number from 0 up to 1.
    # CAREFUL: Python's built-in hash() changes between runs, so we use SHA-256.
    digest = hashlib.sha256(":".join(map(str, (seed, *parts))).encode()).digest()  # Hash the inputs.
    return int.from_bytes(digest[:8], "big") / 2**64  # Convert the first 8 bytes to a fraction.


def make_traffic(seed, total, per_second=1, catalog=None):  # Generates repeatable fake payments.
    catalog = catalog or load_catalog()  # Use the default market unless given one.
    names, shares = zip(*((name, m["share"]) for name, m in catalog["methods"].items()))  # Method mix.
    payments = []  # Stores generated payments.
    for i in range(total):  # Create one payment at a time.
        payment_id, pick, used = f"pay_{seed}_{i:07d}", draw(seed, "method", i), 0.0  # ID and a method draw.
        method = names[-1]  # Fallback in case rounding leaves a tiny gap.
        for name, share in zip(names, shares):  # Walk through the share ranges.
            used += share  # Extend the range.
            if pick < used:  # The draw landed in this method's range.
                method = name  # Choose it.
                break  # Stop searching.
        bank = catalog["banks"][int(draw(seed, "bank", i) * len(catalog["banks"]))]  # Pick a bank.
        network = catalog["networks"][int(draw(seed, "net", i) * len(catalog["networks"]))] if method == "CARD" else None  # Cards only.
        amount_paise = 100 + int(draw(seed, "amount", i) ** 3 * 4_999_900)  # Rs 1 to Rs 50,000, with mostly small payments.
        payments.append({"payment_id": payment_id, "amount_paise": amount_paise, "method": method, "bank": bank, "network": network, "at_ms": i * 1000 // per_second})  # Save it.
    return payments  # Hand back the full list.


@dataclass(frozen=True)  # frozen=True prevents a fault plan from changing mid-run.
class Fault:  # One planned failure.
    kind: str  # Which failure from FAULT_KINDS.
    gateway: str  # Which gateway it affects.
    start_ms: int  # When it starts.
    end_ms: int  # When it stops.
    value: float = 1.0  # Strength: a rate, multiplier, period or capacity, depending on kind.
    method: str | None = None  # Optional: affect only this payment method.
    bank: str | None = None  # Optional: affect only this bank.

    def __post_init__(self):  # Runs automatically after creation.
        if self.kind not in FAULT_KINDS or self.end_ms <= self.start_ms:  # Catch config mistakes early.
            raise ValueError(f"Invalid fault: {self}")  # Fail loudly instead of silently doing nothing.

    def applies(self, gateway, now, payment):  # Does this fault affect this request right now?
        in_scope = self.gateway == gateway and self.start_ms <= now < self.end_ms  # Right gateway and time.
        matches = (not self.method or payment["method"] == self.method) and (not self.bank or payment["bank"] == self.bank)  # Filters.
        up_phase = self.kind == "flap" and int((now - self.start_ms) // self.value) % 2 == 1  # Flap: odd periods are up.
        return in_scope and matches and not up_phase  # All conditions must hold.


@dataclass
class Attempt:  # What a gateway remembers about one payment attempt.
    id: str  # Attempt ID, which is also the gateway's idempotency key.
    gateway: str  # The gateway handling it.
    payment: dict | None  # Payment details. None means a void arrived before the payment.
    state: str  # IN_FLIGHT, AWAITING_CUSTOMER, SUCCEEDED, DECLINED, FAILED, VOIDED or REVERSED.
    outcome: str | None = None  # Planned customer result. None means the customer never responds.
    latency_ms: int = 0  # How long the gateway's reply takes.
    prompted: bool = False  # Has the customer been asked to approve?


class GatewaySim:  # All 5 simulated gateways plus the hidden truth about money.
    def __init__(self, seed, faults=(), catalog=None):  # Set up one simulated world.
        self.seed, self.faults, self.catalog = seed, list(faults), catalog or load_catalog()  # Inputs.
        self.attempts, self.ledger = {}, []  # Attempt records and the true money ledger.
        self._events, self._webhooks, self._seq = [], [], count()  # Time-ordered queues and tie-breaker.
        self._recent = defaultdict(deque)  # Recent request times per gateway, for overload.

    def _match(self, kind, gateway, now, payment):  # Finds active faults of one kind.
        return [f for f in self.faults if f.kind == kind and f.applies(gateway, now, payment)]  # Filter.

    def _hit(self, kind, attempt, now, *extra):  # Decides whether a rate-based fault strikes this attempt.
        # Uses payment + gateway, not attempt ID, so every routing policy gets the same luck.
        key = (kind, attempt.payment["payment_id"], attempt.gateway, *extra)  # Stable random key.
        return any(draw(self.seed, *key) < f.value for f in self._match(kind, attempt.gateway, now, attempt.payment))  # Roll.

    def advance(self, now):  # Runs every scheduled event up to this time.
        while self._events and self._events[0][0] <= now:  # While the earliest event is due.
            at, _, action, key = heapq.heappop(self._events)  # Remove it from the queue.
            getattr(self, action)(self.attempts[key], at)  # Run _arrive or _finish at its exact time.

    def _schedule(self, at, action, attempt):  # Adds a future event.
        heapq.heappush(self._events, (at, next(self._seq), action, (attempt.gateway, attempt.id)))  # Queue it.

    def _emit(self, attempt, status, at, deliver_after=None):  # Queues a webhook, applying webhook faults.
        if self._hit("webhook_lost", attempt, at, status):  # Some webhooks never arrive.
            return  # So later phases must also poll status.
        when = at + self.catalog["sim"]["webhook_ms"]  # Normal delivery time.
        if status == "PENDING" and deliver_after is not None and self._hit("webhook_reorder", attempt, at):  # Reorder fault.
            when = deliver_after + self.catalog["sim"]["reorder_ms"]  # PENDING lands after the final result.
        if self._hit("webhook_late", attempt, at, status):  # Some webhooks are delayed.
            when += self.catalog["sim"]["late_webhook_ms"]  # Add the delay.
        event = {"event_id": hashlib.sha256(f"{attempt.gateway}:{attempt.id}:{status}".encode()).hexdigest()[:24], "gateway": attempt.gateway, "attempt_id": attempt.id, "payment_id": attempt.payment["payment_id"], "status": status, "occurred_at_ms": at}  # Stable ID lets receivers remove duplicates.
        for copy in range(2 if self._hit("webhook_duplicate", attempt, at, status) else 1):  # One or two copies.
            heapq.heappush(self._webhooks, (when + copy, next(self._seq), event))  # Queue each copy.

    def due_webhooks(self, now):  # Returns all webhooks ready to deliver.
        self.advance(now)  # First bring the world up to date.
        due = []  # Collects ready webhooks.
        while self._webhooks and self._webhooks[0][0] <= now:  # While the earliest webhook is due.
            due.append(heapq.heappop(self._webhooks)[2])  # Keep the event data only.
        return due  # Hand them to the caller.

    def health(self, gateway, payment, now):  # True technical success chance of an accepted request. Also used by the Replay Lab oracle.
        health = self.catalog["gateways"][gateway]["success"][payment["method"]]  # Normal chance.
        for f in self._match("decay", gateway, now, payment):  # Gradual decline.
            health *= 1 - (1 - f.value) * (now - f.start_ms) / (f.end_ms - f.start_ms)  # Worse as time passes.
        return health

    def _process(self, a, now):  # The gateway has received the request, so decide what will happen.
        cfg, method = self.catalog["gateways"][a.gateway], self.catalog["methods"][a.payment["method"]]  # Settings.
        pid = a.payment["payment_id"]  # Short name for the payment ID.
        latency = cfg["latency_ms"] * (1 + cfg["jitter"] * draw(self.seed, "latency", pid, a.gateway))  # Base delay.
        health = self.health(a.gateway, a.payment, now)  # Technical success chance right now.
        for f in self._match("brownout", a.gateway, now, a.payment):  # Slowdowns.
            latency *= f.value  # Multiply latency.
        a.latency_ms = int(latency)  # Save a whole number of milliseconds.
        if draw(self.seed, "tech", pid, a.gateway) >= health:  # Technical failure: the gateway's fault.
            if draw(self.seed, "stage", pid, a.gateway) < 0.5:  # Half fail before the customer is asked.
                a.state = "FAILED"  # No money moved and the customer saw nothing.
                self._emit(a, "FAILED", now)  # Report it.
                return  # Nothing more happens.
            a.outcome = "FAILED"  # The rest fail after the customer step.
        # Customer draws leave out the gateway, because the customer behaves the same on any gateway.
        elif draw(self.seed, "decline", pid) < method["business_decline"]:  # Customer-side decline.
            a.outcome = "DECLINED"  # For example, insufficient funds.
        elif draw(self.seed, "abandon", pid) >= method["abandon"]:  # The customer does respond.
            a.outcome = "SUCCEEDED"  # Approved. If this check fails, outcome stays None (abandoned).
        low, high = method["customer_ms"]  # Fastest and slowest customer response.
        finish_at = now + a.latency_ms + int(low + (high - low) * draw(self.seed, "customer", pid))  # Finish time.
        a.state, a.prompted = "AWAITING_CUSTOMER", True  # The customer now sees a prompt.
        self._emit(a, "PENDING", now, deliver_after=finish_at)  # Tell the merchant it is pending.
        if a.outcome:  # Abandoned attempts never finish by themselves, so they need a void.
            self._schedule(finish_at, "_finish", a)  # Plan the final result.

    def _arrive(self, a, at):  # A delayed request finally reaches the gateway.
        if a.state == "IN_FLIGHT":  # CAREFUL: a voided request must not be processed.
            self._process(a, at)  # Handle it normally.

    def _finish(self, a, at):  # The customer step ends.
        if a.state != "AWAITING_CUSTOMER":  # It may have been voided meanwhile.
            return  # Nothing to do.
        a.state = a.outcome  # Apply the planned result.
        if a.state == "SUCCEEDED":  # Only success moves money.
            self._record(a, "CHARGE", a.payment["amount_paise"], at)  # Write to the truth ledger.
        self._emit(a, a.state, at)  # Report the final result.

    def _record(self, a, kind, amount_paise, at):  # Adds one row to the true money ledger.
        cfg = self.catalog["gateways"][a.gateway]  # Gateway fee settings.
        # CAREFUL: integer maths only. // rounds down, so money never becomes a decimal.
        fee = amount_paise * cfg["fee_bps"][a.payment["method"]] // 10_000 + cfg["fee_flat_paise"] if kind == "CHARGE" else 0
        self.ledger.append({"kind": kind, "attempt_id": a.id, "payment_id": a.payment["payment_id"], "gateway": a.gateway, "amount_paise": amount_paise, "fee_paise": fee, "at_ms": at})  # Save it.

    def _reply(self, a):  # Builds what the caller is allowed to see.
        result = "TIMEOUT" if a.state == "IN_FLIGHT" else a.state  # Callers cannot see "in flight".
        return {"attempt_id": a.id, "result": result, "processed": a.state != "FAILED", "customer_prompted": a.prompted, "latency_ms": a.latency_ms}  # processed=False promises no money moved.

    @staticmethod
    def _refused(attempt_id, result, code):  # A safe rejection: the gateway did not accept the request.
        return {"attempt_id": attempt_id, "result": result, "code": code, "processed": False, "customer_prompted": False, "latency_ms": 0}

    def _supports(self, gateway, p):  # Can this gateway handle this payment?
        cfg = self.catalog["gateways"][gateway]  # Gateway settings.
        return p["method"] in cfg["success"] and p["bank"] not in cfg["skip_banks"] and (p["method"] != "CARD" or not p.get("network") or p["network"] in cfg["networks"])  # All checks.

    def _overloaded(self, gateway, now, p):  # Counts requests and checks capacity for this second.
        recent = self._recent[gateway]  # This gateway's recent request times.
        while recent and recent[0] <= now - 1_000:  # Drop anything older than 1 second.
            recent.popleft()  # Remove it.
        recent.append(now)  # Count this request.
        return any(len(recent) > f.value for f in self._match("overload", gateway, now, p))  # Over capacity?

    def pay(self, gateway, attempt_id, payment, now):  # Handles a new payment request.
        self.advance(now)  # Bring the world up to date.
        key = (gateway, attempt_id)  # The idempotency key on this gateway.
        if key in self.attempts:  # CAREFUL: a repeated ID must never process again.
            return self._reply(self.attempts[key])  # Return its current answer.
        if not self._supports(gateway, payment):  # This gateway cannot handle it.
            return self._refused(attempt_id, "UNSUPPORTED", "NOT_SUPPORTED")  # Refuse.
        if self._match("outage", gateway, now, payment) or self._match("flap", gateway, now, payment):  # Gateway down.
            return self._refused(attempt_id, "FAILED", "GATEWAY_DOWN")  # Like connection refused: no record is kept.
        if self._overloaded(gateway, now, payment):  # Too many requests this second.
            return self._refused(attempt_id, "FAILED", "OVERLOADED")  # Refuse safely.
        a = self.attempts[key] = Attempt(attempt_id, gateway, payment, "IN_FLIGHT")  # Create the record.
        if self._hit("timeout_in_flight", a, now):  # The request is stuck on the network.
            self._schedule(now + self.catalog["sim"]["in_flight_ms"], "_arrive", a)  # It will land later.
            return self._reply(a)  # Caller sees TIMEOUT.
        self._process(a, now)  # Handle it right away.
        if a.outcome == "SUCCEEDED" and self._hit("timeout_after_charge", a, now):  # Dangerous case.
            self._finish(a, now)  # Money moves immediately.
            return {**self._reply(a), "result": "TIMEOUT"}  # But the caller only sees a timeout.
        return self._reply(a)  # Normal answer.

    def status(self, gateway, attempt_id, now):  # Answers "what happened to this attempt?"
        self.advance(now)  # Bring the world up to date.
        a = self.attempts.get((gateway, attempt_id))  # Look it up.
        hidden = a is None or a.state == "IN_FLIGHT"  # CAREFUL: "not found" does not mean "safe".
        return {"attempt_id": attempt_id, "result": "NOT_FOUND" if hidden else a.state}  # Answer.

    def void(self, gateway, attempt_id, now):  # Blocks an attempt forever, if money has not moved.
        self.advance(now)  # Bring the world up to date.
        a = self.attempts.get((gateway, attempt_id))  # Look it up.
        if a is None:  # The void reached the gateway before the payment.
            self.attempts[(gateway, attempt_id)] = Attempt(attempt_id, gateway, None, "VOIDED")  # Leave a tombstone.
            return {"attempt_id": attempt_id, "result": "VOIDED"}  # Later arrivals will be refused.
        if a.state == "SUCCEEDED":  # Too late: money already moved.
            return {"attempt_id": attempt_id, "result": "ALREADY_SUCCEEDED"}  # Caller must treat it as success.
        if a.state in ("IN_FLIGHT", "AWAITING_CUSTOMER") and not self._hit("void_bug", a, now):  # A healthy void.
            a.state = "VOIDED"  # Scheduled events will now do nothing.
        elif a.state in ("IN_FLIGHT", "AWAITING_CUSTOMER"):  # Bug simulation.
            return {"attempt_id": attempt_id, "result": "VOIDED"}  # It lies: the attempt can still charge.
        return {"attempt_id": attempt_id, "result": a.state}  # Report the state.

    def reverse(self, gateway, attempt_id, now):  # Returns the money from a successful charge.
        self.advance(now)  # Bring the world up to date.
        a = self.attempts.get((gateway, attempt_id))  # Look it up.
        if a is None or a.state not in ("SUCCEEDED", "REVERSED"):  # Nothing to reverse.
            return {"attempt_id": attempt_id, "result": "NOT_REVERSIBLE"}  # Refuse.
        if a.state == "SUCCEEDED":  # CAREFUL: only the first reversal moves money.
            a.state = "REVERSED"  # A retry now does nothing.
            self._record(a, "REVERSAL", -a.payment["amount_paise"], now)  # Add a negative ledger row.
        return {"attempt_id": attempt_id, "result": "REVERSED"}  # Same answer on every retry.

    def truth(self):  # The hidden true record, used only by the audit.
        net = defaultdict(int)  # Net money taken per payment.
        for row in self.ledger:  # Go through every money movement.
            net[row["payment_id"]] += row["amount_paise"]  # Charges add and reversals subtract.
        return {"ledger": self.ledger, "net_paise": dict(net)}  # Return both views.
