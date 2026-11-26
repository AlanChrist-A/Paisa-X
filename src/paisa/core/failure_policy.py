"""
WHAT: turns a gateway's answer into one safe attempt state.
WHY: treating an unclear answer as "failed" is how customers get charged twice.
USES: nothing. This is pure logic with no database or network.
USED BY: services/payments.py.
SAFE TO CHANGE: add new gateway answers to DIRECT.
"""

DIRECT = {  # Answers that clearly state what happened.
    "SUCCEEDED": "SUCCEEDED",  # The gateway took the money.
    "AWAITING_CUSTOMER": "AWAITING_CUSTOMER",  # The customer has a prompt.
    "DECLINED": "DECLINED",  # A business refusal, such as low balance.
    "VOIDED": "FAILED_SAFE",  # This attempt is permanently blocked.
}


MAX_ATTEMPTS = 3  # CHANGE ME: tries per payment. Must stay within the database limit of 3.


def can_failover(attempts_so_far, customer_prompted, now, deadline, options):  # May we try another gateway?
    # CAREFUL: after a customer prompt, the customer approved one specific request, so never move it silently.
    return not customer_prompted and attempts_so_far < MAX_ATTEMPTS and now < deadline and bool(options)


def health_signal(target, code=None):  # True = healthy, False = unhealthy, None = says nothing about the gateway.
    if target == "DECLINED" or code == "NOT_SUPPORTED":  # A customer refusal or unsupported payment.
        return None  # CAREFUL: do not punish a gateway for these.
    return target in ("SUCCEEDED", "AWAITING_CUSTOMER")  # Accepted means healthy. Failure or timeout means unhealthy.


def classify(reply):  # Returns the attempt state for a gateway reply.
    result = (reply or {}).get("result")  # None means there was no readable reply.
    if result in ("FAILED", "UNSUPPORTED") and reply.get("processed") is False:  # Explicit promise.
        return "FAILED_SAFE"  # The gateway guarantees no money moved.
    return DIRECT.get(result, "UNKNOWN")  # CAREFUL: anything unclear means money may have moved.
