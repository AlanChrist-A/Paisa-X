"""
WHAT: copies fleet-wide gateway health from Redis into the router's memory every few seconds.
WHY: routing must never wait for a network call. It reads this in-memory copy instead,
     so a slow or dead Redis cannot slow down or stop a payment.
USED BY: api/app.py, when PAISA_REDIS_URL is set.
"""

import threading  # Runs the refresh in the background.
import time  # Measures staleness.

from paisa.adapters.redis_store import all_health  # Reads Redis.
from paisa.core.features import WINDOW_MINUTES  # How many recent minutes to add up.

EVERY = 2  # CHANGE ME: seconds between refreshes.
STALE = 30  # CHANGE ME: seconds without a good refresh before the snapshot is dropped.


def rates(r, now_seconds):  # {(gateway, method): (ok, total)} over the recent window.
    start = int(now_seconds) // 60 * 60 - (WINDOW_MINUTES - 1) * 60  # First minute in the window.
    out = {}  # Totals.
    for (gateway, method, minute), (ok, fail) in all_health(r).items():  # Every bucket.
        if minute >= start:  # Recent enough.
            old_ok, old_total = out.get((gateway, method), (0, 0))  # Running totals.
            out[(gateway, method)] = (old_ok + ok, old_total + ok + fail)  # Add this minute.
    return out


def start(router, r, clock):  # Starts the background refresh. Returns an Event that stops it.
    stop = threading.Event()  # Set this to stop.

    def loop():  # Runs until stopped.
        last_good = time.monotonic()  # When Redis last answered.
        while not stop.wait(EVERY):  # Sleep, then refresh.
            try:
                router.shared = rates(r, clock().timestamp())  # Replace the whole dictionary at once.
                last_good = time.monotonic()
            except Exception:  # Redis is down or slow. Payments are unaffected.
                if time.monotonic() - last_good > STALE:  # Old numbers could mislead routing.
                    router.shared = {}  # The router falls back to its own local results.

    threading.Thread(target=loop, daemon=True, name="health-snapshot").start()  # Daemon: never blocks shutdown.
    return stop
