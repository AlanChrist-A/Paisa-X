"""
WHAT: verifies gateway webhooks and stores them in an inbox.
WHY: webhooks can be forged, repeated or late, so we check them and reply fast.
USES: Postgres.
USED BY: api/app.py. The Recovery Worker processes stored webhooks later.
SAFE TO CHANGE: the time tolerance.
"""

import hashlib  # Provides SHA-256.
import hmac  # HMAC = a signature proving who sent a message and that it was not changed.
import json  # Reads the webhook body.
import uuid  # Validates attempt IDs.
from typing import Literal  # Restricts a field to fixed values.

from psycopg.types.json import Jsonb  # Stores the full event as JSONB.
from pydantic import BaseModel, ValidationError  # Validates the event shape.

TOLERANCE_SECONDS = 300  # CHANGE ME: oldest accepted signature. Lower blocks replays sooner, but rejects delayed sends.


class WebhookEvent(BaseModel):  # The shape of a valid webhook.
    event_id: str  # The gateway's unique event ID.
    gateway: str  # Sending gateway.
    attempt_id: uuid.UUID  # Attempt it describes.
    status: Literal["PENDING", "SUCCEEDED", "DECLINED", "FAILED"]  # Reported status.


def valid_signature(secret, timestamp, raw, signature, now_seconds):  # Checks authenticity and age.
    try:  # The timestamp may be missing or malformed.
        fresh = abs(now_seconds - int(timestamp)) <= TOLERANCE_SECONDS  # Blocks replay of old messages.
    except ValueError:  # Not a number.
        return False  # Reject it.
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()  # Recalculate.
    return fresh and hmac.compare_digest(expected, signature)  # CAREFUL: constant-time comparison resists timing attacks.


def store(conn, event):  # Saves one event. A duplicate is safely ignored.
    conn.execute("INSERT INTO webhook_inbox (gateway, gateway_event_id, attempt_id, status, payload) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (event["gateway"], event["event_id"], event["attempt_id"], event["status"], Jsonb(event)))


def receive(pool, gateway, raw, timestamp, signature, secret, now_seconds):  # Handles one request and returns its HTTP code.
    if not secret:  # CAREFUL: fail closed, so an unset secret can never mean "accept everything".
        return 503  # Service is not configured.
    if not valid_signature(secret, timestamp, raw, signature, now_seconds):  # Forged, changed or too old.
        return 401  # Refuse it.
    try:  # The signed body should still be checked.
        event = json.loads(raw)  # Parse JSON.
        parsed = WebhookEvent.model_validate(event)  # Validate its fields.
    except (ValueError, ValidationError):  # Bad content.
        return 400  # Refuse it.
    if parsed.gateway != gateway:  # A gateway may report only its own events.
        return 400  # Refuse it.
    with pool.connection() as conn:  # Save it durably.
        store(conn, event)  # Insert into the inbox.
    return 200  # Reply quickly. The worker does the rest.
