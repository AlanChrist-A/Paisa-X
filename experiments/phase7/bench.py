"""
WHAT: a fast, in-memory test bench for routing policies, plus the Phase 7 step-by-step comparison.
WHY: the full system (Postgres + HTTP) takes about 0.1 s per payment, far too slow for hundreds of runs.
     This bench drives the REAL core Router against the REAL Gateway Simulator, with no database.
RUN: uv run python -m experiments.phase7.bench      (about 5-10 minutes; writes results/phase7.json)
USES: run() from src/paisa/replay/lab.py (the Replay Lab), which drives core routing and the simulator.
CAREFUL: tuning scenarios only. The locked scenarios are kept for the final Phase 8 exam.
LIMITS: no Recovery Worker, so a timed-out attempt never fails over. That is the same for every policy.
"""

import json  # Saves results.
import time  # Measures run time.
from pathlib import Path  # Results folder.

import numpy as np  # Averages.

from paisa.core.routing.bandit import CanaryPolicy, ThompsonPolicy  # Phase 7 brains.
from paisa.core.routing.policies import RollingPolicy  # The baseline to beat.
from paisa.replay.lab import CATALOG, bootstrap, run  # One world, shared with the Replay Lab (Phase 8 moved it there).

SEEDS = range(5)  # CHANGE ME: worlds per scenario. Every policy sees the same worlds (paired comparison).
SCENARIOS = ["calm", "full_outage", "partial_outage", "brownout", "slow_decay", "flapping"]  # "calm" = no faults.
COSTS = {(g, m): (c["fee_bps"][m], c["fee_flat_paise"]) for g, c in CATALOG["gateways"].items() for m in c["fee_bps"]}  # Fee terms.


def evaluate(make, feedback="final"):  # Runs a policy on every scenario and seed.
    return [run(make(), s, seed, feedback) for s in SCENARIOS for seed in SEEDS]


def compare(new, old):  # Paired difference: new minus old, same worlds.
    gain = [100 * (a["success"] - b["success"]) for a, b in zip(new, old)]  # Success, in points.
    fees = [a["fees_paise"] - b["fees_paise"] for a, b in zip(new, old)]  # Fee change.
    lat = [a["latency_ms"] - b["latency_ms"] for a, b in zip(new, old)]  # Delay change.
    return {"success_pts": float(np.mean(gain)), "success_ci": bootstrap(gain), "fees_change_pct": 100 * sum(fees) / sum(b["fees_paise"] for b in old), "latency_change_ms": float(np.mean(lat)), "success": float(np.mean([a["success"] for a in new]))}


def keep(step, utility=False):  # The keep rule the user chose: success first, then fees.
    if utility:  # Fee/latency step: must cut fees without losing more than 0.1 point.
        return step["fees_change_pct"] < 0 and step["success_pts"] >= -0.1
    return step["success_pts"] > 0  # Other steps: must raise success.


def main():  # The Phase 7 ladder: add one idea at a time, keep it only if it helps.
    started, out = time.time(), {}
    base = evaluate(RollingPolicy)  # The default to beat.
    out["rolling"] = {"success": float(np.mean([r["success"] for r in base]))}
    tune = {h: evaluate(lambda: ThompsonPolicy(half_life=h, explore_cap=None), "immediate") for h in (30, 60, 120)}  # Step 1: memory length.
    best_h = max(tune, key=lambda h: np.mean([r["success"] for r in tune[h]]))
    out["1_thompson"] = {"half_life": best_h, **compare(tune[best_h], base)}
    cfg, current = {"half_life": best_h, "explore_cap": None}, tune[best_h]  # What is kept so far, and its results.
    ladder = [  # Each step: name, extra settings, feedback mode, is it the utility step?
        ("2_bank_segments", {"by_bank": True}, "immediate", False),
        ("3_delayed_feedback", {}, "final", False),
        ("4_explore_cap", {"explore_cap": 0.10}, None, False),
        ("5_fee_latency_utility", {"costs": COSTS, "latency_weight": 0.01}, None, True),
    ]
    feedback = "immediate"
    for name, extra, mode, utility in ladder:  # Try each idea on top of what was kept.
        trial_mode = mode or feedback
        trial = evaluate(lambda: ThompsonPolicy(**cfg, **extra), trial_mode)
        step = compare(trial, current)  # Against the current best, not the baseline.
        step["kept"] = keep(step, utility)
        out[name] = step
        if step["kept"]:  # Adopt it.
            cfg, current, feedback = {**cfg, **extra}, trial, trial_mode
    final = compare(current, base)  # The finished bandit against rolling + breaker.
    final["verdict"] = "BETTER" if final["success_ci"][0] > 0 else "NOT BETTER"  # Same rule as Phase 0.
    out["final_vs_rolling"] = {"settings": {k: v for k, v in cfg.items() if k != "costs"} | {"costs": "costs" in cfg, "feedback": feedback}, **final}
    out["rollback_drill"] = rollback_drill()
    out["seconds"] = round(time.time() - started)
    path = Path(__file__).parent / "results"
    path.mkdir(exist_ok=True)
    (path / "phase7.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


class Inverted(ThompsonPolicy):  # A deliberately bad challenger: always picks its worst option.
    version = "inverted-v1"

    def rank(self, payment_id, options, rates, context=None):
        ranked, _ = super().rank(payment_id, options, rates, context)
        return ranked[::-1], False  # Worst first.


class Crashing(ThompsonPolicy):  # A challenger that always crashes.
    version = "crashing-v1"

    def rank(self, payment_id, options, rates, context=None):
        raise RuntimeError("model crashed")


def rollback_drill():  # A bad or crashing canary must roll back or fall back, and never block payments.
    results = {}
    for name, challenger in (("inverted", Inverted), ("crashing", Crashing)):
        canary = CanaryPolicy(challenger=challenger())  # 10% of traffic to the bad brain.
        r = run(canary, "calm", 0, "immediate")  # The kept feedback mode.
        results[name] = {"rolled_back": canary.rolled_back, "challenger_results": canary.score["challenger"][1], "crashes_caught": canary.errors, "success": r["success"]}
    results["rolling_same_world"] = run(RollingPolicy(), "calm", 0)["success"]
    return results


if __name__ == "__main__":
    main()
