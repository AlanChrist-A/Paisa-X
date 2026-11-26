"""
WHAT: the one rule for valid money values in Paisa-X.
WHY: decimals like 0.1 cannot be stored exactly, so all money is whole paise (1 rupee = 100 paise).
USES: nothing. This is pure logic with no database or network.
USED BY: the API request model and tests.
SAFE TO CHANGE: MAX_PAISE.
"""

MAX_PAISE = 100_000_000  # CHANGE ME: largest payment allowed (Rs 10,00,000). Higher allows bigger payments.


def require_paise(amount):  # Returns the amount if it is safe, or stops with an error.
    # CAREFUL: Python treats True as 1, so booleans must be rejected explicitly.
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= MAX_PAISE:
        raise ValueError("Amount must be whole paise, above zero and within the limit.")  # Refuse it.
    return amount  # The value is safe to use.
