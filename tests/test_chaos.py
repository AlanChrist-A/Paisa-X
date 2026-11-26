"""
WHAT: proves `paisa chaos` exits with an error when a drill fails or crashes.
WHY: the exit code is what CI and scripts check. A chaos run that always "passes" is worse than none.
"""

import pytest  # Test framework.

from paisa import chaos  # Code under test.


@pytest.mark.parametrize("drill", [lambda: {}, lambda: 1 / 0])  # A broken rule, and a crashed drill.
def test_chaos_exits_1_when_a_drill_fails(monkeypatch, tmp_path, drill):
    for name, fake in [("stop_apps", lambda: None), ("docker_ready", lambda: True), ("wait_for", lambda *a: None)]:
        monkeypatch.setattr(chaos.stack, name, fake)  # No real processes or Docker.
    monkeypatch.setattr(chaos.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(chaos.postgres, "start", lambda: False)
    monkeypatch.setattr(chaos, "RESULTS", tmp_path / "chaos.json")
    monkeypatch.setattr(chaos, "DRILLS", [("ok", lambda: {}, lambda r: True), ("bad", drill, lambda r: False)])
    with pytest.raises(SystemExit) as stop:
        chaos.main()
    assert stop.value.code == 1 and '"bad": "FAIL"' in (tmp_path / "chaos.json").read_text()
