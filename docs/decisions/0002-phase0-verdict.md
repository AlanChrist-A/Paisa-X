# Decision 0002: Phase 0 verdict

**Verdict: NOT PASSED.** In this simulation, Thompson Sampling did not beat the rolling rule with a circuit breaker.

| Method | Success rate (30 test seeds) | Tuned memory |
|---|---:|---:|
| Perfect knowledge (upper limit) | 94.57% | n/a |
| **Rolling + breaker** | **92.92%** | 100 |
| **Thompson Sampling** | **92.84%** | 500 |
| Rolling | 92.64% | 50 |
| Fixed | 90.84% | n/a |
| Round-robin | 86.13% | n/a |

- **Thompson minus Rolling + breaker:** -0.08 percentage points.
- **95% confidence range:** -0.31 to +0.18 points. It includes zero, so the two are statistically indistinguishable here.
- **Seeds won by Thompson:** 8 of 30.

## What this means

- Adaptive routing clearly helped: both adaptive methods beat fixed routing by about 2 points.
- The learning model showed **no proven advantage** over a strong rule in this simple world.
- Our claim becomes: **"a strong rule is enough in this simulation"**, unless a later, separately declared test shows otherwise.

## Limits

- This world has no fees, latency, banks or payment methods. Those are where a learning model could plausibly help, but that is untested.
- Thompson's best memory (500) was the largest option allowed, so a longer memory might do better. We did **not** change the options after seeing the result, because that would break the locked rules.

## Decision for the build

1. Keep **Rolling + breaker** as the default router design.
2. Keep Thompson Sampling as a challenger in Phase 7. It must beat this baseline on locked scenarios before it may route live traffic.
3. Reproduce with: `uv run python experiments/phase0/routing_experiment.py`
