"""
WHAT: shared test setup for anything that needs Postgres.
WHY: every database test should start from clean tables in a private test database.
USES: the portable Postgres controlled by src/paisa/adapters/postgres.py.
SAFE TO CHANGE: add shared fixtures here. A fixture is setup code pytest runs for a test.
"""

import os  # Reads this process ID for a unique database name.

import psycopg  # Connects to Postgres.
import pytest  # Test framework.

from paisa.adapters import postgres  # Starts, migrates and connects to local Postgres.
from paisa.core.routing.policies import StaticPolicy  # Predictable routing for tests.
from paisa.core.routing.router import Router  # Routing engine.
from paisa.services import payments  # Holds the process's router.


@pytest.fixture(autouse=True)  # Runs for every test.
def fresh_router(monkeypatch):  # Stops one test's gateway health from affecting another.
    monkeypatch.setattr(payments, "router", Router(StaticPolicy()))  # Predictable order, unless a test chooses otherwise.


@pytest.fixture(scope="session")  # Create once for the entire test run.
def database_url():  # Builds a private, fully migrated test database.
    we_started_it = postgres.start()  # Start Postgres only if it is not already running.
    name = f"paisa_test_{os.getpid()}"  # A unique name, so runs never collide.
    with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:  # CREATE DATABASE needs autocommit.
        admin.execute(f'CREATE DATABASE "{name}"')  # Create the test database.
    url = postgres.url(name)  # Connection address for it.
    postgres.migrate(url)  # Build all tables with the real migrations.
    yield url  # Tests run here.
    with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:  # Clean up afterwards.
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')  # CAREFUL: only this temporary database.
    if we_started_it:  # Leave Postgres as we found it, saving RAM.
        postgres.stop()  # Stop the server.


@pytest.fixture
def db(database_url):  # Gives a test a clean database connection.
    with psycopg.connect(database_url, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:  # Open it.
        conn.execute("TRUNCATE merchants, payments, attempt_transitions, routing_decisions, webhook_inbox, reversal_tasks, outbox CASCADE")  # Empty data tables.
        yield conn  # The test runs here.


@pytest.fixture
def merchant(db, database_url):  # Creates one test merchant and its login headers.
    merchant_id, api_key = postgres.create_merchant(database_url)  # Store only the key's hash.
    return {"id": merchant_id, "headers": {"Authorization": f"Bearer {api_key}"}}  # Ready for API calls.
