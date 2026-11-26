"""
WHAT: starts, stops, migrates and connects to the portable local Postgres.
WHY: Paisa-X can run on this laptop without Docker, a Windows service or admin rights.
USES: PostgreSQL binaries in .local/pgsql, and Alembic migrations.
USED BY: the API, the `paisa` command and tests.
SAFE TO CHANGE: anything marked CHANGE ME.
"""

import hashlib  # Hashes API keys before storing them.
import os  # Reads the optional external server address.
import secrets  # Creates strong random passwords and keys.
import subprocess  # Runs Postgres's command-line tools.
import uuid  # Creates merchant IDs.
from pathlib import Path  # Builds file paths that work on any operating system.

import psycopg  # Connects to Postgres.
from alembic import command  # Runs migrations from Python.
from alembic.config import Config  # Holds migration settings.

ROOT = Path(__file__).resolve().parents[3]  # The Paisa-X project folder.
LOCAL = ROOT / ".local"  # Private, git-ignored machine data.
BIN, DATA = LOCAL / "pgsql" / "bin", LOCAL / "pgdata"  # Postgres programs and database files.
PASSWORD = LOCAL / "db_password"  # CAREFUL: local secret, never commit it.
PORT = 55432  # CHANGE ME: uses a non-default port to avoid other Postgres installs.
EXTERNAL = os.getenv("PAISA_PG_URL")  # CI only: an existing server, as postgresql://user:pw@host:port/{database}. Unset = portable Postgres.
SERVER_OPTIONS = f"-p {PORT} -c listen_addresses=127.0.0.1 -c shared_buffers=64MB -c max_connections=30"  # CHANGE ME: small memory footprint, reachable from this laptop only.


def url(database="paisa"):  # Builds the connection address.
    if EXTERNAL:  # CI: someone else runs the server.
        return EXTERNAL.format(database=database)
    return f"postgresql://paisa:{PASSWORD.read_text().strip()}@127.0.0.1:{PORT}/{database}"  # Uses the local password.


def running():  # Is the server up?
    if EXTERNAL:  # Not ours to check.
        return True
    return DATA.exists() and subprocess.run([BIN / "pg_ctl", "status", "-D", DATA], stdout=subprocess.DEVNULL).returncode == 0  # 0 means running.


def start():  # Starts Postgres. Returns True only if this call started it.
    if EXTERNAL:  # Already running elsewhere.
        return False
    if running():  # Nothing to do.
        return False  # Someone else owns this running server.
    first_run = not DATA.exists()  # A new database folder must be initialised.
    if first_run:  # First time on this machine.
        PASSWORD.write_text(secrets.token_hex(24))  # Generate a strong local password.
        subprocess.run([BIN / "initdb", "-D", DATA, "-U", "paisa", "--auth=scram-sha-256", f"--pwfile={PASSWORD}", "-E", "UTF8", "--locale=C"], check=True, stdout=subprocess.DEVNULL)  # Create the data folder.
    # CAREFUL: output goes to a log file, because a captured pipe can hang when the server keeps running.
    subprocess.run([BIN / "pg_ctl", "start", "-w", "-D", DATA, "-l", LOCAL / "postgres.log", "-o", SERVER_OPTIONS], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if first_run:  # Create the main application database once.
        with psycopg.connect(url("postgres"), autocommit=True) as admin:  # CREATE DATABASE needs autocommit.
            admin.execute("CREATE DATABASE paisa")  # Create it.
    return True  # This call started the server.


def stop():  # Stops Postgres cleanly, which also frees its memory.
    if EXTERNAL:  # Never stop someone else's server.
        return
    if running():  # Only if it is up.
        subprocess.run([BIN / "pg_ctl", "stop", "-w", "-m", "fast", "-D", DATA], check=True, stdout=subprocess.DEVNULL)  # Fast = finish safely without waiting for clients.


def migrate(database_url=None):  # Applies every schema change not yet applied.
    config = Config()  # Settings built in code, so no extra config file is needed.
    config.set_main_option("script_location", str(ROOT / "migrations"))  # Where migrations live.
    config.set_main_option("sqlalchemy.url", (database_url or url()).replace("postgresql://", "postgresql+psycopg://"))  # Alembic's address format.
    command.upgrade(config, "head")  # Move the database to the newest version.


def create_merchant(database_url=None):  # Creates a merchant and returns its ID and one-time API key.
    merchant_id, api_key = uuid.uuid4(), f"pxk_{secrets.token_urlsafe(32)}"  # New ID and strong random key.
    with psycopg.connect(database_url or url()) as conn:  # Open a transaction.
        conn.execute("INSERT INTO merchants (id, api_key_hash) VALUES (%s, %s)", (merchant_id, hash_key(api_key)))  # CAREFUL: store the hash only.
    return merchant_id, api_key  # The caller must keep this key, because it cannot be read back.


def hash_key(api_key):  # Converts an API key into its stored fingerprint.
    return hashlib.sha256(api_key.encode()).digest()  # SHA-256 is suitable here because the key is long and random.
