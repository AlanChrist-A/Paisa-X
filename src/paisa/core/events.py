"""
WHAT: the event envelope, topic names and versioned event schemas.
WHY: every producer and consumer must agree on what an event looks like.
USED BY: services/outbox.py and pipeline/event_check.py.
SAFE TO CHANGE: add a NEW version, or add fields. CAREFUL: never remove or rename a field
               in an existing version. tests/event_schemas_v1.json fails the tests if you do.
"""

import uuid  # Creates event IDs.

LIFECYCLE = "paisa.payment.lifecycle.v1"  # Payment and attempt state changes.
DECISIONS = "paisa.router.decisions.v1"  # Routing decisions and their reasons.
DLQ = "paisa.dlq.v1"  # Dead-letter topic: bad events kept for inspection.

ENVELOPE = ("event_id", "event_type", "schema_version", "occurred_at", "recorded_at", "payment_id", "attempt_id", "payload")  # Fields every event has.

SCHEMAS = {  # (event type, version) -> required payload fields.
    ("payment.created", 1): ("amount_paise", "method", "bank"),
    ("payment.state_changed", 1): ("from_state", "to_state"),
    ("attempt.state_changed", 1): ("gateway", "attempt_no", "from_state", "to_state", "failure_class"),
    ("attempt.state_changed", 2): ("gateway", "method", "attempt_no", "from_state", "to_state", "failure_class"),  # Phase 6: adds the method, so features need no lookup.
    ("router.decision", 1): ("gateway", "options", "policy_version", "scores", "explored", "fallback"),
}
TOPIC_OF = {"router.decision": DECISIONS}  # Everything else goes to LIFECYCLE.


def make(event_type, payment_id, attempt_id, payload, now, version=1):  # Builds one event.
    return {
        "event_id": str(uuid.uuid4()),  # Unique, so consumers can remove duplicates.
        "event_type": event_type,  # What happened.
        "schema_version": version,  # Which schema it follows.
        "occurred_at": now.isoformat(),  # When it happened.
        "recorded_at": now.isoformat(),  # When we saved it. Same here; Phase 6 uses the difference for late events.
        "payment_id": str(payment_id),  # Also the Kafka key.
        "attempt_id": str(attempt_id) if attempt_id else None,  # Empty for payment-level events.
        "payload": payload,  # Event details.
    }


def problems(event):  # Lists what is wrong with an event. Empty means valid.
    if not isinstance(event, dict):  # Not even an object.
        return ["not_an_object"]
    found = [f"missing:{f}" for f in ENVELOPE if f not in event]  # Envelope check.
    fields = SCHEMAS.get((event.get("event_type"), event.get("schema_version")))  # Expected payload fields.
    if fields is None:  # Unknown type or version.
        return found + ["unknown_schema"]
    payload = event.get("payload") or {}  # Payload, if any.
    return found + [f"missing:payload.{f}" for f in fields if f not in payload]  # Payload check.
