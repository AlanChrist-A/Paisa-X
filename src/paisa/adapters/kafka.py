"""
WHAT: creates Kafka producers, consumers and topics.
WHY: one place holds every Kafka setting, so they are easy to find and change.
USED BY: workers/relay.py and pipeline/event_check.py.
SAFE TO CHANGE: retention and partition counts.
"""

import os  # Reads the Kafka address setting.
import time  # Waits for topic deletion.

from confluent_kafka import Consumer, Producer  # Kafka client.
from confluent_kafka.admin import AdminClient, NewTopic  # Creates topics.

from paisa.core.events import DECISIONS, DLQ, LIFECYCLE  # Topic names.

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "127.0.0.1:9092")  # CHANGE ME: Kafka address.
TOPICS = {LIFECYCLE: 3, DECISIONS: 3, DLQ: 1}  # Topic -> partitions. More partitions = more consumers can share work.
RETENTION = {"retention.ms": str(7 * 24 * 3600 * 1000), "retention.bytes": str(256 * 1024 * 1024)}  # Keep 7 days or 256 MB per partition, so the disk cannot fill.


def producer():  # A producer that never writes a duplicate on its own retries.
    return Producer({
        "bootstrap.servers": BOOTSTRAP,  # Where Kafka is.
        "enable.idempotence": True,  # Kafka drops the producer's own retry duplicates and keeps order.
        "acks": "all",  # Wait until every in-sync copy has the event before calling it saved.
        "linger.ms": 5,  # Wait 5 ms to group events into batches.
        "message.timeout.ms": 10_000,  # Give up on an event after 10 s, so the relay can report Kafka is down.
    })


def consumer(group):  # A consumer that starts from the oldest event and commits manually.
    return Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": group, "auto.offset.reset": "earliest", "enable.auto.commit": False})


def reset_topics():  # Deletes and recreates every topic. CAREFUL: drills only; this erases all Kafka events.
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})  # Admin connection.
    existing = [t for t in TOPICS if t in admin.list_topics(timeout=10).topics]  # Ours that exist.
    for future in admin.delete_topics(existing).values() if existing else []:  # Delete them.
        future.result()
    for _ in range(30):  # Deletion finishes in the background, so wait for it.
        if not set(TOPICS) & set(admin.list_topics(timeout=10).topics):
            break
        time.sleep(1)
    ensure_topics()  # Fresh, empty topics.


def ensure_topics():  # Creates any missing topic. Safe to run many times.
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})  # Admin connection.
    existing = admin.list_topics(timeout=10).topics  # Topics that already exist.
    new = [NewTopic(name, parts, 1, config=RETENTION) for name, parts in TOPICS.items() if name not in existing]  # 1 copy, because there is 1 broker.
    for future in admin.create_topics(new).values() if new else []:  # Wait for each creation.
        future.result()  # Raises if creation failed.
