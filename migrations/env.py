"""
WHAT: tells Alembic how to connect to the database and run migrations.
WHY: Alembic needs this file to apply each schema version in order.
USED BY: migrate() in src/paisa/adapters/postgres.py.
SAFE TO CHANGE: rarely needed.
"""

from alembic import context  # Details of the current migration run.
from sqlalchemy import create_engine  # Alembic's required database connector.

engine = create_engine(context.config.get_main_option("sqlalchemy.url"))  # Connect using the given address.
with engine.connect() as connection:  # Open one connection.
    context.configure(connection=connection)  # Give it to Alembic.
    with context.begin_transaction():  # All-or-nothing: a failed migration leaves no partial schema.
        context.run_migrations()  # Apply every migration not yet recorded.
engine.dispose()  # Close the connection pool.
