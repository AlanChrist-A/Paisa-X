# Decision 0004: Safe payment core

**Decision:** process each payment in three short database transactions, save the attempt before calling a gateway, and let Postgres enforce the most important money rules.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| Attempt saved before the gateway call | A crash still leaves a record of what must be checked | One extra database write |
| No transaction open during a gateway call | A slow gateway cannot hold locks or freeze other payments | The flow has more steps |
| Idempotency through `INSERT ... ON CONFLICT` | Postgres picks one winner when identical requests race | Keys must be stored |
| Rules in both Python and Postgres | A bug in application code still cannot break core money rules | Some rules appear twice |
| Unclear gateway answers become `UNKNOWN` | We never assume money did not move | Some payments wait for Phase 3 recovery |
| Plain `def` routes with blocking psycopg | Simple code that also works on Windows | Concurrency is limited by the thread and connection pools |
| Portable Postgres 16.13 | No Docker, admin rights or Windows service, and very little RAM | Phase 9 still needs a containerised setup |

## Failure reproduced

**Case:** the API crashes right after the gateway may have taken money, before saving the result.

**Proof:** `test_hard_crash_leaves_recoverable_state[after_gateway]` really kills the server process. The `INITIATED` attempt survives, a retry cannot start a second attempt, and the audit finds no money violation.

## Verified

- All 97 tests pass on real Postgres.
- 100 concurrent identical requests created exactly 1 payment and 1 attempt.
- A hard process crash at each of the 4 risky points left one payment and no duplicate attempt.
- 300 payments with injected timeouts passed the audit.
- Migrations ran forward, repeated safely, rolled back to 0 tables, and ran forward again.
- `paisa audit` exits 0 on clean records and 1 when charges have no matching payment.

## Known limits

- Payments left `INITIATED`, `AWAITING_CUSTOMER` or `UNKNOWN` stay unresolved until the Phase 3 Recovery Worker exists. A retry of an interrupted request gets `409` until then.
- There is no failover yet, so a safe gateway failure ends the payment.
- Expired idempotency keys are not cleaned up yet.
- Postgres used about 18 MB of private memory at idle, which excludes its 64 MB shared buffer. It has not been measured under sustained load.

## Portable Postgres source

`postgresql-16.13-1-windows-x64-binaries.zip` came over HTTPS from `get.enterprisedb.com`. Its SHA-256 is `9B4C7A1BC31147B5B2BA59178D0EEF86C16181D54A20177192B387DFF20F00A9`. `postgres.exe` has no Windows code signature, and no published checksum was compared.
