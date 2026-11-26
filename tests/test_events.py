"""
WHAT: tests for event schemas, the outbox and the Outbox Relay.
WHY: an event must exist for every state change, and a Kafka failure must never lose one.
"""

import json  # Reads the frozen schema file.
from datetime import UTC, datetime  # A fixed time for events.
from pathlib import Path  # Finds the frozen schema file.

import pytest  # Test runner.
from psycopg.rows import dict_row  # Rows as dictionaries.
from psycopg_pool import ConnectionPool  # Connection pool for the relay.

from experiments.phase3.soak import World  # Simulated world with a virtual clock.
from paisa.core.events import SCHEMAS, make, problems  # Event rules.
from paisa.workers.relay import KafkaUnavailable, relay_once  # The relay.

NOW = datetime(2026, 1, 1, tzinfo=UTC)  # Fixed time.


def test_old_schema_fields_are_never_removed():  # The "CI compatibility test".
    frozen = json.loads((Path(__file__).parent / "event_schemas_v1.json").read_text())  # Version 1 as first released.
    for event_type, fields in frozen.items():  # Each released event type.
        assert set(fields) <= set(SCHEMAS[(event_type, 1)])  # Removing or renaming breaks old consumers.


def test_valid_and_invalid_events():  # The schema check catches broken events.
    good = make("payment.state_changed", "p1", None, {"from_state": "CREATED", "to_state": "PROCESSING"}, NOW)  # Valid.
    assert problems(good) == []
    assert problems({**good, "payload": {"to_state": "X"}}) == ["missing:payload.from_state"]  # Missing field.
    assert problems({**good, "schema_version": 99}) == ["unknown_schema"]  # Unknown version.
    assert problems("junk") == ["not_an_object"]  # Not an event at all.


def test_every_state_change_has_its_event(database_url, merchant, db):  # Invariant I6.
    world = World(database_url, merchant["id"], seed=3)  # In-process gateways.
    try:
        for n in range(20):  # A few payments.
            world.pay(f"k{n}")
        world.settle()  # Let everything finish.
    finally:
        world.close()
    count = lambda sql: db.execute(sql).fetchone()["n"]  # Helper: run a COUNT query.
    assert count("SELECT count(*) AS n FROM outbox WHERE event->>'event_type' = 'attempt.state_changed'") == count("SELECT count(*) AS n FROM attempt_transitions")
    assert count("SELECT count(*) AS n FROM outbox WHERE event->>'event_type' = 'payment.created'") == count("SELECT count(*) AS n FROM payments")
    assert count("SELECT count(*) AS n FROM outbox WHERE event->>'event_type' = 'router.decision'") == count("SELECT count(*) AS n FROM routing_decisions")
    assert all(problems(r["event"]) == [] for r in db.execute("SELECT event FROM outbox").fetchall())  # All valid.


class FakeProducer:  # Stands in for Kafka, so this test needs no Docker.
    def __init__(self, up):  # up=False acts like Kafka being down.
        self.up, self.sent = up, []
    def produce(self, topic, key, value, on_delivery):  # Records the event.
        self.sent.append((topic, key, value))
        if not self.up:  # Kafka down: report a delivery failure.
            on_delivery("down", None)
    def flush(self, timeout):  # Nothing left waiting.
        return 0


def test_relay_keeps_events_when_kafka_is_down(database_url, merchant, db):  # No event is lost.
    world = World(database_url, merchant["id"], seed=4)  # Make some events.
    try:
        world.pay("k1")
        world.settle()
    finally:
        world.close()
    total = db.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"]  # Events waiting.
    with ConnectionPool(database_url, min_size=1, max_size=1, kwargs={"row_factory": dict_row}) as pool:
        with pytest.raises(KafkaUnavailable):  # Kafka is down.
            relay_once(pool, FakeProducer(up=False))
        assert db.execute("SELECT count(*) AS n FROM outbox WHERE published_at IS NULL").fetchone()["n"] == total  # Nothing marked.
        up = FakeProducer(up=True)  # Kafka is back.
        assert relay_once(pool, up) == total  # Everything sent.
        assert relay_once(pool, up) == 0  # Nothing sent twice.
    keys = [k for _, k, _ in up.sent]  # Kafka keys used.
    assert set(keys) == {str(world.payment_id)}  # Keyed by payment, so order is kept.
