# Decision 0010: The Replay Lab and the final locked-scenario results

**Decision:** Paisa-X has a Replay Lab: one command replays the same simulated traffic through every routing policy on a virtual clock and writes a results report. On the locked final exam, the three real routers (static, rolling + breaker, Thompson) **tie overall**. **Rolling + breaker stays the default**: it is the only router that never collapses (static loses 73 payments per 6,000 in a retry storm), and the bandit's one clear loss is the same retry storm.

Final report: [docs/results/replay-report.md](../results/replay-report.md). Development run: [replay-tuning.md](../results/replay-tuning.md).

```
uv run python -m paisa.replay           # tuning scenarios (development)
uv run python -m paisa.replay locked    # the final exam, about 8 minutes on 8 cores
```

## What it measures

| Number | Meaning |
|---|---|
| Success rate | Share of payments where money really moved (from the simulator's hidden ledger) |
| Regret | Payments lost compared with the oracle, per 6,000 |
| Payments saved | Extra successes compared with static priority, per 6,000 |
| Recovery (degraded seconds) | Seconds in 10-second windows where first-try health was 5+ points below the same policy's calm world |
| Fees, latency | Gateway fees on successful charges; average first-try gateway delay |

Every policy sees the same worlds: 11 scenarios (calm + 10 fault groups) × 30 seeds × 6,000 payments. Comparisons are paired, with 95% bootstrap ranges.

**The oracle** (the user's choice) knows each gateway's true health at every moment, including outages, flapping, overload and decay, but not the luck of each payment. It is the yardstick for regret, not a real router.

## Final results (locked scenarios, opened once)

| | static | rolling | Thompson | oracle |
|---|---:|---:|---:|---:|
| Success, all scenarios | 84.16% | 84.16% | 84.18% | 84.63% |
| Regret, all scenarios | 28.4 | 28.3 | 27.2 | 0 |
| Regret, retry storm | **73.4** | 21.1 | 38.2 | 0 |
| Degraded seconds, retry storm | 118 | 25 | 15 | 0 |
| Fees (₹ per 6,000) | 503,360 | 490,233 | 501,909 | 521,994 |

**Thompson vs rolling, per scenario:**
- **Wins** in slow decay (+0.18 points), lost reply after charge (+0.09) and delayed requests (+0.17).
- **Loses** in the retry storm (−0.29 points, range −0.38 to −0.19).
- **Ties** everywhere else, including the pooled total (+0.02, range −0.01 to +0.05).

The locked scenarios were first opened on 2026-11-26 17:44. The routing code has not changed since then, and its fingerprint is stored in the report so any later change would be flagged.

## What we learned

- **There is little left to win.** Even a perfect router beats the real ones by only about 0.45 points (about 28 payments per 6,000). Most failures are customer declines and abandonment, which routing cannot fix.
- **Rolling's weakness is noise.** In slow decay, rolling loses 36 payments to the oracle while static loses 18, because small samples move traffic (the question raised in Decision 0006). Thompson sits in between at 25.
- **Rolling's strength is the bad day.** In the retry storm, static falls apart and rolling stays close to the oracle. Thompson recovers faster (15 s vs 25 s) but loses more payments overall, because it keeps exploring the overloaded gateway.
- **Tuning vs exam.** On the tuning scenarios rolling was ahead overall (regret 30.8 vs 32.1). On the locked ones Thompson was slightly ahead. Neither gap is significant, which agrees with Phases 0 and 7.

## Design choices

| Choice | Reason | Trade-off |
|---|---|---|
| The lab reuses the Phase 7 bench loop (moved to `src/paisa/replay/lab.py`) with the real `Router` and the real simulator | Same code as live routing; the bench now imports it, so there is one copy | No Recovery Worker, so timed-out attempts never fail over (the same for every policy) |
| `GatewaySim.health()` is shared by the simulator and the oracle | The yardstick cannot drift from the world it measures | None; Phase 7 numbers reproduce exactly |
| Worlds run in parallel on every CPU core | 1,320 worlds in about 8 minutes | Uses the whole laptop while running |
| Recovery is measured as degraded seconds, not "time to recover" | Works for every fault shape, including flapping and decay | The oracle shows degraded seconds in timeout scenarios, because it knows success chances, not timeouts |
| Run with `python -m`, not a `paisa` command | The simulator lives outside the installed package | One slightly longer command |

## Known limits

- The simulator gives banks no effect and gateways differ only a little, so routing has a small ceiling here.
- Shared vs local routing beliefs (Decision 0008) was not compared; the user chose to leave it out of this report.
