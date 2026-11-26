"""
WHAT: the thin web layer that exposes the Gateway Simulator over HTTP.
WHY: Paisa-X must talk to gateways over a network, so it cannot peek at the simulator's memory.
USES: simulator/engine.py for all payment logic.
USED BY: run with `uv run uvicorn simulator.app:app --host 127.0.0.1 --port 9000`.
SETTINGS: SIM_SEED, and optionally SIM_WEBHOOK_URL with SIM_WEBHOOK_SECRET.
SAFE TO CHANGE: routes and HTTP codes. Keep payment rules in engine.py.
CAREFUL: /sim/admin endpoints reveal the truth and inject faults. Never expose them publicly.
"""

import asyncio  # Runs background delivery and simulated delays.
import hashlib  # Provides SHA-256 for webhook signatures.
import hmac  # HMAC = a signature proving who sent a message and that it was not changed.
import json  # Converts webhook data into bytes.
import os  # Reads settings from environment variables.
import time  # Provides the real clock for live runs.
from contextlib import asynccontextmanager  # Runs setup and cleanup around the app's life.
from typing import Literal  # Restricts a field to fixed values.

import httpx  # Sends webhooks over HTTP.
from fastapi import FastAPI, HTTPException  # The web framework and its error type.
from fastapi.responses import JSONResponse  # Lets us choose the HTTP status code.
from pydantic import BaseModel, Field  # Validates incoming request data.

from simulator.engine import FAULT_KINDS, Fault, GatewaySim  # The pure simulation logic.

STATUS_CODES = {"TIMEOUT": 504, "FAILED": 503, "UNSUPPORTED": 422}  # Other results return HTTP 200.


class PayIn(BaseModel):  # The shape of a valid payment request.
    payment_id: str = Field(min_length=1)  # The payment's unique ID.
    amount_paise: int = Field(gt=0)  # CAREFUL: whole paise only, and above zero.
    method: Literal["UPI", "CARD", "NETBANKING"]  # Supported payment methods.
    bank: str  # The customer's fictional bank.
    network: str | None = None  # Card network, for cards only.


class FaultIn(BaseModel):  # The shape of a live fault request.
    kind: str  # Fault type.
    gateway: str  # Affected gateway.
    duration_ms: int = Field(gt=0)  # How long it lasts from now.
    value: float = 1.0  # Fault strength.
    method: str | None = None  # Optional method filter.
    bank: str | None = None  # Optional bank filter.


def sign(secret, timestamp, body):  # Creates a webhook signature.
    # The timestamp is signed too, so an attacker cannot resend an old webhook as new.
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def deliver_once(sim, now, client, url, secret):  # Sends every webhook that is due.
    for event in sim.due_webhooks(now):  # Go through ready webhooks.
        body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()  # Exact bytes to sign.
        timestamp = str(int(time.time()))  # Current time in seconds.
        headers = {"X-Sim-Timestamp": timestamp, "X-Sim-Signature": sign(secret, timestamp, body), "Content-Type": "application/json"}
        try:  # A receiver being down must not crash the simulator.
            await client.post(url.replace("{gateway}", event["gateway"]), content=body, headers=headers)  # Fill in the gateway name.
        except httpx.HTTPError:  # The network call failed.
            pass  # Treat it as a lost webhook, so Paisa-X must recover through status polling.


def create_app(seed=None, clock=None, use_latency=True):  # Builds the app. Tests can inject a fake clock.
    sim = GatewaySim(int(os.getenv("SIM_SEED", "42")) if seed is None else seed)  # One simulated world.
    started = time.monotonic()  # Monotonic clock: unaffected by system time changes.
    now = clock or (lambda: int((time.monotonic() - started) * 1000))  # Milliseconds since startup.
    url, secret = os.getenv("SIM_WEBHOOK_URL"), os.getenv("SIM_WEBHOOK_SECRET")  # Optional webhook target.
    if url and not secret:  # CAREFUL: never send unsigned webhooks.
        raise RuntimeError("SIM_WEBHOOK_SECRET is required when SIM_WEBHOOK_URL is set.")  # Stop at startup.

    async def deliver_forever():  # Background loop for webhooks.
        async with httpx.AsyncClient(timeout=2) as client:  # Reuse one HTTP connection pool.
            while True:  # Runs until the app stops.
                await deliver_once(sim, now(), client, url, secret)  # Send what is due.
                await asyncio.sleep(0.05)  # CHANGE ME: check every 50 ms.

    @asynccontextmanager
    async def lifespan(_):  # Starts and stops the background loop.
        task = asyncio.create_task(deliver_forever()) if url else None  # Run only if a target is set.
        yield  # The app serves requests here.
        if task:  # On shutdown.
            task.cancel()  # Stop the loop.

    app = FastAPI(title="Paisa-X Gateway Simulator", lifespan=lifespan)  # Create the web app.
    app.state.sim = sim  # Lets in-process test harnesses reach the engine. HTTP callers cannot use this.

    def known(gateway):  # Rejects unknown gateway names.
        if gateway not in sim.catalog["gateways"]:  # Check the catalog.
            raise HTTPException(404, "Unknown gateway")  # Return HTTP 404.

    # CAREFUL: routes are `async def` on one event loop, so engine calls never run at the same time.
    @app.post("/sim/{gateway}/pay")
    async def pay(gateway: str, attempt_id: str, body: PayIn):  # Start a payment.
        known(gateway)  # Validate the gateway.
        reply = sim.pay(gateway, attempt_id, body.model_dump(), now())  # Run the engine.
        if use_latency:  # Live runs behave like a real network.
            await asyncio.sleep(reply["latency_ms"] / 1000)  # Wait without blocking other requests.
        return JSONResponse(reply, STATUS_CODES.get(reply["result"], 200))  # Reply with the right code.

    @app.get("/sim/{gateway}/status/{attempt_id}")
    async def status(gateway: str, attempt_id: str):  # Check an attempt.
        known(gateway)  # Validate the gateway.
        return sim.status(gateway, attempt_id, now())  # Return its state.

    @app.post("/sim/{gateway}/void/{attempt_id}")
    async def void(gateway: str, attempt_id: str):  # Block an attempt forever.
        known(gateway)  # Validate the gateway.
        return sim.void(gateway, attempt_id, now())  # Return the result.

    @app.post("/sim/{gateway}/reverse/{attempt_id}")
    async def reverse(gateway: str, attempt_id: str):  # Return the money.
        known(gateway)  # Validate the gateway.
        return sim.reverse(gateway, attempt_id, now())  # Return the result.

    @app.post("/sim/admin/faults")
    async def add_fault(body: FaultIn):  # Inject a live fault, used by tests and the demo.
        known(body.gateway)  # Validate the gateway.
        if body.kind not in FAULT_KINDS:  # Validate the fault type.
            raise HTTPException(422, "Unknown fault kind")  # Reject it.
        start = now()  # The fault starts immediately.
        sim.faults.append(Fault(body.kind, body.gateway, start, start + body.duration_ms, body.value, body.method, body.bank))  # Activate it.
        return {"active_from_ms": start, "active_until_ms": start + body.duration_ms}  # Confirm the window.

    @app.get("/sim/admin/truth")
    async def truth():  # The true money ledger, for the audit only.
        sim.advance(now())  # Bring the world up to date.
        return sim.truth()  # Return it.

    return app  # Hand back the finished app.


app = create_app()  # The object uvicorn runs.
