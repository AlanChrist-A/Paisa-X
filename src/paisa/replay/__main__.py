"""
WHAT: entry point for `uv run python -m paisa.replay [locked]`.
WHY: `python -m` runs from the project folder, so the simulator package can be imported.
"""

import sys  # Reads the optional "locked" argument.

from paisa.replay.lab import main  # The Replay Lab.

if __name__ == "__main__":  # CAREFUL: worker processes import this file too; only the parent may start a run.
    main(sys.argv[1] if len(sys.argv) > 1 else "tuning")
