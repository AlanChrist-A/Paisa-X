"""
WHAT: the real Gateway Simulator server with artificial waiting turned off.
WHY: integration tests stay fast while still using a real HTTP connection.
USED BY: tests/test_payment_integration.py.
"""

from simulator.app import create_app  # The normal simulator app builder.

app = create_app(use_latency=False)  # Same behaviour, without sleeping before replies.
