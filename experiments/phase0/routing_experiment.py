"""
WHAT: compares 5 ways of routing payments while gateway health keeps changing.
WHY: before building Paisa-X's learning router, we test whether learning beats a strong rule.
USES: NumPy for maths and matplotlib for the chart.
USED BY: tests/test_phase0.py. Run it with: uv run python experiments/phase0/routing_experiment.py
SAFE TO CHANGE: anything marked CHANGE ME.
ADD A ROUTING METHOD: write a class with choose() and update(), then add one line to POLICIES.
RULES: docs/decisions/0001-phase0-protocol.md was locked before the first run.
       If you change the settings, treat the result as a new experiment.
"""

import json  # Saves results in a file that people and programs can read.
from collections import deque  # A fast queue, used for recent results and delayed feedback.
from dataclasses import dataclass  # Creates a small, clean settings object.
from pathlib import Path  # Builds file paths that work on any operating system.

import matplotlib  # The chart-drawing library.
matplotlib.use("Agg")  # Draw straight to an image file, so no chart window is needed.
import matplotlib.pyplot as plt  # The part of matplotlib used to build charts.
import numpy as np  # Fast maths on arrays of numbers.


@dataclass(frozen=True)  # frozen=True stops settings from changing by accident mid-run.
class Config:  # Every experiment setting lives here, so nobody has to hunt through the code.
    gateways: int = 5  # CHANGE ME: number of simulated gateways.
    payments: int = 20_000  # CHANGE ME: payments per run. More is steadier but slower.
    event_every: int = 1_500  # CHANGE ME: average gap between faults. Lower means a rougher world.
    feedback_delay: int = 20  # CHANGE ME: payments that pass before a result reaches the router.
    explore: float = 0.05  # CHANGE ME: share of rolling-rule traffic used to test other gateways.
    breaker_size: int = 10  # CHANGE ME: how many recent results the circuit breaker watches.
    breaker_failures: int = 7  # CHANGE ME: failures within that group that block a gateway.
    breaker_cooldown: int = 100  # CHANGE ME: payments to wait before sending one test payment.
    memory_options: tuple = (50, 100, 200, 500)  # CHANGE ME: memory lengths tried during tuning.
    tune_seeds: tuple = tuple(range(10))  # Seeds used only to pick settings.
    test_seeds: tuple = tuple(range(1000, 1030))  # Separate seeds used only for the final result.


CONFIG = Config()  # The one shared settings object used by this file and its tests.


def make_world(seed, cfg=CONFIG):  # Builds one reproducible simulated world.
    rng = np.random.default_rng(seed)  # A seed makes the "random" world repeatable.
    base = rng.uniform(0.85, 0.97, cfg.gateways)  # Each gateway's normal success chance.
    probs = np.tile(base, (cfg.payments, 1))  # Table: one row per payment, one column per gateway.
    start = 0  # Position of the latest fault.
    while (start := start + int(rng.integers(cfg.event_every // 2, cfg.event_every * 3 // 2))) < cfg.payments:  # Next fault.
        gateway, kind = rng.integers(cfg.gateways), rng.integers(3)  # Pick a victim and fault type.
        end = min(start + int(rng.integers(200, 1_500)), cfg.payments)  # Pick when the fault ends.
        # Fault types: 0 = outage, 1 = brownout (partly working), 2 = slow decay.
        effect = (0.05, 0.7, np.linspace(1, 0.5, end - start))[kind]  # How much health is multiplied.
        probs[start:end, gateway] *= effect  # Apply the fault to that gateway only.
    # CAREFUL: outcomes are fixed here so every method faces identical luck (common random numbers).
    outcomes = rng.random(probs.shape) < probs  # True means that gateway would succeed.
    return probs, outcomes, base  # Hand back true health, fixed results and normal health.


class Fixed:  # Baseline 1: always use the gateway that is normally best.
    def __init__(self, base, _=None):  # The unused second value keeps all methods' setup the same.
        self.best = int(base.argmax())  # Remember the normally best gateway.

    def choose(self, t, rng):  # Pick a gateway for payment number t.
        return self.best  # The answer never changes.

    def update(self, t, gateway, success):  # Receive a delayed result.
        pass  # This method does not learn.


class RoundRobin(Fixed):  # Baseline 2: take turns. It reuses Fixed's update().
    def __init__(self, base, _=None):  # Set up the method.
        self.count = len(base)  # Remember how many gateways exist.

    def choose(self, t, rng):  # Pick a gateway for payment number t.
        return t % self.count  # Remainder cycles 0, 1, 2, 3, 4, 0, 1...


class Rolling:  # Baseline 3: use the best recent success rate, and sometimes test others.
    def __init__(self, base, window, cfg=CONFIG):  # window = how many recent results to remember.
        self.cfg = cfg  # Keep the settings.
        self.results = [deque(maxlen=window) for _ in base]  # Recent results per gateway.
        self.wins = np.zeros(len(base))  # Running win count, faster than recounting every time.
        self.rates = np.ones(len(base))  # Unknown gateways start at 100%, so each gets tried.
        self.tripped = np.zeros(len(base), bool)  # Blocked gateways. Stays False without a breaker.

    def choose(self, t, rng):  # Pick a gateway for payment number t.
        options = np.flatnonzero(~self.tripped)  # List the gateways that are not blocked.
        options = options if options.size else np.arange(len(self.rates))  # If all are blocked, use all.
        if rng.random() < self.cfg.explore:  # Exploration: a small share of traffic tests others.
            return int(rng.choice(options))  # Pick any allowed gateway at random.
        return int(options[self.rates[options].argmax()])  # Otherwise use the best recent rate.

    def update(self, t, gateway, success):  # Receive a delayed result.
        recent = self.results[gateway]  # Find this gateway's recent results.
        if len(recent) == recent.maxlen:  # If full, the oldest result is about to drop out.
            self.wins[gateway] -= recent[0]  # Remove that oldest result from the win count.
        recent.append(bool(success))  # Add the new result.
        self.wins[gateway] += bool(success)  # Add it to the win count.
        self.rates[gateway] = self.wins[gateway] / len(recent)  # Refresh the success rate.


class RollingBreaker(Rolling):  # Baseline 4, the one to beat: rolling rule + circuit breaker.
    def __init__(self, base, window, cfg=CONFIG):  # Set up the method.
        super().__init__(base, window, cfg)  # Reuse all of the rolling rule's setup.
        self.recent = [deque(maxlen=cfg.breaker_size) for _ in base]  # Short memory for the breaker.
        self.open_until = np.zeros(len(base), int)  # When each blocked gateway may get a test payment.

    def choose(self, t, rng):  # Pick a gateway for payment number t.
        due = np.flatnonzero(self.tripped & (self.open_until <= t))  # Blocked gateways due for a test.
        if due.size:  # Half-open state: carefully test whether a gateway has recovered.
            self.open_until[due[0]] = t + self.cfg.breaker_cooldown  # Allow only one test per cooldown.
            return int(due[0])  # Send this one payment as the test.
        return super().choose(t, rng)  # Otherwise route normally.

    def update(self, t, gateway, success):  # Receive a delayed result.
        if self.tripped[gateway]:  # While blocked, only a successful test matters.
            if success:  # The gateway appears to have recovered.
                self.tripped[gateway] = False  # Close the breaker, allowing traffic again.
                self.results[gateway].clear(), self.recent[gateway].clear()  # Forget outage data.
                self.wins[gateway], self.rates[gateway] = 0, 1  # Restart its score fresh.
            return  # Ignore late failures from before the block.
        super().update(t, gateway, success)  # Update the normal rolling rate.
        self.recent[gateway].append(bool(success))  # Add the result to the breaker's memory.
        failures = len(self.recent[gateway]) - sum(self.recent[gateway])  # Count recent failures.
        if failures >= self.cfg.breaker_failures:  # Too many failures means it is probably broken.
            self.tripped[gateway] = True  # Open the breaker, blocking normal traffic.
            self.open_until[gateway] = t + self.cfg.breaker_cooldown  # Set the next test time.


class Thompson:  # Challenger: discounted Thompson Sampling, a learning method.
    def __init__(self, base, half_life):  # half_life = updates until old evidence counts half.
        self.alpha, self.beta = np.ones(len(base)), np.ones(len(base))  # Start with no opinion.
        self.keep = 0.5 ** (1 / half_life)  # Share of the evidence kept after each update.

    def choose(self, t, rng):  # Pick a gateway for payment number t.
        # Beta distribution = a range of believable success rates. Less data means a wider range.
        return int(rng.beta(self.alpha, self.beta).argmax())  # Draw one guess each, pick the highest.

    def update(self, t, gateway, success):  # Receive a delayed result.
        self.alpha = 1 + (self.alpha - 1) * self.keep  # Fade old success evidence toward the start.
        self.beta = 1 + (self.beta - 1) * self.keep  # Fade old failure evidence too.
        self.alpha[gateway] += bool(success)  # A success raises this gateway's belief.
        self.beta[gateway] += not success  # A failure lowers it.


# ADD A NEW METHOD HERE: "Display name": (ClassName, settings to try during tuning).
POLICIES = {
    "Fixed": (Fixed, (None,)),  # No settings to tune.
    "Round-robin": (RoundRobin, (None,)),  # No settings to tune.
    "Rolling": (Rolling, CONFIG.memory_options),  # Tune the window length.
    "Rolling + breaker": (RollingBreaker, CONFIG.memory_options),  # Same options, for fairness.
    "Thompson Sampling": (Thompson, CONFIG.memory_options),  # Same options, for fairness.
}


def run(policy, outcomes, delay, rng):  # Sends every payment through one routing method.
    chosen = np.empty(len(outcomes), int)  # Stores the gateway chosen for each payment.
    pending = deque()  # Results still travelling back to the router.
    for t in range(len(outcomes)):  # Handle payments one at a time, in order.
        while pending and pending[0][0] <= t:  # Deliver every result that is now due.
            policy.update(t, *pending.popleft()[1:])  # CAREFUL: the router never sees results early.
        chosen[t] = policy.choose(t, rng)  # Ask the method for a gateway.
        pending.append((t + delay, chosen[t], outcomes[t, chosen[t]]))  # Schedule the result.
    return outcomes[np.arange(len(outcomes)), chosen], chosen  # Return successes and choices.


def score(name, option, seed, cfg=CONFIG):  # Returns one method's success rate in one world.
    _, outcomes, base = make_world(seed, cfg)  # Build the world for this seed.
    policy = POLICIES[name][0](base, option)  # Create the routing method.
    return run(policy, outcomes, cfg.feedback_delay, np.random.default_rng([seed, 1]))[0].mean()  # Run it.


def bootstrap_ci(values, seed=0, draws=10_000):  # Estimates a 95% confidence interval.
    rng = np.random.default_rng(seed)  # Keep the statistics reproducible too.
    # Bootstrap = resample our results many times to see how much the average could vary.
    picks = rng.integers(len(values), size=(draws, len(values)))  # Random positions, with repeats.
    return tuple(np.percentile(np.asarray(values)[picks].mean(axis=1), [2.5, 97.5]).tolist())  # Middle 95%.


def save_chart(summary, oracle, path):  # Draws the results as one simple dot chart.
    names = sorted(summary, key=lambda n: summary[n]["success"])  # Worst at the bottom, best on top.
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=160)  # Create the image.
    for y, name in enumerate(names):  # Draw one row per method.
        row, color = summary[name], "#2563eb" if name == "Thompson Sampling" else "#6b7280"  # Highlight challenger.
        x = 100 * row["success"]  # Convert the rate to a percentage.
        err = [[x - 100 * row["ci"][0]], [100 * row["ci"][1] - x]]  # Distance to the interval ends.
        ax.errorbar(x, y, xerr=err, fmt="o", ms=8, color=color, elinewidth=2)  # Dot and range.
        ax.annotate(f"{x:.2f}%", (x, y), xytext=(0, 9), textcoords="offset points", ha="center", color="#111827")  # Value.
    ax.axvline(100 * oracle, color="#111827", ls="--", lw=1)  # Mark the best possible average.
    ax.annotate(f"Perfect knowledge {100 * oracle:.2f}%", (100 * oracle, -0.45), xytext=(-5, 0), textcoords="offset points", ha="right", color="#374151")  # Label it.
    ax.set(yticks=range(len(names)), yticklabels=names, ylim=(-0.7, len(names) - 0.4), xlabel="Payment success rate (%), mean across 30 test seeds, with 95% interval")  # Axes.
    ax.spines[["top", "right", "left"]].set_visible(False), ax.grid(axis="x", alpha=0.2)  # Keep it clean.
    ax.set_title("Phase 0: routing methods on simulated gateway failures", loc="left", weight="bold")  # Title.
    fig.tight_layout(), fig.savefig(path), plt.close(fig)  # Save the image and free memory.


def main(cfg=CONFIG):  # Runs the full locked experiment.
    # Step 1: tune each method on tuning seeds only.
    best = {n: max(opts, key=lambda o: np.mean([score(n, o, s, cfg) for s in cfg.tune_seeds])) for n, (_, opts) in POLICIES.items()}
    # Step 2: score each method on separate test seeds.
    scores = {n: np.array([score(n, best[n], s, cfg) for s in cfg.test_seeds]) for n in POLICIES}
    oracle = float(np.mean([make_world(s, cfg)[0].max(axis=1).mean() for s in cfg.test_seeds]))  # Best possible.
    summary = {n: {"memory": best[n], "success": float(v.mean()), "ci": bootstrap_ci(v), "regret": oracle - float(v.mean())} for n, v in scores.items()}  # Summary table.
    # Step 3: apply the locked rule to paired differences (same seed, same luck).
    diff = scores["Thompson Sampling"] - scores["Rolling + breaker"]  # Gain per seed.
    low, high = bootstrap_ci(diff)  # Confidence range for that gain.
    result = {"verdict": "PASS" if low > 0 else "NOT PASSED", "gain": float(diff.mean()), "gain_ci": [low, high], "seeds_won": int((diff > 0).sum()), "seeds": len(diff), "oracle": oracle, "methods": summary}  # Final result.
    out = Path(__file__).parent / "results"  # Results folder beside this file.
    out.mkdir(exist_ok=True)  # Create it if needed.
    (out / "results.json").write_text(json.dumps(result, indent=2))  # Save the numbers.
    save_chart(summary, oracle, out / "phase0_results.png")  # Save the chart.
    print(json.dumps(result, indent=2))  # Show the result in the terminal.


if __name__ == "__main__":  # Run only when this file is started directly, not when tests import it.
    main()  # Start the experiment.
