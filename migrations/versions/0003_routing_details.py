"""
WHAT: Alembic wrapper that applies or removes the Phase 4 routing columns.
WHY: the schema stays in readable SQL, while Alembic records the database version.
USES: 0003_routing_details.sql.
SAFE TO CHANGE: do not edit after use. Add a new migration file instead.
"""

from pathlib import Path  # Finds the SQL file beside this one.

from alembic import op  # Gives access to the current migration's connection.

revision = "0003"  # This migration's version ID.
down_revision = "0002"  # It must run after the recovery schema.


def upgrade():  # Applies this migration.
    sql = Path(__file__).with_suffix(".sql").read_text("utf-8")  # Read the schema.
    op.get_bind().connection.cursor().execute(sql)  # Run it inside Alembic's transaction.


def downgrade():  # Removes this migration.
    # CAREFUL: this permanently deletes the saved routing details.
    op.get_bind().connection.cursor().execute("ALTER TABLE routing_decisions DROP COLUMN fallback, DROP COLUMN explored, DROP COLUMN scores")
