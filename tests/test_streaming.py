"""
WHAT: tests for health signals, shared routing health, Redis updates, the Parquet Sink and the data lake.
WHY: streaming and batch must count the same thing, duplicates must count once, and a crash
     mid-write must never corrupt history.
NOTE: the Redis test is skipped when Redis is not running (docker compose up -d).
"""

import json  # Reads stored events.
from datetime import UTC, datetime  # Fixed event times.

import pytest  # Test runner.

from experiments.phase3.soak import World  # Simulated world with a virtual clock.
from paisa.adapters import redis_store  # Redis helpers.
from paisa.core.events import make  # Builds events.
from paisa.core.features import signal  # Event -> health signal.
from paisa.core.routing.policies import RollingPolicy  # Uses success rates.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.pipeline.lake import build  # Silver and gold.
from paisa.workers.parquet_sink import write  # Bronze writer.

T = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)  # Fixed time, 30 s into a minute.


def attempt(from_state, to_state, code=None, version=2):  # Builds one attempt event.
    return make("attempt.state_changed", "p1", "a1", {"gateway": "alpha", "method": "UPI", "attempt_no": 1, "from_state": from_state, "to_state": to_state, "failure_class": code}, T, version)


def test_only_gateway_answers_are_health_signals():
    assert signal(attempt("INITIATED", "SUCCEEDED")) == ("alpha", "UPI", int(T.timestamp()) // 60 * 60, int(T.timestamp()), True)  # Healthy.
    assert signal(attempt("INITIATED", "UNKNOWN"))[-1] is False  # Timeout = unhealthy.
    assert signal(attempt("INITIATED", "DECLINED")) is None  # Customer refusal: not the gateway's fault.
    assert signal(attempt("AWAITING_CUSTOMER", "SUCCEEDED")) is None  # Customer outcome, not a gateway answer.
    assert signal(attempt("INITIATED", "SUCCEEDED", version=1)) is None  # Old events lack the method.


def test_router_uses_shared_health_only_with_enough_evidence():
    router = Router(RollingPolicy(explore=0))  # No exploration, so results are predictable.
    router.shared = {("alpha", "UPI"): (1, 40), ("beta", "UPI"): (39, 40)}  # Fleet says alpha is failing.
    assert router.decide("p", "UPI", ["alpha", "beta"], 0).chosen == "beta"  # Shared health is used.
    router.shared = {("alpha", "UPI"): (0, 5)}  # Too few results to trust.
    assert router.decide("p", "UPI", ["alpha", "beta"], 0).chosen == "alpha"  # Back to local (both unknown, priority order).
    router.shared = {}  # Redis wiped.
    assert router.decide("p", "UPI", ["alpha", "beta"], 0).chosen == "alpha"  # Routing still works.


def redis_or_skip():  # Returns a Redis client, or skips the test.
    r = redis_store.client()
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis is not running")
    return r


def test_redis_counts_once_and_flags_late_events():
    r = redis_or_skip()
    r.flushdb()  # CAREFUL: local test Redis only.
    m = 1_000_020  # A whole-minute timestamp... used as the bucket.
    assert redis_store.apply(r, "e1", "alpha", "UPI", 1_000_020, 1_000_030, True, 120) == 1  # Counted.
    assert redis_store.apply(r, "e1", "alpha", "UPI", 1_000_020, 1_000_030, True, 120) == 0  # Same event again: ignored.
    assert redis_store.apply(r, "e2", "alpha", "UPI", 1_000_500, 1_000_500, False, 120) == 1  # Newer event moves the watermark to 1,000,380.
    assert redis_store.apply(r, "e3", "alpha", "UPI", m, 1_000_100, True, 120) == 2  # Older than the watermark: late.
    assert redis_store.all_health(r) == {("alpha", "UPI", 1_000_020): (1, 0), ("alpha", "UPI", 1_000_500): (0, 1)}
    r.flushdb()  # Leave it clean.


def test_lake_from_bronze_with_duplicates_and_a_crash_leftover(database_url, merchant, db, tmp_path):
    world = World(database_url, merchant["id"], seed=5)  # Real payment flow, in-process gateways.
    try:
        for n in range(15):
            world.pay(f"k{n}")
        world.settle()
    finally:
        world.close()
    events = [r["event"] for r in db.execute("SELECT event FROM outbox ORDER BY id").fetchall()]  # What the relay would send.
    rows = [("paisa.payment.lifecycle.v1", {**{k: e[k] for k in ("event_id", "event_type", "schema_version", "payment_id", "attempt_id", "occurred_at", "recorded_at")}, "payload": json.dumps(e["payload"]), "kafka_partition": 0, "kafka_offset": i}) for i, e in enumerate(events)]
    write(rows, tmp_path)  # Bronze.
    write([(t, {**r, "kafka_offset": r["kafka_offset"] + 10_000}) for t, r in rows[:10]], tmp_path)  # A resent batch: 10 duplicates.
    (tmp_path / "bronze" / "half-written.parquet.tmp").write_bytes(b"garbage")  # A crash leftover.
    report = build(tmp_path, database_url)  # Silver and gold.
    assert report["bronze_duplicates"] == 10  # Copies are seen...
    assert report["silver_payments"] == 15  # ...but removed in silver.
    assert report["violations"] == {k: 0 for k in report["violations"]}  # Every quality check passes.
    assert (tmp_path / "gold" / "gold_success.parquet").exists()  # Gold was saved.
