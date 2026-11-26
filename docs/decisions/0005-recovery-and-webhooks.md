# Decision 0005: Recovery, webhooks and safe failover

**Decision:** send every gateway result through one function (`apply_result`), and let a restartable Recovery Worker finish anything left unclear.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| One result path for API replies, webhooks and status checks | Results can arrive in any order, and all follow the same rules | That function is critical and must stay well tested |
| Webhook inbox | We can reply fast, drop duplicates and process safely after a crash | Results apply slightly later |
| Claims commit before gateway calls | No locks are held during slow calls, and a crashed worker's job simply becomes due again | A job can run twice, so gateway calls must be idempotent |
| Void, then read the status back | A gateway that wrongly says "voided" cannot trigger a second charge | One extra call, and some payments wait longer |
| No automatic failover after a customer prompt | The customer approved one specific request | The customer must retry themselves |
| Late charges go to `reversal_tasks` in the same transaction | The refund cannot be forgotten after a crash | Refunds are eventual, not instant |

## Failure reproduced and fixed

**Found by:** the first 10,000-payment run. It had 0 money errors, but **23 payments never finished**.

**Cause:** when a late webhook proved a gateway had failed, the worker marked the payment ready for failover but did not start it. The backup rescue also missed it, because it compared the database's real clock with the test's virtual clock.

**Fix:** the worker now starts failover straight after the webhook, and all payment timestamps use the caller's clock. Two regression tests failed before the fix and pass after it.

## Verified

- All 123 tests pass on real Postgres.
- The final run of **10,000 payments with all tuning faults repeating** ended with **0 audit violations, 0 unresolved payments and 0 pending reversals**.
- A real worker process was killed at 6 risky points. After restart, recovery finished and the audit stayed clean.
- Forged, altered, old and wrong-gateway webhooks are refused. Duplicates are stored once.

## Known limits

- The 10,000-payment run uses a virtual clock and an in-process simulator, with `synchronous_commit` off on its disposable database. It proves the logic under faults, not real-time throughput or crash durability.
- It ran one worker. Row claiming is tested for two workers, but a sustained multi-worker run has not been done.
- The simulator's live webhook delivery has not been run end to end against the real API process. The route and the worker are each tested separately.
- There is no retry budget yet, so a retry storm is not limited until Phase 4.
- An attempt the gateway never resolves stays `UNKNOWN` and keeps being checked. There is no alert yet.
- Expired idempotency keys are not cleaned up yet.
