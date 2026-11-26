# Decision 0009: The learning router (Thompson Sampling)

**Decision:** Paisa-X now has a learning router: discounted Thompson Sampling, one belief per gateway + payment method, with a 60-second half-life and a 10% exploration cap. It can run in three modes: `thompson` (all traffic), `shadow` (rolling routes, Thompson only watches) and `canary` (Thompson routes 10% of payments and rolls back on its own). **Rolling + breaker stays the default**, because the finished bandit is not clearly better on the tuning scenarios. This matches the Phase 0 result.

Select a mode with `PAISA_ROUTING_POLICY=thompson|shadow|canary`. The circuit breakers, retry budget and fallback stay the same in every mode.

## How each idea was judged

Each idea was added one at a time, on top of what had been kept so far. The keep rule, chosen by the user, is **success first, then fees**:
- most ideas stay only if they raise the success rate;
- the fee and latency idea stays only if it cuts fees without losing more than 0.1 point of success.

Every policy ran on the same simulated worlds: 6 tuning scenarios (calm, full outage, partial outage, brownout, slow decay, flapping) × 5 seeds × 6,000 payments. Each comparison is paired, so it uses the same payments and the same luck for both policies.

| Step | Success change (points) | 95% interval | Other effects | Kept? |
|---|---:|---|---|---|
| 1. Thompson per gateway + method (half-life tuned: 60 s of 30/60/120) vs rolling | +0.03 | -0.05 to +0.12 | fees -1.5% | base for the next steps |
| 2. Separate beliefs per bank | **-0.31** | -0.39 to -0.25 | | No |
| 3. Learn from final results (delayed feedback) instead of the first reply | **-0.09** | -0.16 to -0.01 | | No |
| 4. Exploration cap at 10% | +0.04 | -0.04 to +0.13 | first-try latency -30 ms, fees +3.6% | Yes |
| 5. Utility = success × (1 − fee share) − latency penalty | **-0.57** | -0.70 to -0.45 | fees -11.3% | No (loses more than 0.1 point) |
| **Finished bandit vs rolling + breaker** | **+0.07** | **-0.03 to +0.18** | latency -28 ms, fees +2.1% | **Not better**, so rolling stays the default |

Reproduce with `uv run python -m experiments.phase7.bench` (about 8 minutes). Results are in `experiments/phase7/results/phase7.json`.

## Why the dropped ideas lost

- **Bank segments.** In the simulator, a bank does not change any gateway's success rate. Splitting the data by bank therefore only gave each belief fewer results to learn from. The user chose to keep the simulator unchanged so earlier phases stay comparable. This step would be worth trying again if real bank-level differences existed.
- **Delayed feedback.** Final results arrive 3 to 120 seconds later, after the customer step. In that time, an outage can already be costing payments. The fast signal ("the gateway accepted it") is noisier but arrives right away, and here speed won. The code is kept in the bench (`outcome()` and the `final` mode), so the test can be repeated.
- **Fee utility.** It moved traffic to cheaper gateways and cut fees by 11%, but it cost 0.57 point of success. Under the user's rule, success comes first.

## Safe rollout, measured

| Drill | Result | Target |
|---|---|---|
| Canary with a deliberately bad challenger (always picks the worst gateway) | Rolled back automatically after 200 scored results. Success was 84.83%, vs 84.73% for rolling in the same world | Automatic rollback, no lasting harm |
| Canary with a challenger that crashes on every call | 655 crashes caught, every payment routed by the base policy. Success was identical to rolling | Never blocks a payment |
| Shadow mode | The live decision is identical in every case; the shadow only counts agreement | Zero effect on payments |
| Real payment flow (Postgres + Recovery Worker), Thompson on **all** traffic, busiest gateway killed mid-run | 450 payments, **0 audit violations, 0 unresolved** | Audit clean with the bandit on |

Reproduce the last drill with `uv run paisa db-start`, then `uv run python -m experiments.phase7.live_drill`.

## Design choices

| Choice | Reason | Trade-off |
|---|---|---|
| A fast in-memory bench using the real `Router` and simulator | The full system takes about 0.1 s per payment; the bench takes about 0.25 ms | No Recovery Worker, so timed-out attempts never fail over (the same for every policy) |
| Beliefs fade toward Beta(1, 1) with a half-life in seconds | Old evidence should stop counting after a gateway recovers | Needs a clock; uses the router's time |
| Policies receive a `context` (method, bank, amount, time) | Learning policies need more than rates | All policies accept a `context=None` argument |
| `Router.learn()` holds the same lock as `decide()`, and ignores policy crashes | Beliefs never change mid-decision, and learning bugs never break payments | A bug could make it stop learning without any visible error |
| The canary scores each arm only on payments that arm routed | A fair comparison | Needs at least 200 results per arm before it can judge |
| Beliefs live in memory only | Simple and fast | A restart forgets them; the bandit relearns within minutes |

## Known limits

- In the simulator, gateway differences are small and breakers already handle outages, which leaves little for learning to win. A world with longer, milder problems may favour the bandit. Phase 8's Replay Lab will test it on the locked scenarios.
- Each API process learns on its own. Shared beliefs (for example through Redis, as Phase 6 does for rates) are not built.
- The canary judges on the fast health signal, not on final money results.
