"""
WHAT: the Feature Worker. Reads payment events from Kafka and keeps live gateway health in Redis.
WHY: gives every process the same up-to-date health numbers.
RUN: uv run paisa features            (normal)
     uv run paisa features-rebuild    (replays Kafka from the start, after Redis was wiped)
CAREFUL: Kafka offsets are committed only AFTER Redis is updated. A crash in between replays
         those events, and the Redis script ignores ones it already counted.
"""

import json  # Reads event bytes.
from collections import Counter  # Counts outcomes.

from confluent_kafka import OFFSET_BEGINNING, TopicPartition  # Lets a rebuild start from the oldest event.

from paisa.adapters import kafka, redis_store  # Kafka and Redis helpers.
from paisa.core.events import LIFECYCLE, problems  # Topic and schema check.
from paisa.core.features import LATENESS, signal  # Event -> health signal.

RESULT = {0: "duplicate", 1: "counted", 2: "late"}  # Names for the Redis script's answers.


def handle(r, value, partition=0):  # Processes one event. Returns what happened to it.
    try:
        event = json.loads(value)  # Parse.
    except ValueError:
        return "invalid"
    if problems(event):  # Broken events are left to the dead-letter check.
        return "invalid"
    s = signal(event)  # Health signal, if any.
    if s is None:  # Says nothing about gateway health.
        return "ignored"
    gateway, method, minute, at, ok = s  # Unpack.
    return RESULT[redis_store.apply(r, event["event_id"], gateway, method, minute, at, ok, LATENESS, partition)]  # Atomic, duplicate-safe update.


def run(group="feature-worker", rebuild=False, idle_polls=None, r=None):  # Runs forever, or until idle_polls empty polls in a row.
    r = r or redis_store.client()  # Redis connection.
    consumer = kafka.consumer(group)  # Kafka consumer in a group, so offsets are remembered.
    rewind = lambda c, parts: c.assign([TopicPartition(p.topic, p.partition, OFFSET_BEGINNING) for p in parts])  # Start from the oldest event.
    consumer.subscribe([LIFECYCLE], **({"on_assign": rewind} if rebuild else {}))  # Normal runs continue where they stopped.
    totals, quiet = Counter(), 0  # Outcome counts and empty polls in a row.
    try:
        while idle_polls is None or quiet < idle_polls:  # Keep going.
            batch = [m for m in consumer.consume(500, timeout=2) if not m.error()]  # Next events.
            quiet = 0 if batch or not consumer.assignment() else quiet + 1  # Not idle until Kafka has given us partitions.
            for m in batch:  # Each event.
                totals[handle(r, m.value(), m.partition())] += 1  # Update Redis.
            if batch:  # Only after Redis has every update.
                consumer.commit(asynchronous=False)  # Remember our place in Kafka.
    finally:
        consumer.close()  # Leave the group cleanly.
    return dict(totals)


def main():  # `paisa features`.
    run()


def rebuild_main():  # `paisa features-rebuild`.
    run(rebuild=True)
