"""
WHAT: describe this schema change.
WHY: explain why it is needed.
SAFE TO CHANGE: only before this migration has run on data you keep.
"""

from alembic import op  # Runs SQL in the current migration.

revision = ${repr(up_revision)}  # This migration's version ID.
down_revision = ${repr(down_revision)}  # The migration that must run first.


def upgrade():  # Applies the change.
    ${upgrades if upgrades else "pass"}


def downgrade():  # Undoes the change.
    ${downgrades if downgrades else "pass"}
