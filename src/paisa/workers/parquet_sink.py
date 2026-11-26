"""
WHAT: the Parquet Sink. Saves every Kafka event into bronze Parquet files, the raw history layer.
WHY: Kafka keeps events for 7 days only. Parquet files are cheap, permanent and fast to analyse.
LAYOUT: data/bronze/topic=.../date=YYYY-MM-DD/hour=HH/part-{partition}-{first offset}.parquet
RUN: uv run paisa sink
CAREFUL: each file is written to a .tmp name, then renamed. A rename is atomic, so a reader
         sees either the whole file or nothing. Offsets are committed only after the rename.
"""

import json  # Reads event bytes.
import os  # Atomic rename and settings.
from pathlib import Path  # File paths.

import pyarrow as pa  # In-memory tables.
import pyarrow.parquet as pq  # Parquet writer.

from paisa.adapters import kafka  # Kafka helpers.
from paisa.adapters.failpoints import crash_if  # Test-only crash points.
from paisa.core.events import DECISIONS, LIFECYCLE  # Topics to save.

DATA_DIR = Path(os.getenv("PAISA_DATA_DIR", "data"))  # CHANGE ME: where history files go.
SCHEMA = pa.schema([  # Column names and types. Same for every file.
    ("event_id", pa.string()), ("event_type", pa.string()), ("schema_version", pa.int32()),
    ("payment_id", pa.string()), ("attempt_id", pa.string()),
    ("occurred_at", pa.string()), ("recorded_at", pa.string()),
    ("payload", pa.string()),  # Kept as JSON text, because each event type has different fields.
    ("kafka_partition", pa.int32()), ("kafka_offset", pa.int64()),  # Where it came from. Orders one payment's events.
])


def row(topic, partition, offset, value):  # Turns one Kafka event into one bronze row.
    e = json.loads(value)  # Parse.
    return topic, {**{k: e.get(k) for k in ("event_id", "event_type", "schema_version", "payment_id", "attempt_id", "occurred_at", "recorded_at")}, "payload": json.dumps(e.get("payload")), "kafka_partition": partition, "kafka_offset": offset}


def write(rows, data_dir=DATA_DIR):  # Writes (topic, row) pairs into partitioned files. Returns files written.
    groups = {}  # (topic, date, hour, partition) -> rows.
    for topic, r in rows:  # Group them.
        groups.setdefault((topic, r["recorded_at"][:10], r["recorded_at"][11:13], r["kafka_partition"]), []).append(r)
    for (topic, date, hour, partition), part in groups.items():  # One file per group.
        folder = Path(data_dir) / "bronze" / f"topic={topic}" / f"date={date}" / f"hour={hour}"  # Hive-style folders.
        folder.mkdir(parents=True, exist_ok=True)  # Create if needed.
        final = folder / f"part-{partition}-{part[0]['kafka_offset']}.parquet"  # Same batch gives the same name.
        temp = final.with_name(final.name + ".tmp")  # Readers only look for *.parquet, so they skip this.
        pq.write_table(pa.Table.from_pylist(part, schema=SCHEMA), temp)  # Write everything first.
        crash_if("sink_before_rename")  # Crash test point: half-done work stays invisible.
        os.replace(temp, final)  # Atomic publish.
    return len(groups)


def run(data_dir=DATA_DIR, group="parquet-sink", idle_polls=None):  # Runs forever, or until idle.
    consumer = kafka.consumer(group)  # Remembers its place in Kafka.
    consumer.subscribe([LIFECYCLE, DECISIONS])  # Save both event topics.
    files, quiet = 0, 0  # Files written; empty polls in a row.
    try:
        while idle_polls is None or quiet < idle_polls:  # Keep going.
            batch = [m for m in consumer.consume(2_000, timeout=2) if not m.error()]  # Up to 2,000 events.
            # Not idle until Kafka has given us partitions. After a crash, Kafka waits up to 45 s
            # for the dead member's session to expire before handing them over.
            quiet = 0 if batch or not consumer.assignment() else quiet + 1
            if batch:  # Save, then commit.
                files += write([row(m.topic(), m.partition(), m.offset(), m.value()) for m in batch], data_dir)
                consumer.commit(asynchronous=False)  # Only after the files exist.
    finally:
        consumer.close()  # Leave the group cleanly.
    return files


def main():  # `paisa sink`.
    run()
