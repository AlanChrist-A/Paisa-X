# Paisa-X: Implementation Plan

**Read first:** [ARCHITECTURE.md](ARCHITECTURE.md) and [BUILD_RULES.md](BUILD_RULES.md).

**How we work:** one phase at a time. A phase is finished only when its proof passes. There are no deadlines here, because you manage the timeline.

**Every phase ends with:** your own summary of the goal, heavily commented code, passing tests, one short decision note, one failure you reproduced, and a 60-second explain-back.

---

## Build order at a glance

| Phase | Build | New concepts | Proof |
|---|---|---|---|
| 0 | Routing experiment | Bandits, non-stationary data | Written verdict with confidence ranges |
| 1 | Gateway Simulator | Failure modelling, reproducibility | Same seed gives identical run |
| 2 | Safe payment core | Transactions, idempotency, state machines | Crash tests + first audit |
| 3 | Unknowns and webhooks | Timeouts, void, HMAC, workers | Timeout-after-charge never double-charges |
| 4 | Rule-based router | Circuit breakers, fallback, retry budget | Survives outage and broken model |
| 5 | Kafka pipeline | Outbox, consumers, at-least-once | No event lost after crashes |
| 6 | Features and data layers | Windows, Redis, Parquet, DuckDB | Stream values match batch |
| 7 | Learning router | Thompson Sampling, delayed feedback | Live routing adapts safely |
| 8 | Replay Lab | Regret, fair evaluation | Final locked-scenario results |
| 9 | Hardening | Observability, load, CI | One-command run + load report |
| 10 | Short AWS run | Terraform, IAM, EC2, S3, OIDC, cost control | Create, run, audit, destroy with code |
| 11 | Demo page | SSE, live UI | "Kill a Gateway" public replay |
| 12 | Interview pack | Explaining trade-offs | You can defend every phase |

**Why safety comes before machine learning:** a smart router on top of an unsafe payment flow is worthless. Phases 2 and 3 are the backbone of the project.

---

## Phase 0: Is there a real gain to find?

**Build:** one small Python experiment with no Docker. Simulate 5 gateways whose health changes. Compare fixed, round-robin, rolling success rate, rolling + breaker, and discounted Thompson Sampling.

**Written before running:**
- Primary metric: overall payment success rate.
- Win rule: Thompson Sampling beats **rolling + breaker**, and the 95% confidence interval of the paired difference across 30 seeds stays above zero.

**If it does not win:** the project continues. Our claim changes to "a strong rule is enough here, and here is the evidence", which is still an honest and interesting result.

**✅ Done when:** one chart and one written verdict exist.

---

## Phase 1: Gateway Simulator

**Build:**
- A separate FastAPI service with 5 fictional gateways.
- UPI, card and netbanking behaviour, banks, fees and latency.
- Pay, status, void and reverse endpoints, all idempotent by attempt ID.
- Signed webhooks that can be lost, duplicated, delayed or reordered.
- All fault scenarios from Architecture section 11.
- A hidden truth ledger of real money movement.
- Common random numbers, so outcomes are reproducible.

**✅ Done when:**
- The same seed produces byte-identical outcomes.
- Each fault scenario has a test.
- Tuning and locked test scenarios are stored separately.

---

## Phase 2: Safe payment core

**Build:**
- `core/money.py`, `core/states.py` and `core/failure_policy.py` as pure logic.
- Postgres schema and migrations, including all constraints.
- `POST /v1/payments` and `GET /v1/payments/{id}`.
- Three-transaction flow with attempt saved before the gateway call.
- Idempotency, and failpoints (deliberate crash switches for tests).
- A simple static router for now.
- First version of `paisa audit`.

**✅ Done when:**
- 100 concurrent identical requests create exactly one payment.
- Property tests show random event orders cannot produce an illegal state.
- Database constraints reject direct attempts to break I1, I2, I3 and I7.
- A crash at each failpoint leaves a recoverable state.

---

## Phase 3: Unknown results and webhooks

**Build:**
- Recovery Worker using `FOR UPDATE SKIP LOCKED`.
- Status polling with backoff, and the void flow.
- Webhook verification, inbox and processing.
- Safe failover rules for each payment method.
- Late-success handling and automatic reversal.

**✅ Done when:**
- A gateway that times out **after** taking the money never causes a net double charge.
- Lost, duplicated, late and reordered webhooks all end in the right state.
- Killing the worker at any point and restarting it leaves the audit clean.
- Audit passes across at least 10,000 simulated payments with faults on.

---

## Phase 4: Rule-based router

**Build:**
- Eligibility filter.
- Circuit breakers per gateway + method.
- Rolling success-rate policy.
- Retry budget and deadlines.
- Static fallback.
- A clean `RoutingPolicy` interface, so the bandit can plug in later.

**✅ Done when:**
- A full gateway outage moves traffic within the target time.
- A retry storm stays within budget.
- A deliberately crashing policy still produces a valid decision.
- Decision p50 and p99 are measured.

---

## Phase 5: Kafka and reliable events

**First step, before anything else:** run Kafka, Postgres and Redis with memory limits and record real RAM use. If it does not fit, stop and decide together.

**Build:**
- Docker Compose with single-node Kafka in KRaft mode.
- Outbox writes in every state-change transaction.
- Outbox Relay with an idempotent producer and `acks=all`.
- Versioned event schemas, CI compatibility test and dead-letter topic.

**✅ Done when:**
- RAM use is recorded.
- Killing the relay or restarting Kafka loses no event.
- Stopping Kafka does **not** stop payments, and events catch up afterwards.
- Per-payment event order is verified.

---

## Phase 6: Streaming features and data layers

**Build:**
- Feature Worker with event-time windows and late-event handling.
- Atomic, duplicate-safe updates to Redis.
- API snapshot refreshed in the background.
- Parquet Sink with atomic file writes.
- Bronze, silver and gold in DuckDB with quality checks.
- Full audit, including Postgres vs Kafka vs bronze.

**✅ Done when:**
- Streaming values match a batch recalculation within a stated tolerance.
- Wiping Redis does not stop routing, and state rebuilds from Kafka.
- A crash mid-write does not corrupt or lose data.

---

## Phase 7: Learning router

**Build in this order, measuring each step:**
1. Discounted Thompson Sampling per gateway + method.
2. Segments by bank or issuer, with shared starting beliefs.
3. Delayed-feedback handling.
4. Exploration cap.
5. Fee and latency utility.
6. Shadow mode: the bandit decides silently beside the rule router.
7. Canary rollout with automatic rollback.

**Each addition stays only if it improves results on tuning scenarios.**

**✅ Done when:**
- The bandit runs live, can be rolled back automatically, and its failure never blocks a payment.
- The audit stays clean with the bandit on.

---

## Phase 8: Replay Lab

**Build:** virtual-clock replay using the same `core/routing` code. One command runs every policy across scenarios and at least 30 seeds. Output includes success, payments saved, regret, recovery time, fees, latency and confidence ranges.

**Rule:** open the locked test scenarios only once, after all tuning is complete. If we change the model after seeing them, we must say so in the report.

**✅ Done when:** one command regenerates the full results report, including where the bandit loses.

---

## Phase 9: Hardening

**Build:** one-command startup, GitHub Actions, Prometheus metrics, Grafana dashboards, structured logs with trace IDs, k6 load tests, and one `paisa chaos` command that runs every failure and then the audit.

**✅ Done when:**
- A stranger can run it from the README.
- The load report shows where latency starts to degrade on this laptop.
- The chaos command exits with an error if any invariant fails.

---

## Phase 10: Short AWS run

**Before anything is created:**
- Check current AWS Free plan rules, eligible instance types and prices.
- You open the AWS account yourself and turn on MFA. Claude never handles your AWS password or payment details.
- Agree the budget limit and maximum run time together.

**Build (design in Architecture section 15):**
1. Budget alerts first, before any other resource.
2. Terraform for the network, EC2, ECR, S3, IAM, SSM and CloudWatch.
3. GitHub Actions login through OIDC, building and pushing images.
4. Run the Docker Compose stack on EC2, with automatic termination.
5. Save Parquet history to encrypted S3 and query it with DuckDB.
6. Run `paisa chaos` and `paisa audit` in the cloud.
7. Destroy everything and confirm nothing billable remains.

**✅ Done when:**
- One command creates the setup, and one command removes it.
- The audit passes on AWS with 0 invariant violations.
- No inbound ports are open and no long-lived AWS keys exist.
- Real cost per run is recorded from AWS billing.

---

## Phase 11: "Kill a Gateway" demo

**Build:** one React page showing payments flowing through gateways. Click to break one, watch failures rise, watch traffic move, and see payments saved versus fixed routing with the audit status beside it. The public version plays a **recorded run**, so hosting is free and always works.

**✅ Done when:** a public link works on a phone, and there is a 2-minute video.

---

## Phase 12: Interview pack

Prepare and practise answers to:

1. Walk me through a payment from request to final state.
2. A gateway timed out. What do you do, and why not just retry?
3. How exactly do you prevent double charges, and where can that still fail?
4. Why an outbox? What breaks without it?
5. What does at-least-once mean, and how do you handle duplicates?
6. Why is the database the source of truth, rather than Kafka?
7. Why a bandit rather than a classifier?
8. How do you handle delayed feedback?
9. How do you know your evaluation is fair?
10. What happens if Redis, Kafka or Postgres dies?
11. What would you change at 10,000 payments per second?
12. How is the AWS setup secured, what did a run cost, and why no NAT Gateway, MSK or EKS?

**✅ Done when:** you can answer each in about 60 seconds, plus the README, decision notes, known limits and resume bullets use measured numbers only.

---

## Main risks

| Risk | How we handle it |
|---|---|
| Laptop RAM | Memory limits, measure in Phase 5, monitoring is optional |
| Three payment methods add work | One shared state machine, with method differences kept in small rule tables |
| Simulator flatters the model | Locked scenarios, strong baselines, many seeds |
| Bandit may not win | Honest result is acceptable, safety work stands on its own |
| Code you cannot explain | Comments on nearly every line + explain-backs before moving on |
| Surprise AWS bill | Budget alerts first, auto-termination, destroy after each run, no always-on services |
| AWS free period runs out before Hawk X3 | Use few short runs, then re-check credits and plan Hawk's cloud run before it starts |
| Scope creep | Anything outside Architecture section 1 waits until version 1 is finished |
