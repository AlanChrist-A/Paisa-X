"""
WHAT: the simple Phase 2 router: pick the highest-priority gateway that supports the payment.
WHY: the safe payment flow should be proven before smarter routing is added.
USES: nothing. This is pure logic with no database or network.
USED BY: services/payments.py.
SAFE TO CHANGE: Phase 4 adds health checks and rolling success rates.
"""


def eligible(payment, configs):  # Lists gateways able to handle this payment, best priority first.
    def supports(cfg):  # Checks one gateway.
        network_ok = payment["method"] != "CARD" or payment.get("network") in cfg["networks"]  # Cards need a network.
        return payment["method"] in cfg["methods"] and payment["bank"] not in cfg["skip_banks"] and network_ok  # All rules.
    return [cfg["gateway"] for cfg in sorted(configs, key=lambda c: c["priority"]) if supports(cfg)]  # Lower number first.


def choose_gateway(payment, configs):  # Picks one gateway, or None if none can serve it.
    options = eligible(payment, configs)  # Find valid gateways.
    return options[0] if options else None  # Use the first one.
