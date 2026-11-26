"""
WHAT: reads every event in Kafka and compares it with the outbox in Postgres.
WHY: proves no event was lost, duplicates are only extra copies, and each payment's events are in order.
     Invalid events go to the dead-letter topic.
USES: the Kafka adapter and event schemas.
RUN: uv run paisa event-check
"""

import json  # Reads event bytes.
import uuid  # Unique consumer group, so every check starts from the beginning.

import psycopg  # Reads the outbox.
from confluent_kafka import OFFSET_BEGINNING, TopicPartition  # Points a reader at one partition's start.

from paisa.adapters import kafka  # Kafka helpers.
from paisa.core.events import DECISIONS, DLQ, LIFECYCLE, problems  # Topics and schema check.


def read_all():  # Reads every event that exists right now, partition by partition.
    consumer = kafka.consumer(f"event-check-{uuid.uuid4()}")  # Throwaway group name; offsets are never committed.
    topics = consumer.list_topics(timeout=10).topics  # Partition list of each topic.
    parts = [TopicPartition(t, p, OFFSET_BEGINNING) for t in (LIFECYCLE, DECISIONS) for p in topics[t].partitions]  # Start at the oldest event.
    end = {(tp.topic, tp.partition): consumer.get_watermark_offsets(tp, timeout=10)[1] for tp in parts}  # Offset after the newest event.
    todo = {k for k, offset in end.items() if offset > 0}  # Partitions that have events.
    consumer.assign(parts)  # Read these partitions directly, with no group join delay.
    messages, idle = [], 0  # Everything read; empty rounds in a row.
    try:
        while todo and idle < 6:  # Stop when every partition is read, or nothing arrives for 30 s.
            batch = consumer.consume(500, timeout=5)  # Next events.
            idle = 0 if batch else idle + 1  # Count quiet rounds.
            for m in batch:  # Each event.
                if m.error():  # Transport notice, not an event.
                    continue
                messages.append(m)  # Keep it.
                if m.offset() >= end[(m.topic(), m.partition())] - 1:  # Reached this partition's end.
                    todo.discard((m.topic(), m.partition()))
    finally:
        consumer.close()  # Release the connection.
    return messages


def check(database_url):  # Returns counts. Missing and out_of_order must be 0.
    with psycopg.connect(database_url) as conn:  # Postgres is the source of truth.
        rows = conn.execute("SELECT event_id::text, id, published_at IS NOT NULL FROM outbox").fetchall()
    position = {e: i for e, i, _ in rows}  # Event ID -> outbox order. Kafka may also hold other databases' events; those are ignored.
    published = {e for e, _, done in rows if done}  # Events the relay marked as sent.
    seen, order, duplicates, invalid = set(), {}, 0, 0  # Event IDs, first-seen order per payment, counters.
    dlq = kafka.producer()  # Sends bad events away.
    for m in read_all():  # Kafka order within each partition.
        try:
            event = json.loads(m.value())  # Parse.
        except ValueError:  # Not JSON.
            event = None
        if problems(event):  # Bad event: keep it for inspection, do not use it.
            dlq.produce(DLQ, key=m.key(), value=m.value())
            invalid += 1
        elif event["event_id"] not in position:  # Belongs to another database.
            continue
        elif event["event_id"] in seen:  # A repeat from an at-least-once resend.
            duplicates += 1
        else:  # First copy: record its position for the order check.
            seen.add(event["event_id"])
            # Kafka keeps order only inside one partition, so compare per topic and payment.
            order.setdefault((m.topic(), event["payment_id"]), []).append(position[event["event_id"]])
    dlq.flush(10)  # Make sure dead letters are saved.
    out_of_order = sum(1 for ids in order.values() for a, b in zip(ids, ids[1:]) if a > b)  # A later event arrived before an earlier one.
    return {"outbox_rows": len(rows), "unpublished": len(rows) - len(published), "in_kafka": len(seen), "missing": len(published - seen), "duplicates": duplicates, "out_of_order": out_of_order, "sent_to_dlq": invalid}
