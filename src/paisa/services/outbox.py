"""
WHAT: saves an event to the outbox table.
WHY: writing to Kafka directly could lose an event if we crash after the database commit.
     Saving it in the same transaction as the state change means both happen, or neither.
USED BY: services/payments.py and services/settlement.py.
CAREFUL: always call inside the caller's transaction, AFTER locking the payment row.
         That lock keeps one payment's events in order.
"""

from psycopg.types.json import Jsonb  # Stores the event as JSONB.

from paisa.core.events import LIFECYCLE, TOPIC_OF, make  # Event rules.


def add(conn, event_type, payment_id, attempt_id, payload, now, version=1):  # Saves one event. The relay publishes it later.
    event = make(event_type, payment_id, attempt_id, payload, now, version)  # Build the envelope.
    conn.execute("INSERT INTO outbox (event_id, topic, key, event, created_at) VALUES (%s, %s, %s, %s, %s)", (event["event_id"], TOPIC_OF.get(event_type, LIFECYCLE), event["payment_id"], Jsonb(event), now))  # Same transaction as the change.
