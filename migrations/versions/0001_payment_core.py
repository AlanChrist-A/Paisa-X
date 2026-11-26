"""
WHAT: Alembic wrapper that applies or removes the Phase 2 schema.
WHY: the schema stays in readable SQL, while Alembic records which version each database has.
USES: 0001_payment_core.sql.
SAFE TO CHANGE: do not edit after use. Add a new migration file instead.
"""

from pathlib import Path  # Finds the SQL file beside this one.

from alembic import op  # Gives access to the current migration's database connection.

revision = "0001"  # This migration's version ID.
down_revision = None  # It is the first migration.


def upgrade():  # Applies this migration.
    sql = Path(__file__).with_suffix(".sql").read_text("utf-8")  # Read the schema.
    op.get_bind().connection.cursor().execute(sql)  # Run it as plain SQL, inside Alembic's transaction.


def downgrade():  # Removes this migration.
    # CAREFUL: this permanently deletes every Paisa-X table and its data.
    op.get_bind().connection.cursor().execute("DROP TABLE gateway_config, routing_decisions, attempt_transitions, payment_attempts, idempotency_keys, payments, merchants; DROP FUNCTION block_change, keep_final_state")
