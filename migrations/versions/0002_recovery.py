"""
WHAT: Alembic wrapper that applies or removes the Phase 3 recovery schema.
WHY: the schema stays in readable SQL, while Alembic records the database version.
USES: 0002_recovery.sql.
SAFE TO CHANGE: do not edit after use. Add a new migration file instead.
"""

from pathlib import Path  # Finds the SQL file beside this one.

from alembic import op  # Gives access to the current migration's connection.

revision = "0002"  # This migration's version ID.
down_revision = "0001"  # It must run after the payment core.


def upgrade():  # Applies this migration.
    sql = Path(__file__).with_suffix(".sql").read_text("utf-8")  # Read the schema.
    op.get_bind().connection.cursor().execute(sql)  # Run it inside Alembic's transaction.


def downgrade():  # Removes this migration.
    # CAREFUL: this permanently deletes stored webhooks and reversal tasks.
    op.get_bind().connection.cursor().execute("DROP TABLE reversal_tasks, webhook_inbox; DROP INDEX idempotency_by_payment, payments_interrupted, attempts_due; ALTER TABLE payment_attempts DROP COLUMN must_void, DROP COLUMN check_count")
