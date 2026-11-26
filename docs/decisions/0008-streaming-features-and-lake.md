# Decision 0008: Streaming features, Redis and the data lake

**Decision:** a Feature Worker turns Kafka events into live gateway health in Redis. The API copies that health into memory every 2 seconds. A Parquet Sink saves every event as bronze files, and DuckDB builds silver and gold from them. Postgres stays the source of truth; Redis and Parquet can always be rebuilt.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| One health rule in `core/features.py`, mirrored in gold SQL | Streaming and batch must count the same thing | The SQL copy can drift, so the drill compares them |
| Event time, in 1-minute windows | Health belongs to when a payment happened, not when we read it | Needs a watermark and late-event handling |
| One watermark **per Kafka partition** | Partitions are read at different speeds | Slightly more Redis keys |
| Redis Lua script: duplicate check + count + watermark in one step | No half-updates, and a replayed event counts once | Keys must share one slot on a Redis cluster |
| Commit Kafka offsets only after Redis or files are written | A crash replays events instead of losing them | Replays are possible, so every step is duplicate-safe |
| Routing reads an in-memory snapshot, never Redis directly | A slow or dead Redis cannot slow a payment | Snapshot is up to about 2 s old |
| Breakers stay local, only success rates are shared | Breakers must react within seconds | Two processes may open breakers at different times |
| Shared rate used only with at least 20 results | Avoids trusting tiny samples | Small gateways use local rates longer |
| Write `.tmp`, then rename | A reader sees a whole file or nothing | None significant |
| Event schema v2 for attempts adds `method` | Features need it, and v1 stays readable | Two versions to support |
| Redis in Docker with no disk saving, capped at 64 MB | Redis is fast state only, rebuilt from Kafka | A restart empties it until a rebuild |

## Measured

Reproduce with `docker compose up -d`, then `uv run python -m experiments.phase6.drills` (it erases local Kafka and Redis data first).

| Drill (2,000 payments with gateway faults) | Result | Target |
|---|---:|---:|
| Health signals, streaming (Redis) vs batch (gold) | 2,145 vs 2,145, **0 difference** | Match within a stated tolerance |
| Sink killed mid-write | 1 `.tmp` file left, ignored; restart rewrote the batch | No corruption or loss |
| Postgres events missing from bronze | **0** of 16,567 | 0 |
| Quality checks (amounts, orphan events, illegal transitions, gold vs silver) | All 0 | 0 |
| Routing with Redis wiped | Works, using local rates | Payments continue |
| Redis rebuilt from Kafka | Same 2,145 signals, 0 difference | Same numbers |
| RAM | Kafka about 504 MB of 800 MB, Redis about 5 MB of 64 MB | Recorded |

All 155 tests pass.

## Mistakes caught during testing

1. **A restarted sink stopped too early.** After a crash, Kafka waits up to 45 seconds for the dead member's session to end before giving its partitions to the new one. Workers now count idle time only after they have partitions.
2. **47% of events were marked late.** One shared watermark raced ahead while one partition's backlog was read, so the other partitions' events looked late. With one watermark per partition, late events fell to 0 and streaming matches batch exactly.

## Known limits

- Laptop free RAM fell to about 0.9 GB with Docker, Kafka, Redis and the tests running. Docker Desktop's own VM is the largest cost.
- The shared snapshot is off unless `PAISA_REDIS_URL` is set, so the Phase 3-5 drills keep their measured behaviour. Phase 8 compares shared and local routing.
- The Recovery Worker does not start a snapshot yet.
- The worker's own voids are counted as health signals. They are rare, but this is a small difference from what the router sees.
- In live traffic the lateness limit is 120 seconds. Real late events would show as a gap between streaming and batch, and the drill reports it.
