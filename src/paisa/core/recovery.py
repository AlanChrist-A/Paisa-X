"""
WHAT: pure rules for deciding what to do with an unresolved gateway attempt.
WHY: keeping these decisions free of database and network code makes them easy to test and change.
USES: nothing.
USED BY: workers/recovery.py.
SAFE TO CHANGE: anything marked CHANGE ME.
"""

CUSTOMER_WINDOW_SECONDS = {"UPI": 60, "CARD": 75, "NETBANKING": 150}  # CHANGE ME: how long a customer may take.
NOT_FOUND_GRACE_SECONDS = 10  # CHANGE ME: wait this long for a delayed request before voiding it.
MAX_BACKOFF_SECONDS = 30  # CHANGE ME: longest gap between status checks.
RESOLVED = {"SUCCEEDED": "SUCCEEDED", "DECLINED": "DECLINED", "FAILED": "FAILED_SAFE", "VOIDED": "FAILED_SAFE"}  # Clear answers.


def next_delay(checks):  # Exponential backoff: wait 1, 2, 4, 8... seconds.
    return min(2**min(checks, 10), MAX_BACKOFF_SECONDS)  # The inner limit avoids huge numbers.


def next_step(status, method, age_seconds, must_void):  # Returns an attempt state, "VOID" or "WAIT".
    if status in RESOLVED:  # The gateway gave a clear result.
        return RESOLVED[status]  # Record it.
    if status not in ("AWAITING_CUSTOMER", "NOT_FOUND"):  # Anything unexpected.
        return "WAIT"  # CAREFUL: never guess, so check again later.
    limit = CUSTOMER_WINDOW_SECONDS[method] if status == "AWAITING_CUSTOMER" else NOT_FOUND_GRACE_SECONDS  # Time allowed.
    return "VOID" if must_void or age_seconds > limit else "WAIT"  # Block it only when time is up or another attempt won.
