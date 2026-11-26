"""
WHAT: runs the REAL payment flow (Postgres, Recovery Worker, simulator) with the learning router on
      all traffic, kills the busiest gateway mid-run, then audits every paisa.
WHY: Phase 7 is done only if the money audit stays clean with the bandit live.
RUN: uv run paisa db-start, then uv run python -m experiments.phase7.live_drill (about 1-2 minutes).
"""

import json  # Saves results.
from pathlib import Path  # Results folder.

from experiments.phase3.soak import World, run_traffic, temporary_database  # Virtual-clock world.
from paisa.adapters import postgres  # Test merchant.
from paisa.core.routing.bandit import ThompsonPolicy  # The learning brain.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.pipeline.audit import audit_records  # Money audit.
from simulator.engine import Fault, make_traffic  # Faults and traffic.


def main(seed=11, seconds=90, start_s=20, end_s=80):  # Same shape as the Phase 4 outage drill.
    with temporary_database("paisa_p7") as url:  # Disposable database.
        merchant_id, _ = postgres.create_merchant(url)
        policy, killed = ThompsonPolicy(), []  # Brain, and the gateway killed.
        router = Router(policy)  # The router the payment flow will use.
        world = World(url, merchant_id, seed, router=router)

        def each_payment(w):  # Before every payment: start the outage once. Learning happens inside the payment flow.
            if not killed and w.ms >= start_s * 1000:  # Outage time: kill the busiest gateway.
                with w.pool.connection() as conn:
                    killed.append(conn.execute("SELECT gateway FROM payment_attempts GROUP BY gateway ORDER BY count(*) DESC, gateway LIMIT 1").fetchone()["gateway"])
                w.sim.faults.append(Fault("outage", killed[0], w.ms, end_s * 1000))

        try:
            run_traffic(world, make_traffic(seed, seconds * 5, 5), each_payment)
            report = audit_records(url, world.sim.truth())  # Check the money.
        finally:
            world.close()
    result = {"policy": policy.version, "gateway_killed": killed[0], "beliefs": len(policy.beliefs), "alpha_beta": {str(k): [round(v[0], 1), round(v[1], 1)] for k, v in policy.beliefs.items()}, "audit": report}
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    (out / "phase7_live.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
