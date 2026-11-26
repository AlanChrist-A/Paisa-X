"""
WHAT: compares Paisa-X's records with the simulator's hidden money ledger.
WHY: the system should not be trusted just because it says it worked.
USES: Postgres, the simulator's truth endpoint, and the state rules.
USED BY: `uv run paisa audit` and tests.
SAFE TO CHANGE: add a check by adding one entry to `violations`.
"""

import httpx  # Reads the simulator's truth.
import psycopg  # Reads Paisa-X's records.
from psycopg.rows import dict_row  # Returns rows as dictionaries.

from paisa.core.states import ATTEMPT, FINAL_PAYMENT  # State rules.


def legal(row):  # Checks one history row.
    if row["from_state"] is None:  # First row of an attempt.
        return row["to_state"] == "INITIATED"  # Every attempt must start here.
    return row["to_state"] in ATTEMPT.get(row["from_state"], ())  # Must be an allowed move.


def audit(payments, transitions, net_paise):  # Pure comparison, with no database or network.
    known = {p["id"] for p in payments}  # IDs Paisa-X knows about.
    net = lambda p: net_paise.get(p["id"], 0)  # Money really taken for this payment.
    violations = {  # CAREFUL: every value here must be zero.
        "overcharged": sum(net(p) > p["amount_paise"] for p in payments),  # Customer paid too much.
        "success_without_charge": sum(p["state"] == "SUCCEEDED" and net(p) < p["amount_paise"] for p in payments),  # We claimed money we lack.
        "failed_with_money": sum(p["state"] == "FAILED" and net(p) != 0 for p in payments),  # We said failed, but kept money.
        "charge_without_payment": sum(pid not in known and amount != 0 for pid, amount in net_paise.items()),  # Money with no record.
        "illegal_transitions": sum(not legal(row) for row in transitions),  # History broke the state machine.
    }
    unresolved = sum(p["state"] not in FINAL_PAYMENT for p in payments)  # Not a violation, but needs recovery.
    return {"payments": len(payments), "unresolved": unresolved, "violations": violations}  # Final report.


def run_audit(database_url, simulator_url):  # Loads the simulator's truth and audits against it.
    truth = httpx.get(f"{simulator_url}/sim/admin/truth", timeout=10).raise_for_status().json()  # Hidden ledger.
    return audit_records(database_url, truth)  # Compare it with our records.


def audit_records(database_url, truth):  # Audits our database against a supplied truth ledger.
    with psycopg.connect(database_url, row_factory=dict_row) as conn:  # Open the database.
        payments = conn.execute("SELECT id::text, amount_paise, state FROM payments").fetchall()  # All payments.
        transitions = conn.execute("SELECT from_state, to_state FROM attempt_transitions").fetchall()  # All history.
        pending = conn.execute("SELECT count(*) AS n FROM reversal_tasks WHERE done_at IS NULL").fetchone()["n"]  # Money still to return.
    return {**audit(payments, transitions, truth["net_paise"]), "pending_reversals": pending}  # Compare and report.
