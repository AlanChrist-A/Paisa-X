"""
WHAT: one command to start or stop the whole local stack.
      uv run paisa up       Postgres, Docker (Kafka, Redis, Prometheus, Grafana), simulator, API and workers
      uv run paisa status   what is running
      uv run paisa down     stops everything and frees RAM
WHY: a stranger should be able to run Paisa-X from the README without knowing every part.
USES: the Postgres adapter, Docker Compose and the Kafka adapter.
LOGS: each process writes JSON lines to .local/logs/<name>.log.
SAFE TO CHANGE: add a process with one entry in apps().
"""

import json  # Saves process IDs.
import os  # Environment variables and Windows paths.
import secrets  # Creates the local webhook secret.
import signal  # Stops processes on Linux and macOS.
import socket  # Checks whether a port is answering.
import subprocess  # Starts processes and Docker.
import sys  # Path to this Python.
import time  # Waiting loops.
from pathlib import Path  # File paths.

from paisa.adapters import kafka, postgres  # Local database and Kafka helpers.

LOCAL = postgres.LOCAL  # Private, git-ignored machine data.
LOGS, PIDS, SECRET = LOCAL / "logs", LOCAL / "pids.json", LOCAL / "webhook_secret"  # Logs, running processes, secret.
WAIT_S = 90  # CHANGE ME: seconds to wait for Docker, Kafka or a port before giving up.
DOCKER_DESKTOP = Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "Docker Desktop.exe"  # Windows user install.


def apps(secret):  # Process name -> (command, extra settings, port to wait for).
    py = sys.executable  # The project's Python, so every process uses the same packages.
    web = lambda target, port: [py, "-m", "uvicorn", *target, "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]  # noqa: E731  The API logs requests itself.
    return {
        "simulator": (web(["simulator.app:app"], 9000), {"SIM_WEBHOOK_URL": "http://127.0.0.1:8000/v1/webhooks/{gateway}", "SIM_WEBHOOK_SECRET": secret}, 9000),
        "api": (web(["--factory", "paisa.api.app:create_app"], 8000), {"PAISA_WEBHOOK_SECRET": secret, "PAISA_REDIS_URL": "redis://127.0.0.1:6379/0"}, 8000),
        "recovery": ([py, "-m", "paisa.cli", "worker"], {}, 8001),  # Serves /metrics on 8001.
        "relay": ([py, "-m", "paisa.cli", "relay"], {}, 8002),  # Serves /metrics on 8002.
        "features": ([py, "-m", "paisa.cli", "features"], {}, None),  # No port.
    }


def wait_for(check, what):  # Repeats a check until it passes, or fails loudly.
    for _ in range(WAIT_S):
        if check():
            return
        time.sleep(1)
    raise SystemExit(f"Timed out waiting for {what}. See .local/logs.")


def port_open(port):  # Is something listening on this local port?
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def docker_ready():  # Does the Docker engine answer?
    return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def kafka_ready():  # Can we create or see the topics?
    try:
        kafka.ensure_topics()
        return True
    except Exception:  # Not up yet.
        return False


def running():  # Saved processes that are still alive.
    saved = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    return {name: pid for name, pid in saved.items() if alive(pid)}


def alive(pid):  # Is this process still running?
    if os.name == "nt":  # Windows has no signal 0, so ask tasklist.
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)  # Signal 0 only checks.
        return True
    except OSError:
        return False


def kill(pid):  # Stops a process and its children.
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.kill(pid, signal.SIGTERM)


def stop_apps():  # Stops simulator, API and workers, but leaves Postgres and Docker running.
    for name, pid in running().items():
        kill(pid)
        print(f"stopped {name}")
    PIDS.unlink(missing_ok=True)


def up():  # `paisa up`
    if running():
        raise SystemExit("Already running. Use `uv run paisa status`, or `uv run paisa down` first.")
    LOGS.mkdir(parents=True, exist_ok=True)
    if not docker_ready():  # Start Docker Desktop on Windows if it is installed.
        if DOCKER_DESKTOP.exists():
            subprocess.Popen([DOCKER_DESKTOP])
        print("waiting for Docker...")
        wait_for(docker_ready, "Docker (start Docker Desktop)")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=postgres.ROOT, check=True)  # Kafka, Redis, Prometheus, Grafana.
    postgres.start()  # Portable Postgres (no-op if running).
    postgres.migrate()  # Tables up to date.
    print("waiting for Kafka...")
    wait_for(kafka_ready, "Kafka")
    if not SECRET.exists():  # One local secret shared by simulator and API.
        SECRET.write_text(secrets.token_hex(24))
    pids, flags = {}, subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # Own group, so Ctrl+C here does not stop them.
    for name, (command, extra, port) in apps(SECRET.read_text().strip()).items():
        log = open(LOGS / f"{name}.log", "a")  # Append, so earlier runs stay readable.
        pids[name] = subprocess.Popen(command, cwd=postgres.ROOT, env={**os.environ, **extra}, stdout=log, stderr=subprocess.STDOUT, creationflags=flags).pid
        PIDS.write_text(json.dumps(pids))  # Save as we go, so `down` can clean up a half start.
        if port:
            wait_for(lambda: port_open(port), f"{name} on port {port}")
        print(f"started {name}")
    print("\nPaisa-X is up.\n  API        http://127.0.0.1:8000/docs\n  Grafana    http://127.0.0.1:3000\n  Prometheus http://127.0.0.1:9090\n  Logs       .local/logs\n"
          "Create a merchant key with: uv run paisa create-merchant")


def status():  # `paisa status`
    alive_now = running()
    for name in apps(""):
        print(f"{name:10} {'running (pid ' + str(alive_now[name]) + ')' if name in alive_now else 'stopped'}")
    print(f"{'postgres':10} {'running' if postgres.running() else 'stopped'}")
    print(subprocess.run(["docker", "compose", "ps", "--format", "{{.Name}}\t{{.Status}}"], cwd=postgres.ROOT, capture_output=True, text=True).stdout)


def down():  # `paisa down`
    stop_apps()
    if docker_ready():
        subprocess.run(["docker", "compose", "stop"], cwd=postgres.ROOT, check=True)  # Keeps data, frees RAM.
    postgres.stop()
    print("Paisa-X is down.")
