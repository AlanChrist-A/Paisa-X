"""
WHAT: deliberate crash switches for testing.
WHY: the safest way to prove crash recovery is to really kill the server at each risky step.
USED BY: services/payments.py and crash tests.
CAREFUL: never set PAISA_CRASH_AT outside a test. Doing so makes the server die on purpose.
"""

import os  # Reads the setting and exits the process.


def crash_if(point):  # Kills the process if this is the chosen crash point.
    if os.getenv("PAISA_CRASH_AT") == point:  # Was this exact point requested?
        os._exit(137)  # Exit instantly, with no cleanup, just like a real power loss to this process.
