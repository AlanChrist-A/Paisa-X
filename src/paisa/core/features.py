"""
WHAT: turns one event into one gateway-health signal, using event time.
WHY: the streaming Feature Worker and the batch gold layer must count the same thing,
     so the rule lives in one place (the gold SQL in pipeline/lake.py mirrors it, and the
     Phase 6 drill proves both give the same numbers).
USES: the health rule from failure_policy.py. Pure logic, no database or network.
SAFE TO CHANGE: LATENESS and WINDOW_MINUTES.
"""

from datetime import datetime  # Reads event times.

from paisa.core.failure_policy import health_signal  # Same rule the router uses.

LATENESS = 120  # CHANGE ME: seconds an event may arrive behind the newest one and still count live.
WINDOW_MINUTES = 5  # CHANGE ME: how many recent minutes the routing snapshot adds up.


def signal(event):  # Returns (gateway, method, minute, at_seconds, ok), or None if the event says nothing about health.
    if event.get("event_type") != "attempt.state_changed" or event.get("schema_version") != 2:  # Only v2 attempt events carry the method.
        return None
    p = event["payload"]  # Event details.
    if p["from_state"] != "INITIATED":  # Only the gateway's first answer is a health signal. Later changes come from customers or our worker.
        return None
    ok = health_signal(p["to_state"], p["failure_class"])  # True, False, or None for customer-side outcomes.
    if ok is None:  # A decline is not the gateway's fault.
        return None
    at = int(datetime.fromisoformat(event["occurred_at"]).timestamp())  # EVENT time: when it happened, not when we read it.
    return p["gateway"], p["method"], at // 60 * 60, at, ok  # Minute bucket starts on a whole minute.
