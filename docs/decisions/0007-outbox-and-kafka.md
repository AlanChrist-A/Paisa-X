# Decision 0007: Transactional outbox and Kafka

**Decision:** every state change saves its event in an `outbox` table in the **same Postgres transaction**. A separate Outbox Relay copies those events to Kafka and marks them as published. Delivery is at-least-once, and consumers remove duplicates by `event_id`.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| Outbox instead of writing to Kafka directly | A crash between "save" and "send" can never lose an event or create an event without its change | Events reach Kafka a little later |
| Payments never call Kafka | Kafka being down cannot stop payments | Events pile up in Postgres until Kafka returns |
| Idempotent producer, `acks=all` | Kafka drops the producer's own retry duplicates and keeps order | Slightly slower sends |
| Mark as published only after Kafka confirms | Nothing is marked as sent unless it really was | A crash after sending resends the batch, so duplicates are possible |
| One relay at a time (advisory lock) | Two relays could send one payment's events out of order | No parallel relays; fine at this size |
| Key = `payment_id` | One payment's events go to one partition, so they stay in order | Order holds only within one topic |
| Kafka in Docker (your choice), Postgres stays portable | Standard way to run Kafka, and it matches the AWS phase | Docker Desktop itself uses RAM |
| Schemas in code, plus a frozen v1 file checked by tests | Removing or renaming a field fails the tests | No Schema Registry, to save RAM |
| Dead-letter topic `paisa.dlq.v1` | A bad event is kept for inspection instead of crashing a consumer | Someone must read it |

## Measured

Reproduce with `docker compose up -d`, then `uv run python -m experiments.phase5.drills`.

| Drill | Result | Target |
|---|---:|---:|
| 1,000 payments with Kafka **stopped** | All finished (838 succeeded, 162 failed safely); 8,218 events waited in Postgres | Payments continue |
| Catch-up after Kafka started again | 11.8 seconds, nothing left waiting | Events catch up |
| Kafka restarted with events waiting | Caught up | No loss |
| Relay killed after sending, before marking | 1,641 events left unmarked, then resent by a new relay | No loss |
| Events in Postgres vs Kafka (12,326 total) | **0 missing**, 500 duplicates (the resent batch), **0 out of order** | 0 missing, 0 out of order |
| Kafka RAM | About 318 MB of the 800 MB cap | Under 800 MB |
| Free laptop RAM with Docker running | About 2.3 GB | Recorded |

All 151 tests pass.

## Mistakes caught during testing

1. The first checker read **0 events** from Kafka and reported everything as missing. It gave up during the consumer-group join. It now reads every partition directly until its last offset.
2. The order check then reported 1,500 events out of order. It compared events across **two topics**, where Kafka promises no order. It now compares order per payment **within each topic**, and reports 0.

## Known limits

- One Kafka broker, so there is only one copy of each event. Production uses 3 brokers.
- `trace_id` from the planned envelope is not added yet; it arrives with tracing in Phase 9.
- `occurred_at` and `recorded_at` are the same for now. Phase 6 uses the difference for late events.
- The relay holds its database transaction while waiting for Kafka (up to 15 seconds). It locks only outbox rows, so payments are not blocked.
- Kafka data lives inside the container. `docker compose down` deletes it, but Postgres still has every event.
