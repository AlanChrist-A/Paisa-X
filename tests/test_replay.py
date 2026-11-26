"""
WHAT: tests for the Phase 8 Replay Lab: the oracle, repeatability, recovery maths and the exam guard.
WHY: if the yardstick is wrong, every number in the final report is wrong.
"""

import pytest  # Test framework.

from paisa.replay import lab  # Code under test.
from simulator.engine import Fault, GatewaySim  # The simulated world.

CTX = {"method": "UPI", "bank": "BANK_A", "amount_paise": 10_000, "now": 100.0}  # A payment at 100 s.


def test_oracle_avoids_a_down_gateway():  # True health 0 during an outage, so it goes last.
    oracle = lab.OraclePolicy()
    oracle.world = GatewaySim(1, [Fault("outage", "alpha", 0, 200_000)], lab.CATALOG)
    ranked, explored = oracle.rank("p1", ["alpha", "bravo"], {}, CTX)
    assert ranked == ["bravo", "alpha"] and not explored


def test_oracle_sees_decay():  # A decaying gateway scores below its normal health.
    sim = GatewaySim(1, [Fault("decay", "alpha", 0, 200_000, value=0.4)], lab.CATALOG)
    oracle, payment = lab.OraclePolicy(), {"method": "UPI", "bank": "BANK_A"}
    oracle.world = sim
    assert oracle.health("alpha", payment, 100_000) < lab.CATALOG["gateways"]["alpha"]["success"]["UPI"]


def test_run_is_repeatable_and_oracle_is_hard_to_beat(monkeypatch):  # Same world, same answer; oracle wins on an outage.
    monkeypatch.setattr(lab, "PAYMENTS", 1_500)  # Short world: faults start at 60 s = payment 1,200.
    first = lab.run(lab.RollingPolicy(), "full_outage", 3, "immediate")
    assert first == lab.run(lab.RollingPolicy(), "full_outage", 3, "immediate")
    assert lab.run(lab.OraclePolicy(), "full_outage", 3, "immediate")["successes"] >= first["successes"]


def test_degraded_seconds():  # Only windows well below the calm level count.
    calm = {"windows": [[90, 100]] * 3}  # 90% healthy.
    hurt = {"windows": [[90, 100], [70, 100], [88, 100], [10, 20]]}  # One bad window; one too small to judge.
    assert lab.degraded_s(hurt, calm) == lab.WINDOW_S


def test_locked_scenarios_stay_locked():  # The exam opens only on purpose.
    with pytest.raises(PermissionError):
        lab.load_scenarios("locked")
