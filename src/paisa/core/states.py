"""
WHAT: the allowed steps of a payment and of each gateway attempt (state machines).
WHY: one small table stops impossible jumps, such as a finished payment changing its result.
USES: nothing. This is pure logic with no database or network.
USED BY: services/payments.py, the audit and tests.
SAFE TO CHANGE: add a state by adding it here, in the migration's CHECK list, and in tests.
"""

PAYMENT = {  # Each state lists where a payment may go next.
    "CREATED": {"PROCESSING", "FAILED"},  # Saved, with no gateway chosen yet.
    "PROCESSING": {"AWAITING_CUSTOMER", "PENDING_CONFIRMATION", "SUCCEEDED", "FAILED"},  # Gateway is being called.
    "AWAITING_CUSTOMER": {"PROCESSING", "PENDING_CONFIRMATION", "SUCCEEDED", "FAILED"},  # Customer must approve.
    "PENDING_CONFIRMATION": {"PROCESSING", "SUCCEEDED", "FAILED"},  # Result unknown, so money may have moved.
    "SUCCEEDED": set(),  # CAREFUL: final state.
    "FAILED": set(),  # CAREFUL: final state.
}

ATTEMPT = {  # Each state lists where one gateway attempt may go next.
    "INITIATED": {"AWAITING_CUSTOMER", "SUCCEEDED", "DECLINED", "FAILED_SAFE", "UNKNOWN"},  # Saved before the call.
    "AWAITING_CUSTOMER": {"SUCCEEDED", "DECLINED", "FAILED_SAFE", "UNKNOWN"},  # Customer was prompted.
    "UNKNOWN": {"SUCCEEDED", "DECLINED", "FAILED_SAFE"},  # We must find out what happened.
    "DECLINED": {"SUCCEEDED"},  # Business refusal. Moves only on a rare late success.
    "FAILED_SAFE": {"SUCCEEDED"},  # Proven no money. Moves only if a gateway misbehaves.
    "SUCCEEDED": {"REVERSED"},  # Money moved. Only a duplicate may be reversed.
    "REVERSED": set(),  # Money returned, so nothing further can happen.
}

FINAL_PAYMENT = {"SUCCEEDED", "FAILED"}  # Payment states that can never change.
PAYMENT_FOR_ATTEMPT = {  # What an attempt's result means for its payment.
    "AWAITING_CUSTOMER": "AWAITING_CUSTOMER",  # Keep waiting for the customer.
    "SUCCEEDED": "SUCCEEDED",  # Money moved.
    "DECLINED": "FAILED",  # A business refusal ends the payment.
    "FAILED_SAFE": "FAILED",  # Ends the payment unless settlement finds failover is allowed.
    "UNKNOWN": "PENDING_CONFIRMATION",  # CAREFUL: never report failure while money may have moved.
}


class IllegalTransition(ValueError):  # A clear error for a forbidden state change.
    pass  # It needs no extra behaviour.


def move(machine, current, target):  # Checks one state change.
    if current == target:  # A repeated event, such as a duplicate webhook.
        return False  # Safe, but nothing needs changing.
    if target not in machine[current]:  # This path is not in the table.
        raise IllegalTransition(f"{current} cannot move to {target}")  # Stop.
    return True  # The change is allowed.
