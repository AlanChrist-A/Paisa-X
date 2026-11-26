"""
WHAT: the `paisa` command-line tool.
WHY: common jobs should be one short command.
USES: the Postgres adapter and money audit.
EXAMPLES: uv run paisa db-start, uv run paisa migrate, uv run paisa audit
SAFE TO CHANGE: add a new command with one more entry in COMMANDS.
"""

import argparse  # Reads command-line arguments.
import json  # Prints the audit report clearly.
import os  # Reads settings from the environment.
import sys  # Sets the program's exit code.

from paisa import chaos, stack  # Whole-stack start/stop, and the chaos drills.
from paisa.adapters import postgres  # Local database helpers.
from paisa.pipeline.audit import run_audit  # Money audit.
from paisa.pipeline.event_check import check as check_events  # Kafka vs outbox check.
from paisa.pipeline.lake import build as build_lake  # Bronze -> silver -> gold.
from paisa.workers import features, parquet_sink, recovery, relay  # Background workers.


def lake_command():  # Builds the data layers and fails loudly on any quality problem.
    report = build_lake(parquet_sink.DATA_DIR, os.getenv("DATABASE_URL") or postgres.url())  # Run it.
    print(json.dumps({k: v for k, v in report.items() if k != "gold_health"}, indent=2, default=str))  # Show the summary.
    sys.exit(1 if any(report["violations"].values()) else 0)  # Exit code 1 lets scripts detect failure.


def event_check_command():  # Fails loudly if any event is lost or out of order.
    report = check_events(os.getenv("DATABASE_URL") or postgres.url())  # Run it.
    print(json.dumps(report, indent=2))  # Show the result.
    sys.exit(1 if report["missing"] or report["out_of_order"] else 0)  # Duplicates are allowed; loss is not.


def audit_command():  # Runs the audit and fails loudly if money rules broke.
    report = run_audit(os.getenv("DATABASE_URL") or postgres.url(), os.getenv("SIMULATOR_URL", "http://127.0.0.1:9000"))  # Run it.
    print(json.dumps(report, indent=2))  # Show the result.
    sys.exit(1 if any(report["violations"].values()) else 0)  # Exit code 1 lets scripts and CI detect failure.


def merchant_command():  # Creates a merchant and shows its key once.
    merchant_id, api_key = postgres.create_merchant(os.getenv("DATABASE_URL"))  # Create it.
    print(f"merchant_id={merchant_id}\napi_key={api_key}")  # CAREFUL: the key cannot be shown again.


COMMANDS = {  # Command name and the function it runs.
    "db-start": postgres.start,  # Start local Postgres.
    "db-stop": postgres.stop,  # Stop it and free memory.
    "migrate": postgres.migrate,  # Create or update tables.
    "create-merchant": merchant_command,  # Add a merchant.
    "audit": audit_command,  # Check the money.
    "worker": recovery.main,  # Run the Recovery Worker.
    "relay": relay.main,  # Run the Outbox Relay (needs Kafka: docker compose up -d).
    "event-check": event_check_command,  # Compare Kafka with the outbox.
    "features": features.main,  # Run the Feature Worker (needs Kafka and Redis).
    "features-rebuild": features.rebuild_main,  # Replay Kafka into Redis from the start.
    "sink": parquet_sink.main,  # Run the Parquet Sink (writes bronze files).
    "lake": lake_command,  # Build silver and gold, and check data quality.
    "up": stack.up,  # Start everything (Phase 9).
    "down": stack.down,  # Stop everything and free RAM.
    "status": stack.status,  # Show what is running.
    "chaos": chaos.main,  # Run every failure drill and audit; exit 1 on any broken rule.
}


def main():  # Entry point of the `paisa` command.
    parser = argparse.ArgumentParser(prog="paisa")  # Build the argument reader.
    parser.add_argument("command", choices=COMMANDS)  # Require one known command.
    COMMANDS[parser.parse_args().command]()  # Run it.


if __name__ == "__main__":  # Lets `paisa up` start workers with `python -m paisa.cli worker`.
    main()
