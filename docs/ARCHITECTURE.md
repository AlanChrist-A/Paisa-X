# Paisa-X: Core Architecture and System Design

**Status:** design v1, Nov 23, 2026. No code has been built yet.
**All payments, gateways, banks and customers are simulated.** No real money or real card data is ever used.

Numbers marked **[Target]** are goals we must measure. Numbers marked **[Estimate]** are planning guesses. Neither may go on a resume until measured.

---

## 1. What Paisa-X is

Paisa-X is a **payment orchestrator**. A merchant sends it one payment. Paisa-X chooses the best gateway, runs the payment safely, survives failures, learns from results, and proves what its routing saved.

**The one-line pitch:** *a payment router that learns which gateway is healthy, and can prove it never knowingly charged a customer twice, even while gateways are failing.*

### The three questions the system must answer

| Question | Part of the system that answers it |
|---|---|
| Which gateway should this payment use right now? | Routing engine |
| What is the true state of this payment, even after a crash or timeout? | Payment core + Postgres |
| Did the smart routing really help, and by how much? | Data pipeline + Replay Lab |

### What is in and out of version 1

| In | Out (and why) |
|---|---|
| UPI, cards, netbanking (simulated) | Real gateway connections (needs business accounts) |
| Full payment lifecycle, webhooks, recovery | Real card data (PCI rules, unnecessary risk) |
| Automatic reversal of a duplicate charge | Merchant-requested refunds, partial refunds |
| Learning router + fair evaluation | Settlement files and ledger (that is Hawk X3's job) |
| Kafka, Redis, Postgres, Parquet | Kubernetes, Spark (not justified at this size) |
| Short, on-demand AWS run with Terraform | Always-on hosting, MSK, RDS, EKS (too costly for this project) |
| One live demo page + Grafana | Login, merchant dashboard |

---

## 2. The rules the system must never break (invariants)

An **invariant** is a statement that must always be true. These eight drive the whole design. Each has an automated check.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | One idempotency key creates at most one payment | Database unique constraint |
| I2 | A payment has at most **one active attempt** at any time | Partial unique index |
| I3 | A payment has at most **one winning** successful attempt | Partial unique index |
| I4 | We never try another gateway while an earlier attempt's result is unknown | I2 + failover rules |
| I5 | Only allowed state changes can happen, and a final payment state never changes | State machine + conditional update |
| I6 | Every state change has its event, and no event exists without its state change | Transactional outbox |
| I7 | Money is always whole paise, above zero | `BIGINT` + `CHECK` |
| I8 | After recovery, each customer's **net charge** for a payment is the payment amount or zero | `paisa audit` against gateway truth |

**An honest limit on "no double charge":** no system can fully prevent one when an outside gateway misbehaves. A gateway might report "not found" and then process the payment later. So Paisa-X makes two precise claims:

1. It never **knowingly** starts a second charge while the first is unresolved.
2. If a second charge happens anyway, it **detects and reverses it**, and the audit proves the final net charge is correct.

---

## 3. High-level architecture

```
   Traffic generator / Demo page
              │  POST /v1/payments (with Idempotency-Key)
              ▼
┌───────────────────────────────────────────────┐       ┌──────────────────────┐
│ PAISA API (FastAPI, modular monolith)         │ HTTP  │ GATEWAY SIMULATOR    │
│                                               │──────►│ 5 fake gateways      │
│  Payments ─► Routing engine ─► Gateway client │       │ Faults, delays       │
│     │            ▲ in-memory snapshot         │◄──────│ Signed webhooks      │
│     │            │                            │webhook│ Holds the TRUTH      │
│  Webhook intake  │                            │       └──────────▲───────────┘
└─────┬────────────┼────────────────────────────┘                  │ status check
      │ one        │ refresh every 1s                              │ / void
      │ transaction│                                               │
      ▼            │                              ┌────────────────┴───────────┐
┌───────────────┐  │                              │ RECOVERY WORKER            │
│ POSTGRES      │◄─┼──────────────────────────────│ Resolves unknown attempts  │
│ Source of     │  │                              │ Processes webhooks         │
│ truth + outbox│  │                              │ Reverses duplicate charges │
└─────┬─────────┘  │                              └────────────────────────────┘
      │ poll       │
      ▼            │
┌───────────────┐  │
│ OUTBOX RELAY  │  │
└─────┬─────────┘  │
      ▼            │
┌───────────────────────────────┐
│ KAFKA (one node, KRaft mode)  │
└───┬───────────────────────┬───┘
    ▼                       ▼
┌───────────────┐   ┌─────────────────┐
│ FEATURE WORKER│   │ PARQUET SINK    │
│ Rolling stats │   │ Raw event files │
│ Bandit state  │   └────────┬────────┘
└───┬───────────┘            ▼
    ▼                ┌─────────────────────────────┐
┌───────┐            │ DuckDB: bronze/silver/gold  │
│ REDIS │            │ Quality checks, audit       │
└───────┘            │ Replay Lab                  │
                     └─────────────────────────────┘
```

### Why it is built this way

| Decision | Choice | Why | Rejected option |
|---|---|---|---|
| Service style | **Modular monolith + small workers** | One team, one laptop. Clean module borders without network overhead | Microservices add failure points and RAM with no benefit here |
| Source of truth | **Postgres** | Transactions and constraints protect money | Kafka or Redis as truth: neither enforces these rules |
| Reliable events | **Transactional outbox** | Saving and publishing separately can lose events in a crash | Direct dual-write to Kafka |
| Outbox reader | **Simple polling relay** | Easy to understand. Debezium is saved for Hawk X3 | Debezium now adds RAM and a second new idea |
| Routing path | **In-memory snapshot** | A routing decision needs no network call, so Redis or Kafka being down cannot stop payments | Calling Redis on every payment |
| Gateway simulator | **Separate service** | Paisa-X cannot "cheat" by reading the truth. It sees only what a real gateway would show | Function calls inside the API |
| Core logic | **Functional core, imperative shell** | Routing and state logic are pure functions, so live system and Replay Lab run the *same* code | Separate replay logic could drift from production logic |

**Dependency rule:** if Kafka, Redis or the workers die, **payments continue**. Only Postgres and at least one gateway are required to process a payment.

---

## 4. Components

| Component | Job | Must survive |
|---|---|---|
| **Paisa API** | Accept payments, route, call gateways, receive webhooks | Kafka, Redis or model failure |
| **Gateway Simulator** | Act like 5 outside gateways, inject faults, record true money movement | Runs independently |
| **Recovery Worker** | Resolve unknown attempts, process webhooks, reverse duplicates | Restart at any moment |
| **Outbox Relay** | Publish committed events to Kafka in order | Crash between send and mark |
| **Feature Worker** | Compute rolling health and bandit state, write to Redis | Duplicate and late events |
| **Parquet Sink** | Save every event to history files | Crash mid-file |
| **Replay Lab** | Compare routing policies on identical traffic | Offline tool |
| **Demo page** | Show traffic, let you kill a gateway, show payments recovered | Read-only except demo controls |
| **Prometheus + Grafana** | Engineering metrics | Optional profile, to save RAM |

### Full stack

| Layer | Tool | Reason |
|---|---|---|
| Language | Python 3.12 (already installed) | Best fit for data + ML, and fastest for learning |
| API | FastAPI, Uvicorn, Pydantic v2 | Typed requests and automatic API docs |
| Database | PostgreSQL 16, psycopg 3, plain SQL, Alembic migrations | Plain SQL teaches more and matches interview questions |
| Streaming | **Apache Kafka** (single node, KRaft), `confluent-kafka` client | Your chosen tool and the industry standard |
| Fast state | Redis | Latest features and bandit state |
| Data | Parquet, PyArrow, DuckDB | Real analytics with no cluster |
| ML | NumPy, custom Thompson Sampling | Small enough to fully understand and explain |
| Frontend | React, TypeScript, Vite, Server-Sent Events | One live page |
| Testing | pytest, Hypothesis, Testcontainers, k6 (via Docker) | Property, integration, crash and load tests |
| Ops | Docker Compose, GitHub Actions, Prometheus, Grafana | Repeatable one-command run |
| Cloud | AWS (EC2, ECR, S3, IAM, SSM, CloudWatch, Budgets), Terraform | Real deployment, security and cost control (section 15) |

### RAM budget [Estimate, measured in Phase 5]

Your laptop has 15.8 GB total and about 6 GB free. Docker will be capped at **3 GB**.

| Service | Limit |
|---|---:|
| Kafka (heap capped at 512 MB) | 800 MB |
| Postgres | 300 MB |
| Paisa API | 200 MB |
| Prometheus | 200 MB |
| Gateway Simulator | 150 MB |
| Parquet Sink | 150 MB |
| Grafana | 150 MB |
| Feature Worker | 120 MB |
| Recovery Worker | 100 MB |
| Outbox Relay | 80 MB |
| Redis | 64 MB |
| **Total** | **about 2.3 GB** |

---

## 5. Payment methods

Each method fails differently. That difference is why routing needs to know the method.

| | UPI | Card | Netbanking |
|---|---|---|---|
| **Customer step** | Approves in UPI app with PIN | Enters OTP (3-D Secure) | Logs in on bank page |
| **Typical wait** | Seconds to minutes | Seconds | Up to minutes |
| **Common failure** | Bank down, PIN wrong, approval ignored | Issuer decline, OTP failure | Customer drops off, callback lost |
| **Routing segment** | Gateway + customer's bank | Gateway + card network + issuer | Gateway + bank |
| **Auto-failover allowed** | Only **before** the customer is prompted | Only **before** OTP | Only **before** the redirect |

**Why failover stops after the customer step:** the customer has already approved a specific request. Moving it silently is unsafe, so Paisa-X returns a failure with `can_retry_with_customer: true`.

Version 1 models a card payment as a single "sale" step. Separate authorization and capture are a later add-on.

---

## 6. Core logic: the payment flow

### 6.1 Happy path

```
Merchant        Paisa API              Postgres            Gateway
   │ POST /payments │                      │                   │
   │───────────────►│ TX1: save key+payment│                   │
   │                │─────────────────────►│                   │
   │                │ Route (in memory, no DB transaction open)│
   │                │ TX2: save attempt, decision, event       │
   │                │─────────────────────►│                   │
   │                │ Call gateway using attempt_id as its key │
   │                │─────────────────────────────────────────►│
   │                │◄─────────────────────────────────────────│
   │                │ TX3: save result + event                 │
   │                │─────────────────────►│                   │
   │◄───────────────│ response             │                   │
```

**The two most important rules here:**

1. **Write the attempt before calling the gateway (TX2).** If the API crashes right after calling the gateway, the saved attempt tells the Recovery Worker to go and check. Without it, money could move with no record.
2. **Never hold a database transaction during a gateway call.** A slow gateway would hold database connections and locks, so one bad gateway could freeze every payment.

### 6.2 Payment state machine

```
CREATED ─► PROCESSING ─► AWAITING_CUSTOMER ─► SUCCEEDED (final)
   │           │  ▲              │
   │           │  └ failover     ├──────────► FAILED (final)
   │           ▼                 ▼
   │    PENDING_CONFIRMATION ◄───┘   (result unknown, never auto-failed)
   │           ├─► SUCCEEDED
   │           ├─► FAILED
   │           └─► PROCESSING (only if failover is proven safe)
   └─► FAILED (no eligible gateway, or abandoned before any attempt)
```

`PROCESSING` can also go straight to `SUCCEEDED` or `FAILED` when a gateway answers immediately. `AWAITING_CUSTOMER` can return to `PROCESSING` only if the attempt is proven safe and the customer was not prompted, so in practice it ends in success, failure or pending confirmation.

### 6.3 Attempt state machine

| From | Can move to |
|---|---|
| `INITIATED` | `AWAITING_CUSTOMER`, `SUCCEEDED`, `DECLINED`, `FAILED_SAFE`, `UNKNOWN` |
| `AWAITING_CUSTOMER` | `SUCCEEDED`, `DECLINED`, `FAILED_SAFE`, `UNKNOWN` |
| `UNKNOWN` | `SUCCEEDED`, `DECLINED`, `FAILED_SAFE` |
| `DECLINED`, `FAILED_SAFE` | `SUCCEEDED` (late success, raises an alert) |
| `SUCCEEDED` | `REVERSED` (only for a non-winning duplicate) |

- **`DECLINED`:** a business answer such as insufficient funds. Never retry it.
- **`FAILED_SAFE`:** the gateway proved that no money moved.
- **`UNKNOWN`:** we do not know. Treat it as if money *may* have moved.

### 6.4 What happens for each result

| Gateway result | Attempt state | Failover? | Counts against gateway health? |
|---|---|---|---|
| Success | `SUCCEEDED` | No | Good signal |
| Insufficient funds, wrong PIN, card blocked | `DECLINED` | **Never** | No, it is not the gateway's fault |
| Connection refused (request never sent) | `FAILED_SAFE` | Yes, if allowed | Yes |
| 5xx with explicit "not processed" | `FAILED_SAFE` | Yes, if allowed | Yes |
| Timeout, connection lost mid-request, unclear 5xx | `UNKNOWN` | **Not until resolved** | Yes |
| Customer did not respond in time | `UNKNOWN`, then void (section 6.5) | No | No |

**Why an expired customer step is not an instant failure:** the customer might approve at the very moment we give up. Only a successful void proves the money did not move.

**Failover needs all of these:** last attempt is `FAILED_SAFE`, customer was not prompted, fewer than 3 attempts, time remains before the deadline, and the retry budget allows it.

The **retry budget** limits retries to about 10% of recent traffic **[CHANGE ME]**. Without it, a failing gateway causes a **retry storm**, where retries overload the healthy gateways too.

### 6.5 Resolving an unknown attempt (the hardest logic)

```
Attempt becomes UNKNOWN
        ▼
Ask gateway for status, waiting longer each time (1s, 2s, 4s...)
        ├─ SUCCESS ───────► SUCCEEDED
        ├─ DECLINED ──────► DECLINED
        ├─ FAILED ────────► FAILED_SAFE
        └─ NOT_FOUND
              ▼ wait until the safe window has passed
        Send VOID(attempt_id)
              ├─ VOIDED ────────────► FAILED_SAFE (failover may be allowed)
              ├─ ALREADY_SUCCEEDED ─► SUCCEEDED
              └─ No answer ─────────► stay UNKNOWN, alert after time limit
```

**Why "not found" is not enough:** the original request may still be travelling. If we retried elsewhere and it then arrived, the customer would pay twice. **VOID** tells the gateway to reject that attempt ID forever, which closes the gap.

**Who picks these up:** the Recovery Worker claims only attempts older than the gateway timeout plus a safety margin, so it does not interfere with a request the API is still handling. If a void makes failover safe, the worker starts the next attempt using the same payment service code. If the API and worker race, the row lock and state machine let only one valid change through.

### 6.6 Late and duplicate success

If an attempt succeeds after the payment has already finished:

1. Record the attempt as `SUCCEEDED`, with `is_winner = false`.
2. **Do not change the final payment state.**
3. Create a `reversal_task` in the same transaction.
4. The Recovery Worker reverses the charge, using a stable reversal ID so retrying is safe.
5. The audit confirms the customer's net charge is correct.

If an old attempt succeeds while the payment is **still running** on a newer attempt:

1. The old attempt becomes the winner, and the payment becomes `SUCCEEDED`.
2. The newer attempt is immediately sent a void.
3. If the newer attempt also succeeded, it follows the reversal steps above.

### 6.7 Idempotency

| Situation | Response |
|---|---|
| New key | Create the payment |
| Same key, same body, finished | Return the saved response |
| Same key, same body, in progress | `409` with `Retry-After` and the payment ID |
| Same key, **different** body | `422`, because this is a caller bug |

Two identical requests arriving together are handled by `INSERT ... ON CONFLICT DO NOTHING`. The database picks exactly one winner, so there is no check-then-insert race.

### 6.8 Webhooks

Real webhooks can arrive late, twice, out of order, or never. Paisa-X assumes all four.

1. Verify the **HMAC signature** on the raw body, and reject old timestamps to stop replay attacks.
2. Insert into `webhook_inbox`. A unique `(gateway, gateway_event_id)` drops duplicates.
3. Reply `200` immediately.
4. The Recovery Worker processes it through the same state machine, so a stale "pending" cannot overwrite "success".
5. If a webhook never comes, status polling resolves the attempt.

### 6.9 Crash map

We will inject a crash at every one of these points and run the audit.

| Crash happens | What is left behind | How it recovers |
|---|---|---|
| After TX1 | `CREATED` payment, no attempt | Worker marks it `FAILED` after a short limit. No gateway was called, so this is safe |
| After TX2, before gateway call | `INITIATED` attempt | Worker checks status, then voids |
| After gateway took money, before TX3 | `INITIATED` attempt | Worker finds success |
| After TX3, before reply | Finished payment, response saved in TX3 | Retry returns saved response |
| Relay: after Kafka send, before marking | Unmarked outbox row | Sent again, consumers remove duplicate |
| Feature Worker mid-batch | Uncommitted offset | Reprocess, event ID removes duplicate |
| Parquet Sink mid-file | Temporary file | Ignored, offset was not committed |
| Redis wiped | Empty fast state | Router uses fallback, and a rebuild command replays Kafka from the retained history |

---

## 7. Routing engine

```
Payment
  ▼ 1. ELIGIBILITY    Which gateways support this method, bank and amount?
  ▼ 2. HEALTH         Remove gateways whose circuit breaker is open
  ▼ 3. SCORE          Thompson Sampling estimates each gateway's success
  ▼ 4. UTILITY        Combine success, fee and latency
  ▼ 5. GUARDRAIL      Limit how much traffic is used for exploration
  ▼ 6. LOG            Save the decision, its reasons and policy version
Chosen gateway
```

**If any step after step 1 fails,** the router uses a static priority list and still answers. Decision time **[Target]: p99 under 10 ms**.

### 7.1 Two speeds of signal

| | Fast signal: circuit breaker | Slow signal: bandit |
|---|---|---|
| **Question** | Is it broken right now? | Which is best over time? |
| **Input** | Timeouts and 5xx seen directly by the API | Final outcomes through Kafka |
| **Speed** | Immediate | Seconds or more |

A **circuit breaker** has three states. **Closed** sends traffic normally. **Open** blocks it after too many failures. **Half-open** sends a few test payments to see if the gateway has recovered. Breakers are tracked per gateway + method, because a gateway's UPI can fail while its cards still work.

### 7.2 The learning model: discounted Thompson Sampling

For each gateway + segment, keep two numbers:

- **α (alpha):** starting belief + recent successes
- **β (beta):** starting belief + recent failures

To decide, draw one random guess from `Beta(α, β)` for each gateway. A gateway with little data has a wide range, so it sometimes gets tried. That is **exploration**. A gateway with strong data gives consistent guesses. That is **exploitation**.

**Forgetting old data:** before each update, move the counts back toward the starting belief (`α₀`, `β₀`):

```
decay = 0.5 ^ (seconds_since_last_update / half_life)
α = α₀ + (α - α₀) × decay
β = β₀ + (β - β₀) × decay
```

Without forgetting, a gateway that was great for weeks would keep getting traffic after it broke.

**Rare segments:** a small bank may have little data. Its starting belief comes from the wider gateway + method average, so it does not begin from nothing.

### 7.3 Choosing with fees and latency

```
utility = sampled_success × (merchant_value − fee) − latency_penalty
```

We keep success learning separate from cost. That keeps the maths simple, and lets us change fee or latency weights without relearning anything. Phase 7 starts with success only, then adds cost.

### 7.4 The delayed-feedback trap

Failures often come back fast, while successes wait for the customer. If we count results as they arrive, a healthy gateway can look **worse than it is**.

**Fix:** the bandit learns from attempts grouped by **when they started**, and only after they are final or older than a set maturity time. Sudden breakage is still caught immediately by the circuit breaker.

### 7.5 What teaches the bandit

| Outcome | Bandit update |
|---|---|
| Success | α + 1 |
| Technical failure or timeout that stays unresolved | β + 1 |
| Business decline | **Ignored** |
| Customer abandoned | **Ignored** |

---

## 8. Data pipeline

### 8.1 Event design

**Kafka topics** (one node, so replication factor is 1, which is a known local limit):

| Topic | Key | Partitions | Content |
|---|---|---:|---|
| `paisa.payment.lifecycle.v1` | `payment_id` | 3 | Payment and attempt state changes |
| `paisa.router.decisions.v1` | `payment_id` | 3 | Every routing decision and its reasons |
| `paisa.dlq.v1` | original key | 1 | Bad events kept for inspection |

Using `payment_id` as the key keeps **all events of one payment in order**. Kafka retention is capped by both time and size, so it cannot fill your disk (E: has about 25 GB free).

**Every event uses one envelope:**

```json
{
  "event_id": "time-sorted unique ID",
  "event_type": "attempt.succeeded",
  "schema_version": 1,
  "occurred_at": "when it happened",
  "recorded_at": "when we saved it",
  "payment_id": "...",
  "attempt_id": "...",
  "trace_id": "...",
  "payload": {}
}
```

Schemas live in the repo, and CI fails if a change would break old events. A Schema Registry is skipped to save RAM.

### 8.2 Delivery guarantee

Kafka delivery here is **at-least-once**: an event is never lost, but may arrive twice. We get a correct final result by making each consumer **idempotent**.

| Consumer | How duplicates are handled |
|---|---|
| Feature Worker | Redis Lua script checks `event_id` and updates state in one atomic step |
| Parquet Sink | Bronze may hold duplicates, silver removes them by `event_id` |

**Outbox order:** every write to a payment first locks that payment row, so its events commit in order. The relay reads unpublished rows by ID, and one relay runs at a time.

### 8.3 Data layers

```
BRONZE  Raw events exactly as received, partitioned by date and hour
   ▼    remove duplicates, validate, handle late events
SILVER  One clean row per payment and per attempt
   ▼    aggregate
GOLD    Success rate by gateway/method/minute, recovery time,
        fees, latency, policy comparison
```

**Quality checks:** unique IDs, valid state paths, amounts above zero, no event without a payment, event counts match Postgres, and gold totals match silver.

### 8.4 `paisa audit`, the final judge

The audit compares Paisa-X's records against the Gateway Simulator's hidden truth, and exits with an error if any invariant fails.

| Check | Must be |
|---|---:|
| Customers with net charge above the payment amount | 0 |
| Payments marked success with no real charge | 0 |
| Payments marked failed with money still taken | 0 |
| Illegal state transitions | 0 |
| Postgres events missing from Kafka/bronze | 0 |
| Attempts stuck unknown beyond the limit | Reported |

---

## 9. Database design

```sql
-- Sketch only. The final migrations will have a simple comment on every line.

CREATE TABLE payments (
  id                UUID PRIMARY KEY,
  merchant_id       UUID NOT NULL,
  amount_paise      BIGINT NOT NULL CHECK (amount_paise > 0),
  currency          CHAR(3) NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
  method            TEXT NOT NULL CHECK (method IN ('UPI','CARD','NETBANKING')),
  instrument_token  TEXT NOT NULL,      -- fake token only, never a card number
  bank_code         TEXT NOT NULL,
  state             TEXT NOT NULL,
  deadline_at       TIMESTAMPTZ NOT NULL,
  version           INT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
  merchant_id    UUID NOT NULL,
  key            TEXT NOT NULL,
  request_hash   BYTEA NOT NULL,
  payment_id     UUID REFERENCES payments(id),
  response_code  INT,
  response_body  JSONB,
  expires_at     TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (merchant_id, key)        -- enforces I1
);

CREATE TABLE payment_attempts (
  id                  UUID PRIMARY KEY,  -- also sent to the gateway as its idempotency key
  payment_id          UUID NOT NULL REFERENCES payments(id),
  attempt_no          SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
  gateway             TEXT NOT NULL,
  state               TEXT NOT NULL,
  is_winner           BOOLEAN NOT NULL DEFAULT false,
  customer_prompted   BOOLEAN NOT NULL DEFAULT false,
  failure_class       TEXT,
  next_check_at       TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (payment_id, attempt_no)
);

CREATE UNIQUE INDEX one_active_attempt ON payment_attempts (payment_id)
  WHERE state IN ('INITIATED','AWAITING_CUSTOMER','UNKNOWN');   -- enforces I2

CREATE UNIQUE INDEX one_winner ON payment_attempts (payment_id)
  WHERE is_winner;                                              -- enforces I3
```

| Other table | Purpose |
|---|---|
| `merchants` | Merchant IDs and hashed API keys |
| `attempt_transitions` | Append-only history of every state change (a trigger blocks edits) |
| `routing_decisions` | Options, scores, chosen gateway, policy version, exploration flag |
| `webhook_inbox` | Received webhooks, unique per gateway event |
| `outbox` | Events waiting to be published |
| `reversal_tasks` | Duplicate charges to reverse, unique per attempt |
| `gateway_config` | Supported methods, banks, limits, fees, static priority |

**Workers claim jobs using `FOR UPDATE SKIP LOCKED`,** so two workers never take the same row and neither waits for the other.

**Every state change** runs in one transaction: lock the payment, validate the move, update the row, add a transition, add the outbox event.

---

## 10. API

| Method and path | Purpose |
|---|---|
| `POST /v1/payments` | Create and run a payment (needs `Idempotency-Key`) |
| `GET /v1/payments/{id}` | Current status and attempts |
| `POST /v1/webhooks/{gateway}` | Receive signed gateway results |
| `GET /v1/router/health` | Live gateway health and traffic share |
| `GET /v1/demo/stream` | Live events for the demo page (SSE) |
| `GET /healthz`, `/readyz`, `/metrics` | Service health and Prometheus metrics |

| Gateway Simulator endpoint | Purpose |
|---|---|
| `POST /sim/{gateway}/pay` | Start a payment |
| `GET /sim/{gateway}/status/{attempt_id}` | Status inquiry |
| `POST /sim/{gateway}/void/{attempt_id}` | Block an attempt forever |
| `POST /sim/{gateway}/reverse/{attempt_id}` | Reverse a charge |
| `POST /sim/admin/faults` | Inject a failure (demo and tests only) |
| `GET /sim/admin/truth` | True ledger, used **only** by the audit |

**Security, kept proportionate:** hashed API keys, signed webhooks, secrets in an untracked `.env`, fake instrument tokens only, no sensitive values in logs, and admin endpoints available only on the private Docker network.

---

## 11. Simulator and fair evaluation

### Fault scenarios

| Scenario | What it tests |
|---|---|
| Full outage | Fast failover |
| Partial outage (one bank or method) | Segment-level routing |
| Brownout (slow but working) | Latency handling |
| Slow decay | Detecting gradual decline |
| Flapping (up/down/up) | Breaker stability |
| Timeout **after** taking money | Double-charge protection |
| Lost, duplicate, late, reordered webhooks | Webhook safety |
| Retry storm | Retry budget |
| Late success after void bug | Reversal path |

### How the comparison stays fair

1. **Common random numbers:** each payment's outcome on each gateway is fixed by `hash(seed, payment_id, gateway)`. Every policy faces the exact same luck.
2. **Tuning and test scenarios are separate.** The test scenarios stay locked until the final run.
3. **Strong baselines:** fixed priority, round-robin, rolling success rate, and **rolling success rate + circuit breaker**. Beating only weak rules proves nothing.
4. **At least 30 seeds,** with paired differences and 95% bootstrap confidence intervals.
5. **Fast virtual-clock replay,** using the same routing code as the live system.
6. **Every result is reported,** including cases where the bandit loses.

**Metrics:** success rate, payments saved, recovery time after an outage, regret, average fee, p95 latency, exploration cost.

**The simulator's limit:** results prove the engineering and compare policies under stated assumptions. They do **not** predict real-world gains.

---

## 12. Repository structure

```
Paisa-X/
├── README.md
├── docker-compose.yml
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── IMPLEMENTATION_PLAN.md
│   ├── BUILD_RULES.md
│   └── decisions/             # short "why we chose this" notes
├── src/paisa/
│   ├── core/                  # PURE logic: no database or network
│   │   ├── money.py
│   │   ├── states.py
│   │   ├── failure_policy.py
│   │   └── routing/           # eligibility, breaker, bandit, utility
│   ├── api/                   # FastAPI routes and schemas
│   ├── services/              # payment flow that coordinates everything
│   ├── adapters/              # Postgres, Kafka, Redis, gateway client
│   ├── workers/               # relay, recovery, features, parquet sink
│   ├── pipeline/              # bronze, silver, gold, quality, audit
│   └── replay/                # Replay Lab
├── simulator/                 # Gateway Simulator + traffic generator
├── migrations/
├── frontend/                  # one live demo page
├── infra/                     # Prometheus, Grafana and Terraform (AWS) config
├── load/                      # k6 scripts
└── tests/                     # unit, property, integration, chaos
```

**Import rule, checked in CI:** `core/` may not import from any other Paisa-X layer. This keeps the money and routing logic pure and testable.

---

## 13. Reliability and performance targets [Target]

| Measure | Target |
|---|---|
| Audit violations across all chaos scenarios | **0** |
| Routing decision p99 | Under 10 ms |
| Payment API overhead p99, excluding gateway time | Under 100 ms on this laptop |
| Traffic moved after full gateway outage | Under 30 s |
| Payments continue with Kafka, Redis or workers down | Yes |
| Streaming features match batch recalculation | Within a stated tolerance |

---

## 14. What changes at real scale

| Today | At real payment volume |
|---|---|
| One Postgres | Partitioning or sharding by merchant, replicas |
| One Kafka node | At least 3 brokers, replication factor 3 |
| One Redis | Redis cluster |
| Polling relay | CDC with Debezium |
| Simulated gateways | Real adapters, contracts, per-gateway quirks |
| One region | Multi-region failover |
| Fake tokens | PCI-compliant card vault |
| Docker Compose | Kubernetes |
| Python hot path | Possibly Rust or Go for the routing service |

---

## 15. Cloud deployment (short AWS run)

**Goal:** prove the same system can be created, secured, run, measured and destroyed on AWS with code. It is **not** left running.

```
GitHub Actions ── OIDC (short-lived login, no stored keys) ──► AWS
      │ builds images
      ▼
     ECR (private image storage)
      │ pulled by
      ▼
┌──────────────────────────────────────────────┐
│ One EC2 instance, no inbound ports           │
│ Docker Compose: the same Paisa-X stack       │◄── SSM Session Manager (admin access)
│ Auto-terminates after a fixed time limit     │
└───────┬───────────────────────────┬──────────┘
        │ IAM role                  │ logs and metrics
        ▼                           ▼
   S3 (encrypted Parquet)       CloudWatch

Around it: Terraform creates and destroys everything · AWS Budgets alerts on spend
```

| Decision | Choice | Why | Rejected option |
|---|---|---|---|
| Compute | One EC2 instance running Compose | Same setup as local, so the cloud run tests the real system | EKS or ECS add cost and a second deployment design |
| Kafka and Postgres | Containers on that instance | Cheap for a short test | MSK and RDS are costly for a portfolio run |
| Admin access | SSM Session Manager | No SSH key and no open port | Public SSH |
| Permissions | IAM role with least privilege | The app gets only the access it needs | Long-lived access keys |
| CI login | GitHub OIDC | Nothing secret is stored in GitHub | AWS keys in repository secrets |
| Data | S3 with encryption and public access blocked | Durable history that DuckDB can read | Keeping cloud results only on the instance |
| Networking | Public subnet, no inbound rules, S3 gateway endpoint | Avoids a NAT Gateway | NAT Gateway bills while it exists |
| Safety net | Auto-terminate + budget alerts + `terraform destroy` | A forgotten run cannot keep billing for long | Relying on memory |

**Honest limits:** one instance is a single point of failure, so this proves deployment skill and not high availability. Section 14 lists what would change at real scale.

**Check before creating anything:** current Free plan rules, eligible instance types, and prices. The instance needs at least 4 GB of RAM for the 2.3 GB stack. **[Verify]**
