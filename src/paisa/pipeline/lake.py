"""
WHAT: builds the silver and gold layers from bronze Parquet files with DuckDB, then checks data quality.
WHY: bronze is raw (may hold duplicates). Silver is clean. Gold answers business questions.
     BRONZE -> remove duplicates, one row per payment / attempt -> SILVER -> aggregate -> GOLD
RUN: uv run paisa lake
USES: DuckDB (an in-process SQL engine, like SQLite for analytics) and Postgres for the cross-check.
"""

import os  # Atomic rename.
from pathlib import Path  # File paths.

import duckdb  # Analytics SQL engine.
import psycopg  # Reads the outbox for the cross-check.

from paisa.core.states import ATTEMPT, PAYMENT  # Legal state changes.

SILVER = """
-- One row per event: bronze can hold copies from at-least-once delivery, so keep the first.
CREATE TABLE silver_events AS
SELECT * EXCLUDE (rn, payload), payload::JSON AS payload FROM (
  SELECT *, row_number() OVER (PARTITION BY event_id ORDER BY kafka_offset) AS rn FROM bronze) WHERE rn = 1;

-- One row per attempt with its latest state. Kafka offset orders one payment's events.
CREATE TABLE silver_attempts AS
SELECT attempt_id, any_value(payment_id) AS payment_id,
       arg_max(payload->>'to_state', kafka_offset) AS state,
       any_value(payload->>'gateway') AS gateway, any_value(payload->>'method') AS method
FROM silver_events WHERE event_type = 'attempt.state_changed' GROUP BY attempt_id;

-- One row per payment with its latest state.
CREATE TABLE silver_payments AS
SELECT c.payment_id, CAST(c.payload->>'amount_paise' AS BIGINT) AS amount_paise, c.payload->>'method' AS method,
       coalesce(s.state, 'CREATED') AS state
FROM silver_events c
LEFT JOIN (SELECT payment_id, arg_max(payload->>'to_state', kafka_offset) AS state
           FROM silver_events WHERE event_type = 'payment.state_changed' GROUP BY payment_id) s USING (payment_id)
WHERE c.event_type = 'payment.created';
"""

GOLD = """
-- Same rule as core/features.py, in batch form: the gateway's first answer, declines ignored, by event-time minute.
CREATE TABLE gold_health AS
SELECT payload->>'gateway' AS gateway, payload->>'method' AS method,
       CAST(floor(epoch(CAST(occurred_at AS TIMESTAMPTZ)) / 60) * 60 AS BIGINT) AS minute,
       count(*) FILTER (WHERE payload->>'to_state' IN ('SUCCEEDED', 'AWAITING_CUSTOMER')) AS ok,
       count(*) FILTER (WHERE payload->>'to_state' NOT IN ('SUCCEEDED', 'AWAITING_CUSTOMER')) AS fail
FROM silver_events
WHERE event_type = 'attempt.state_changed' AND schema_version = 2 AND payload->>'from_state' = 'INITIATED'
  AND payload->>'to_state' <> 'DECLINED' AND coalesce(payload->>'failure_class', '') <> 'NOT_SUPPORTED'
GROUP BY ALL;

-- Success rate per gateway and method, over finished attempts.
CREATE TABLE gold_success AS
SELECT gateway, method, count(*) AS attempts, count(*) FILTER (WHERE state = 'SUCCEEDED') AS succeeded,
       round(count(*) FILTER (WHERE state = 'SUCCEEDED') / count(*), 4) AS success_rate
FROM silver_attempts GROUP BY ALL ORDER BY gateway, method;
"""

TRANSITIONS = "SELECT DISTINCT payload->>'from_state', payload->>'to_state' FROM silver_events WHERE event_type = ?"  # State changes of one kind.


def build(data_dir, database_url):  # Builds silver and gold, saves gold, and returns a quality report.
    data_dir = Path(data_dir)  # Accept text or a path.
    con = duckdb.connect()  # In-memory DuckDB.
    bronze = (data_dir / "bronze" / "**" / "*.parquet").as_posix()  # Only finished files; .tmp files are skipped.
    con.read_parquet(bronze, hive_partitioning=True).create_view("bronze")  # Bronze as a SQL view.
    con.execute(SILVER)  # Clean layer.
    con.execute(GOLD)  # Business layer.
    one = lambda sql: con.execute(sql).fetchall()[0][0]  # Helper: one number. fetchall() finishes the result, which DuckDB 2 requires.
    illegal = sum(1 for kind, rules in (("attempt.state_changed", ATTEMPT), ("payment.state_changed", PAYMENT))  # Changes the state machine forbids.
                  for a, b in con.execute(TRANSITIONS, [kind]).fetchall() if a is not None and b not in rules.get(a, ()))
    with psycopg.connect(database_url) as pg:  # Postgres is the source of truth.
        outbox = {r[0] for r in pg.execute("SELECT event_id::text FROM outbox").fetchall()}
    silver_ids = {r[0] for r in con.execute("SELECT event_id FROM silver_events").fetchall()}
    report = {
        "bronze_rows": one("SELECT count(*) FROM bronze"),
        "bronze_duplicates": one("SELECT count(*) FROM bronze") - len(silver_ids),  # Allowed: at-least-once copies.
        "silver_payments": one("SELECT count(*) FROM silver_payments"),
        "silver_attempts": one("SELECT count(*) FROM silver_attempts"),
        "violations": {  # Every one of these must be 0.
            "postgres_events_missing_from_bronze": len(outbox - silver_ids),
            "amounts_not_positive": one("SELECT count(*) FROM silver_payments WHERE amount_paise <= 0"),
            "events_without_payment": one("SELECT count(*) FROM silver_events WHERE payment_id NOT IN (SELECT payment_id FROM silver_payments)"),
            "illegal_transitions": illegal,
            "gold_not_matching_silver": abs(one("SELECT coalesce(sum(attempts), 0) FROM gold_success") - one("SELECT count(*) FROM silver_attempts")),
        },
    }
    gold = data_dir / "gold"  # Gold output folder.
    gold.mkdir(parents=True, exist_ok=True)
    for table in ("gold_health", "gold_success"):  # Save each gold table atomically.
        temp = gold / f"{table}.parquet.tmp"  # Write fully first.
        con.table(table).write_parquet(str(temp))
        os.replace(temp, gold / f"{table}.parquet")  # Then publish.
    report["gold_health"] = {(g, m, int(t)): (ok, fail) for g, m, t, ok, fail in con.execute("SELECT * FROM gold_health").fetchall()}  # For the streaming comparison.
    report["gold_success"] = con.execute("SELECT * FROM gold_success").fetchall()  # Readable summary.
    return report
