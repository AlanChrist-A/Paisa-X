"""
WHAT: the HTTP client Paisa-X uses to call a gateway.
WHY: network errors have different safety meanings, and this file keeps that detail in one place.
USES: httpx.
USED BY: services/payments.py and workers/recovery.py.
SAFE TO CHANGE: add a new gateway call as one small function.
"""

import httpx  # Makes HTTP requests.


def pay(client, gateway, attempt_id, payload):  # Sends one payment attempt.
    try:  # Network calls can fail in several ways.
        reply = client.post(f"/sim/{gateway}/pay", params={"attempt_id": attempt_id}, json=payload)  # attempt_id makes retries safe.
        return reply.json()  # Return the gateway's answer.
    except (httpx.ConnectError, httpx.ConnectTimeout):  # The connection never opened.
        return {"result": "FAILED", "code": "CONNECT_FAILED", "processed": False}  # Safe: the request was never sent.
    except (httpx.HTTPError, ValueError):  # Timed out, lost mid-request, or returned unreadable data.
        return None  # CAREFUL: None means "unknown", because money may have moved.


def _read(send):  # Runs a recovery call. Failure here simply means "try again later".
    try:  # The gateway may be unreachable.
        return send().json()  # Return its answer.
    except (httpx.HTTPError, ValueError):  # No usable answer.
        return None  # The worker will check again.


def status(client, gateway, attempt_id):  # Asks what happened to an attempt.
    return _read(lambda: client.get(f"/sim/{gateway}/status/{attempt_id}"))  # Safe to repeat.


def void(client, gateway, attempt_id):  # Blocks an attempt permanently.
    return _read(lambda: client.post(f"/sim/{gateway}/void/{attempt_id}"))  # Safe to repeat.


def reverse(client, gateway, attempt_id):  # Returns a charge.
    return _read(lambda: client.post(f"/sim/{gateway}/reverse/{attempt_id}"))  # Safe to repeat, because the attempt ID is stable.
