"""
WHAT: the Replay Lab. It replays the same simulated traffic through every routing policy on a virtual
      clock, then writes one results report with 95% confidence ranges.
WHY: "router A beats router B" is only fair with the same payments, the same luck and many worlds.
RUN: uv run python -m paisa.replay           (tuning scenarios, for development)
     uv run python -m paisa.replay locked    (the final exam; about 10 minutes on 8 cores)
USES: the REAL core Router and policies, and the REAL Gateway Simulator. No database or network.
CAREFUL: the locked scenarios are the final exam. The report stores a fingerprint of the routing code,
         so if the code changes after the first locked run, the report says so.
LIMITS: no Recovery Worker, so a timed-out attempt never fails over. That is the same for every policy.
"""

import hashlib  # Fingerprint of the routing code.
import json  # Saves raw results.
import time  # Run time and dates.
from collections import deque  # Attempts waiting for a final result, oldest first.
from multiprocessing import Pool  # Runs worlds on every CPU core.
from pathlib import Path  # File paths.

import numpy as np  # Averages and bootstrap confidence ranges.

from paisa.core.failure_policy import MAX_ATTEMPTS, classify, health_signal  # The same rules as live payments.
from paisa.core.routing.bandit import ThompsonPolicy, outcome  # The Phase 7 learning router.
from paisa.core.routing.policies import RollingPolicy, StaticPolicy  # Rule-based routers.
from paisa.core.routing.router import Router  # Breakers, retry budget and fallback.
from paisa.core.routing.static import eligible  # Which gateways can serve a payment.
from simulator.engine import GatewaySim, load_catalog, load_scenarios, make_traffic  # The outside world.

ROOT = Path(__file__).resolve().parents[3]  # Project folder.
RESULTS = ROOT / "docs" / "results"  # Where reports are written.
PAYMENTS = 6_000  # CHANGE ME: payments per world.
PER_SECOND = 20  # CHANGE ME: traffic rate. 6,000 at 20/s covers the 5-minute fault plans.
MATURITY = 150  # CHANGE ME: seconds before an unfinished attempt is judged (slowest customer step is 120 s).
SEEDS = range(30)  # CHANGE ME: worlds per scenario. Every policy sees the same worlds (paired comparison).
WINDOW_S = 10  # CHANGE ME: seconds per window when measuring how long a fault hurt customers.
DEGRADED_PTS = 5  # CHANGE ME: a window is "degraded" if first-try health is this many points below calm.
CATALOG = load_catalog()  # The simulated market.
CONFIGS = [{"gateway": g, "priority": i, "methods": list(c["success"]), "skip_banks": c["skip_banks"], "networks": c["networks"]} for i, (g, c) in enumerate(CATALOG["gateways"].items(), 1)]  # Same as gateway_config.
FINAL = {"SUCCEEDED": "SUCCEEDED", "DECLINED": "DECLINED", "FAILED": "FAILED_SAFE"}  # Webhook -> attempt state.


class OraclePolicy:  # The perfect router used to measure regret: it knows each gateway's TRUE health now.
    version = "oracle-v1"  # Policy name.
    world = None  # The simulator. run() plugs it in, because only the oracle may look inside.

    def health(self, gateway, payment, now_ms):  # True success chance, but not the luck of this payment.
        sim = self.world
        if sim._match("outage", gateway, now_ms, payment) or sim._match("flap", gateway, now_ms, payment):
            return 0.0  # Down: every request is refused.
        recent = sum(t > now_ms - 1_000 for t in sim._recent[gateway])  # Requests in the last second.
        if any(recent >= f.value for f in sim._match("overload", gateway, now_ms, payment)):
            return 0.0  # Full this second: the next request is refused.
        return sim.health(gateway, payment, now_ms)  # Normal chance, after any decay.

    def rank(self, payment_id, options, rates, context=None):  # Healthiest first; ties keep priority order.
        payment = {"method": context["method"], "bank": context["bank"]}  # What faults filter on.
        now_ms = context["now"] * 1000  # The router works in seconds, the simulator in milliseconds.
        return sorted(options, key=lambda g: -self.health(g, payment, now_ms)), False


POLICIES = {"static": StaticPolicy, "rolling": RollingPolicy, "thompson": ThompsonPolicy, "oracle": OraclePolicy}  # ADD A POLICY HERE.


def run(policy, scenario, seed, feedback="final", group="tuning"):  # One world, one policy. Returns its scores.
    faults = [] if scenario == "calm" else load_scenarios(group, unlock=group == "locked")[scenario]  # The fault plan.
    sim, router = GatewaySim(seed, faults, CATALOG), Router(policy)  # Fresh world and fresh router.
    if hasattr(policy, "world"):  # The oracle needs to see the truth.
        policy.world = sim
    pending, info, learned, latency = deque(), {}, set(), []  # Waiting attempts, their details, done ones, delays.
    windows = {}  # Window number -> [healthy first tries, all first tries].

    def teach(attempt_id, state, prompted, code, now):  # Gives one final result to the policy, once.
        if attempt_id in learned:  # A duplicate webhook or a later check.
            return
        learned.add(attempt_id)
        pid, gateway, p, ms = info[attempt_id]  # Who, where, what, and its delay.
        router.learn(pid, gateway, p["method"], p["bank"], outcome(state, prompted, code), now, ms)

    for p in make_traffic(seed, PAYMENTS, PER_SECOND, CATALOG):  # Payments in time order.
        now_ms = p["at_ms"]
        now = now_ms / 1000  # The router works in seconds.
        if feedback == "final":  # Delayed feedback: learn only from final results.
            for event in sim.due_webhooks(now_ms):  # Webhooks that have arrived.
                if event["status"] in FINAL:  # A final result.
                    a = sim.attempts[(event["gateway"], event["attempt_id"])]  # Is the customer prompted?
                    teach(event["attempt_id"], FINAL[event["status"]], a.prompted, None, now)
            while pending and pending[0][0] <= now - MATURITY:  # Too old to wait for: the worker would void it.
                _, gateway, attempt_id = pending.popleft()
                prompted = sim.attempts[(gateway, attempt_id)].prompted  # Abandoned (prompted) or lost (not)?
                teach(attempt_id, "FAILED_SAFE", prompted, "VOIDED", now)  # A webhook may have taught it already.
        options, tried = eligible(p, CONFIGS), []  # Gateways that can serve it.
        for n in range(MAX_ATTEMPTS):  # At most 3 tries, as in live payments.
            remaining = [g for g in options if g not in tried]  # Not tried yet.
            if not remaining or not router.admit(n == 0):  # Out of options or retry budget.
                break
            d = router.decide(p["payment_id"], p["method"], remaining, now, p["bank"], p["amount_paise"])  # Choose.
            attempt_id = f"{p['payment_id']}:{n}"  # One ID per attempt.
            reply = sim.pay(d.chosen, attempt_id, p, now_ms)  # Call the gateway.
            tried.append(d.chosen)
            target, code = classify(reply), reply.get("code")  # Safe meaning of the reply.
            info[attempt_id] = (p["payment_id"], d.chosen, p, reply["latency_ms"] or None)  # Details for learning.
            healthy = health_signal(target, code)  # Fast signal for breakers and rolling rates.
            if n == 0:  # The first try is what the customer feels.
                if reply["latency_ms"]:
                    latency.append(reply["latency_ms"])
                if healthy is not None:  # Count it in its time window.
                    w = windows.setdefault(int(now // WINDOW_S), [0, 0])
                    w[0], w[1] = w[0] + healthy, w[1] + 1
            if healthy is not None:
                router.record(d.chosen, p["method"], healthy, now, now)
            if feedback == "immediate":  # Phase 0 style: learn from the first reply at once.
                learned.add(attempt_id)
                router.learn(p["payment_id"], d.chosen, p["method"], p["bank"], healthy, now, reply["latency_ms"] or None)
            elif target == "FAILED_SAFE":  # Refused or failed at once: already final.
                teach(attempt_id, target, False, code, now)
            if target != "FAILED_SAFE":  # Accepted, timed out or prompted: no automatic failover.
                pending.append((now, d.chosen, attempt_id))
                break
    sim.advance(10**9)  # Let every customer step finish.
    net, fees = sim.truth()["net_paise"], sum(r["fee_paise"] for r in sim.ledger if r["kind"] == "CHARGE")
    successes = sum(v > 0 for v in net.values())  # Payments where money moved.
    return {"success": successes / PAYMENTS, "successes": successes, "fees_paise": fees, "latency_ms": float(np.mean(latency)), "windows": [windows[k] for k in sorted(windows)]}


def bootstrap(values, draws=10_000):  # 95% range for a mean, by resampling.
    values = np.asarray(values)
    picks = np.random.default_rng(0).integers(len(values), size=(draws, len(values)))
    return [float(x) for x in np.percentile(values[picks].mean(axis=1), [2.5, 97.5])]


def degraded_s(result, calm):  # Seconds in which customers felt the fault (first-try health well below calm).
    ok, total = map(sum, zip(*calm["windows"]))  # The same policy and seed with no faults.
    floor = ok / total - DEGRADED_PTS / 100  # Below this, a window counts as degraded.
    return WINDOW_S * sum(n >= 50 and good / n < floor for good, n in result["windows"])  # Skip tiny windows.


def _task(job):  # One world in a worker process. Top level, so Windows can send it to another process.
    name, scenario, seed, group = job
    return job, run(POLICIES[name](), scenario, seed, "immediate", group)  # Immediate feedback was kept in Phase 7.


def fingerprint():  # Hash of the routing code, to detect changes after the exam was seen.
    files = sorted((ROOT / "src" / "paisa" / "core" / "routing").glob("*.py"))
    return hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()[:16]


def summarise(results, scenarios):  # Turns raw runs into per-scenario numbers with confidence ranges.
    get = lambda name, s: [results[(name, s, seed)] for seed in SEEDS]  # noqa: E731  All seeds, in order.
    out = {}
    for s in scenarios + ["ALL"]:  # "ALL" pools every scenario.
        pick = (lambda name: [r for x in scenarios for r in get(name, x)]) if s == "ALL" else (lambda name: get(name, s))
        calm = lambda name: get(name, "calm") * (len(scenarios) if s == "ALL" else 1)  # noqa: E731  Matching calm runs.
        row = {}
        for name in POLICIES:
            runs, oracle, static = pick(name), pick("oracle"), pick("static")
            regret = [o["successes"] - r["successes"] for o, r in zip(oracle, runs)]  # Payments lost vs perfect.
            saved = [r["successes"] - b["successes"] for r, b in zip(runs, static)]  # Payments saved vs fixed order.
            row[name] = {
                "success_pct": 100 * float(np.mean([r["success"] for r in runs])),
                "regret": float(np.mean(regret)), "regret_ci": bootstrap(regret),
                "saved": float(np.mean(saved)), "saved_ci": bootstrap(saved),
                "degraded_s": float(np.mean([degraded_s(r, c) for r, c in zip(runs, calm(name))])),
                "fees_rupees": float(np.mean([r["fees_paise"] for r in runs])) / 100,
                "latency_ms": float(np.mean([r["latency_ms"] for r in runs])),
            }
        gain = [100 * (t["success"] - r["success"]) for t, r in zip(pick("thompson"), pick("rolling"))]  # Paired, in points.
        ci = bootstrap(gain)
        row["thompson_vs_rolling"] = {"pts": float(np.mean(gain)), "ci": ci, "verdict": "WINS" if ci[0] > 0 else "LOSES" if ci[1] < 0 else "TIE"}
        out[s] = row
    return out


def report(summary, meta):  # Writes the Markdown report people read.
    names, rows = list(POLICIES), list(summary)
    lines = [f"# Replay Lab report: {meta['group']} scenarios", "",
             f"Generated {meta['date']} by `{meta['command']}` in {meta['seconds']} s. "
             f"{len(SEEDS)} seeds x {PAYMENTS:,} payments per scenario, the same worlds for every policy. "
             f"Routing code fingerprint `{meta['fingerprint']}`.", ""]
    if meta.get("first_opened"):  # The exam rule.
        lines += [f"Locked scenarios first opened {meta['first_opened']}. "
                  + ("**The routing code changed after the locked scenarios were first seen.**" if meta["changed_after_opening"] else "The routing code has not changed since then."), ""]
    lines += ["`oracle` knows every gateway's true health at each moment (not each payment's luck). It is the yardstick for regret, not a real router.",
              "`ALL` pools every scenario. Ranges are 95% bootstrap intervals over paired worlds.", ""]

    def table(title, note, cell):  # One table: scenarios down, policies across.
        lines.extend([f"## {title}", "", note, "", "| Scenario | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)])
        lines.extend(f"| {s} | " + " | ".join(cell(summary[s][n]) for n in names) + " |" for s in rows)
        lines.append("")

    table("Success rate (%)", "Share of payments where money moved.", lambda c: f"{c['success_pct']:.2f}")
    table("Regret (payments lost vs oracle, per 6,000)", "Lower is better.", lambda c: f"{c['regret']:.1f} [{c['regret_ci'][0]:.1f}, {c['regret_ci'][1]:.1f}]")
    table("Payments saved vs static (per 6,000)", "Higher is better.", lambda c: f"{c['saved']:+.1f} [{c['saved_ci'][0]:+.1f}, {c['saved_ci'][1]:+.1f}]")
    table("Recovery: degraded seconds", f"Seconds in {WINDOW_S}-second windows where first-try health was {DEGRADED_PTS}+ points below the same policy's calm world. Lower means faster recovery; calm shows the noise floor.", lambda c: f"{c['degraded_s']:.1f}")
    table("Fees (rupees per 6,000 payments)", "Gateway fees on successful charges.", lambda c: f"{c['fees_rupees']:,.0f}")
    table("First-try latency (ms)", "Average gateway delay the customer waits for.", lambda c: f"{c['latency_ms']:.0f}")

    lines += ["## Thompson vs rolling + breaker (where the bandit loses)", "", "| Scenario | Change (points) | 95% range | Verdict |", "|---|---:|---|---|"]
    lines += [f"| {s} | {summary[s]['thompson_vs_rolling']['pts']:+.3f} | {summary[s]['thompson_vs_rolling']['ci'][0]:+.3f} to {summary[s]['thompson_vs_rolling']['ci'][1]:+.3f} | {summary[s]['thompson_vs_rolling']['verdict']} |" for s in rows]
    losses = [s for s in rows if summary[s]["thompson_vs_rolling"]["pts"] < 0]
    lines += ["", f"The bandit is behind rolling on average in: {', '.join(losses) or 'none'}. A LOSES verdict means the whole range is below zero.", ""]
    return "\n".join(lines)


def main(group="tuning"):  # One command: run everything, then write the JSON and the report.
    started = time.time()
    scenarios = ["calm"] + list(load_scenarios(group, unlock=group == "locked"))  # CAREFUL: "locked" opens the exam.
    jobs = [(n, s, seed, group) for n in POLICIES for s in scenarios for seed in SEEDS]
    with Pool() as pool:  # One worker per CPU core.
        results = {job[:3]: r for job, r in pool.imap_unordered(_task, jobs, chunksize=4)}
    summary = summarise(results, scenarios)
    name = "replay-report" if group == "locked" else f"replay-{group}"  # The locked run is the final report.
    RESULTS.mkdir(parents=True, exist_ok=True)
    meta = {"group": group, "date": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - started),
            "command": "uv run python -m paisa.replay" + (" locked" if group == "locked" else ""), "fingerprint": fingerprint()}
    old = RESULTS / f"{name}.json"
    if group == "locked":  # Record when the exam was first seen, and whether the code changed after.
        before = json.loads(old.read_text())["meta"] if old.exists() else {}
        meta["first_opened"] = before.get("first_opened", meta["date"])
        meta["changed_after_opening"] = before.get("changed_after_opening", False) or before.get("fingerprint", meta["fingerprint"]) != meta["fingerprint"]
    old.write_text(json.dumps({"meta": meta, "summary": summary}, indent=2))
    (RESULTS / f"{name}.md").write_text(report(summary, meta), encoding="utf-8")
    print(report(summary, meta))
