"""
WHAT: checks the import rule from ARCHITECTURE.md: core/ may not import any other Paisa-X layer.
WHY: core holds the money and routing rules. Keeping it pure keeps it testable, and lets the
     Replay Lab run exactly the same code as live payments.
"""

import re  # Finds import lines.
from pathlib import Path  # Walks the core folder.

CORE = Path(__file__).resolve().parents[1] / "src" / "paisa" / "core"  # The pure layer.
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(paisa\.(?!core\b)\w+|simulator|experiments)", re.M)  # Any other layer.


def test_core_imports_only_core():
    broken = {f.name: FORBIDDEN.findall(f.read_text("utf-8")) for f in CORE.rglob("*.py")}
    assert not {name: hits for name, hits in broken.items() if hits}  # Shows the offending files if any.
