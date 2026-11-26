"""
WHAT: the Outbox Relay. Copies committed events from the outbox table to Kafka.
WHY: payments never wait for Kafka. If Kafka is down, events simply wait in Postgres.
USES: the Kafka adapter and the outbox table.
RUN: uv run paisa relay
CAREFUL: delivery is at-least-once. A crash after sending but before marking sends the
         batch again, so consumers must remove duplicates by event_id.
"""

import json  # Turns events into bytes.
import logging  # Warning level for logs.
import os  # Reads the database address.
import time  # Pauses between rounds.

from psycopg.rows import dict_row  # Rows as dictionaries.
from psycopg_pool import ConnectionPool  # Reusable connections.

from paisa import observability as obs  # Logs and metrics.
from paisa.adapters import kafka, postgres  # Kafka and database helpers.
from paisa.adapters.failpoints import crash_if  # Test-only crash points.
from paisa.services.settlement import utc_now  # Real clock.

METRICS_PORT = int(os.getenv("PAISA_RELAY_METRICS_PORT", "8002"))  # CHANGE ME: where Prometheus reads the relay.
BATCH = 500  # CHANGE ME: events per round.
RELAY_LOCK = 5_005  # Advisory lock number. CAREFUL: only one relay may run at a time, or order could break.


class KafkaUnavailable(Exception):  # Kafka did not confirm every event.
    pass


def relay_once(pool, producer, clock=utc_now):  # Publishes one batch. Returns how many were published.
    with pool.connection() as conn:  # One transaction. An error rolls back, so nothing is marked.
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (RELAY_LOCK,)).fetchone()["ok"]:  # Another relay is busy.
            return 0  # Let it work.
        rows = conn.execute("SELECT id, topic, key, event FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT %s", (BATCH,)).fetchall()  # Oldest first.
        if not rows:  # Nothing to do.
            return 0
        failed = []  # Delivery errors reported by Kafka.
        for r in rows:  # Queue every event.
            producer.produce(r["topic"], key=r["key"], value=json.dumps(r["event"]).encode(), on_delivery=lambda err, msg: failed.append(err) if err else None)
        # Wait longer than message.timeout.ms, so every event is either confirmed or failed.
        if producer.flush(15) or failed:  # Something was not confirmed.
            raise KafkaUnavailable(f"{len(failed)} failed")  # Roll back: these rows are sent again later.
        crash_if("relay_after_send")  # Crash test point: sent to Kafka but not yet marked.
        conn.execute("UPDATE outbox SET published_at = %s WHERE id = ANY(%s)", (clock(), [r["id"] for r in rows]))  # Mark as published.
    return len(rows)  # Report progress.


def main():  # Runs the relay until stopped.
    obs.setup("relay", METRICS_PORT)  # JSON logs, and /metrics on its own port.
    kafka.ensure_topics()  # Create topics if needed.
    producer = kafka.producer()  # One producer for the whole run.
    with ConnectionPool(os.getenv("DATABASE_URL") or postgres.url(), min_size=1, max_size=1, kwargs={"row_factory": dict_row}) as pool:  # One connection is enough.
        while True:  # Forever.
            try:
                sent = relay_once(pool, producer)  # One round.
            except KafkaUnavailable as error:  # Kafka is down. Payments keep working; events wait.
                obs.log("kafka.unavailable", logging.WARNING, error=str(error))  # Visible in logs.
                sent = 0
            obs.EVENTS_RELAYED.inc(sent)  # Progress for dashboards.
            if not sent:  # Nothing published this round.
                with pool.connection() as conn:  # Idle moment: measure the backlog.
                    obs.OUTBOX_WAITING.set(conn.execute("SELECT count(*) AS n FROM outbox WHERE published_at IS NULL").fetchone()["n"])
                time.sleep(0.5)  # Avoid a busy loop.
