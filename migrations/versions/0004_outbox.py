"""
WHAT: Alembic wrapper that applies or removes the Phase 5 outbox table.
WHY: the schema stays in readable SQL, while Alembic records the database version.
USES: 0004_outbox.sql.
SAFE TO CHANGE: do not edit after use. Add a new migration file instead.
"""

from pathlib import Path  # Finds the SQL file beside this one.

from alembic import op  # Gives access to the current migration's connection.

revision = "0004"  # This migration's version ID.
down_revision = "0003"  # It must run after the routing details.


def upgrade():  # Applies this migration.
    sql = Path(__file__).with_suffix(".sql").read_text("utf-8")  # Read the schema.
    op.get_bind().connection.cursor().execute(sql)  # Run it inside Alembic's transaction.


def downgrade():  # Removes this migration.
    # CAREFUL: this permanently deletes unpublished events.
    op.get_bind().connection.cursor().execute("DROP TABLE outbox")
