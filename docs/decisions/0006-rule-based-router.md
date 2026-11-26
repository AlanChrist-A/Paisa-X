# Decision 0006: Rule-based router

**Decision:** route with a rolling success rate behind per-gateway, per-method circuit breakers, a retry budget and a static fallback. Policies plug in through one small interface.

## Why

| Choice | Reason | Trade-off |
|---|---|---|
| Rolling + breaker as the default | It was the strongest method in Phase 0 | It reacts quickly to small samples, so it can move traffic on noise |
| Breakers per gateway + method | A gateway's UPI can fail while its cards still work | More state to track |
| One careful probe after a cooldown | Recovery is tested without sending a wave of traffic | A real payment may hit a broken gateway, but only safely |
| Retry budget of about 10% of traffic | Failures cannot multiply into a retry storm | Some safe retries are refused during a large outage |
| Static fallback | A crashing or invalid policy can never stop a payment | Fallback routing ignores health scores from the policy |
| Routing in memory | No network call is needed to decide | Each process learns separately until Phase 6 |
| Policy registry | A new policy needs one class and one registry line | None significant |

## Failure reproduced and fixed

**Case:** the first outage drill reported rerouting in 0 seconds, which looked too good.

**Check:** the drill always killed `alpha`, even if `alpha` was not carrying most traffic. It now kills the **busiest** gateway at that moment and records its traffic share. A second-by-second check confirmed the result is real.

## Measured

Measured on this laptop. Reproduce with `uv run python -m experiments.phase4.drills`.

| Measure | Result | Target |
|---|---:|---:|
| Routing decision p50 | 36.6 microseconds | n/a |
| Routing decision p99 | 223.1 microseconds | Under 10 ms |
| Busiest gateway's traffic before outage | 78% | n/a |
| Time until its traffic fell below 10% | Under 1 second on the drill's whole-second measure | Under 30 s |
| First attempts sent to it during the 60-second outage | 10 of 300, mainly recovery probes | n/a |
| Audit violations in the drill | 0 | 0 |
| 10,000-payment fault run | 0 violations, 0 unresolved | 0 |

All 147 tests pass.

## Known limits

- The drill and soak use a virtual clock at 5 payments per second, so they prove routing logic and not production throughput.
- Decision time covers `Router.decide()` only. It excludes database and gateway time.
- The router learns only from direct gateway replies. Late customer outcomes reach it after Phase 6.
- The API and Recovery Worker keep separate health and retry budgets until shared state exists.
- A single failure can move traffic because small samples swing the rolling rate. Phase 8 should measure whether this costs success rate or fees.
