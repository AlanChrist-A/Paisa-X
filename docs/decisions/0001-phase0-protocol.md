# Decision 0001: Phase 0 test rules (locked before running)

**Question:** when gateway health keeps changing, does a learning router (discounted Thompson Sampling) beat a strong rule-based router?

**Why these rules are written first:** if we choose the rules after seeing results, we can accidentally make our own idea look good.

## Assumptions

- This is a simulation, so it can only compare methods under these assumptions. It cannot predict real-world gains.
- 5 gateways, 20,000 payments per run. Normal gateway success is 85-97%.
- Random outages, brownouts (partial failure) and slow decay happen about every 1,500 payments.
- A result reaches the router 20 payments after the decision (delayed feedback).
- Phase 0 models success or failure only. Fees, latency, banks and payment methods come later.

## Methods compared

1. Fixed: always use the gateway that is normally best.
2. Round-robin: take turns.
3. Rolling success rate, with 5% exploration.
4. **Rolling + circuit breaker (the baseline to beat).**
5. **Discounted Thompson Sampling (the challenger).**

## Fairness rules

- Rolling methods and Thompson Sampling each choose one memory length from the same options: 50, 100, 200 or 500 payments.
- That choice uses tuning seeds 0-9 only.
- Final results use separate test seeds 1000-1029.
- Every method faces identical payment luck for each seed (common random numbers).

## Pass or fail

- **Primary metric:** payment success rate.
- **Pass:** Thompson Sampling minus Rolling + Breaker has a paired 95% bootstrap confidence interval entirely **above zero**.
- **Otherwise:** the claim becomes "a strong rule is enough in this simulation". The project still continues.
