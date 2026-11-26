"""
WHAT: the Paisa-X HTTP API.
WHY: merchants need one safe way to create payments and read their status.
USES: services/payments.py for payment logic and Postgres for storage.
USED BY: run with `uv run uvicorn --factory paisa.api.app:create_app --host 127.0.0.1 --port 8000`.
SETTINGS: DATABASE_URL and SIMULATOR_URL.
SAFE TO CHANGE: add routes here, but keep payment rules in services and core.
CAREFUL: routes use plain `def`. FastAPI runs them in worker threads, which suits these blocking
         database and gateway calls and avoids an async Postgres limitation on Windows.
"""

import os  # Reads settings from environment variables.
import time  # Provides the current time for webhook checks.
import uuid  # Validates payment IDs in URLs.
from contextlib import asynccontextmanager  # Runs setup and cleanup around the app's life.
from typing import Literal  # Restricts a field to fixed values.

import httpx  # Calls gateways.
from fastapi import Depends, FastAPI, Header, HTTPException, Request  # Web framework pieces.
from fastapi.concurrency import run_in_threadpool  # Runs blocking database work outside the event loop.
from fastapi.responses import JSONResponse, Response  # Lets us choose the HTTP status code and content type.
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # The /metrics page.
from psycopg.rows import dict_row  # Returns database rows as dictionaries.
from psycopg_pool import ConnectionPool  # Reuses a small set of database connections.
from pydantic import BaseModel, ConfigDict, Field, StrictInt  # Validates incoming data.

from paisa import observability as obs  # Logs with trace IDs, and metrics.
from paisa.adapters import postgres, redis_store  # Local database and Redis helpers.
from paisa.core.money import MAX_PAISE  # Largest allowed payment.
from paisa.services import health_snapshot, payments, webhooks  # Health sharing, payment flow and webhook intake.
from paisa.services.settlement import utc_now  # Real clock.

GATEWAY_TIMEOUT_SECONDS = 3  # CHANGE ME: longer tolerates slow gateways, but makes customers wait.
POOL_SIZE = 10  # CHANGE ME: maximum database connections used by this API process.


class PaymentIn(BaseModel):  # The shape of a valid payment request.
    model_config = ConfigDict(extra="forbid")  # Reject unknown fields instead of ignoring mistakes.
    amount_paise: StrictInt = Field(gt=0, le=MAX_PAISE)  # CAREFUL: strict whole paise, never a decimal or text.
    method: Literal["UPI", "CARD", "NETBANKING"]  # Supported payment methods.
    bank: str = Field(min_length=1, max_length=32)  # Customer's fictional bank.
    network: str | None = Field(default=None, max_length=32)  # Card network, for cards.
    instrument_token: str = Field(pattern=r"^tok_[A-Za-z0-9_]{4,64}$")  # CAREFUL: fake token only, so card numbers are refused.


def create_app(database_url=None, gateway_http=None, webhook_secret=None):  # Builds the app. Tests can inject a fake gateway.
    obs.setup("api")  # JSON logs with trace IDs.
    address = database_url or os.getenv("DATABASE_URL") or postgres.url()  # Choose the database.
    secret = webhook_secret or os.getenv("PAISA_WEBHOOK_SECRET")  # CAREFUL: comes from the environment, never from code.
    pool = ConnectionPool(address, min_size=1, max_size=POOL_SIZE, open=False, kwargs={"row_factory": dict_row})  # Prepare the pool.
    http = gateway_http or httpx.Client(base_url=os.getenv("SIMULATOR_URL", "http://127.0.0.1:9000"), timeout=GATEWAY_TIMEOUT_SECONDS)  # Gateway client.

    @asynccontextmanager
    async def lifespan(_):  # Opens and closes shared resources.
        pool.open(wait=True)  # Connect before accepting requests.
        # Optional: share gateway health through Redis. Off unless PAISA_REDIS_URL is set.
        stop = health_snapshot.start(payments.router, redis_store.client(), utc_now) if os.getenv("PAISA_REDIS_URL") else None
        yield  # The app serves requests here.
        stop and stop.set()  # Stop the background refresh.
        pool.close(), http.close()  # Release connections on shutdown.

    app = FastAPI(title="Paisa-X API", lifespan=lifespan)  # Create the web app.
    obs.BREAKERS_OPEN.set_function(lambda: sum(not b.closed for b in payments.router.breakers.values()))  # Read live at scrape time.

    @app.middleware("http")
    async def observe(request: Request, call_next):  # Gives every request a trace ID, a log line and metrics.
        tid = request.headers.get("X-Trace-Id") or uuid.uuid4().hex  # Reuse the caller's ID, or make one.
        token, started = obs.trace_id.set(tid), time.perf_counter()  # Logs in this request now carry the ID.
        try:
            response = await call_next(request)  # Run the route.
        finally:
            obs.trace_id.reset(token)  # Never leak the ID into the next request.
        route = getattr(request.scope.get("route"), "path", "unmatched")  # "/v1/payments/{payment_id}", never the raw ID.
        seconds = time.perf_counter() - started
        obs.HTTP_REQUESTS.labels(route, response.status_code).inc()  # Count it.
        obs.HTTP_SECONDS.labels(route).observe(seconds)  # Time it.
        if route != "/metrics":  # Scrapes every few seconds would flood the log.
            obs.log("http.request", trace_id=tid, method=request.method, route=route, status=response.status_code, ms=round(seconds * 1000, 1))
        response.headers["X-Trace-Id"] = tid  # The caller can quote it in a support ticket.
        return response

    @app.get("/metrics")
    def metrics():  # Prometheus reads this page.
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    def current_merchant(authorization: str | None = Header(default=None)):  # Identifies the caller.
        scheme, _, api_key = (authorization or "").partition(" ")  # Expected form: "Bearer <key>".
        with pool.connection() as conn:  # Look up the key's hash.
            row = conn.execute("SELECT id FROM merchants WHERE api_key_hash = %s", (postgres.hash_key(api_key),)).fetchone()  # Find it.
        if scheme != "Bearer" or not row:  # Missing or wrong key.
            raise HTTPException(401, "Invalid API key")  # Refuse.
        return row["id"]  # The authenticated merchant.

    @app.get("/healthz")
    def healthz():  # A simple "is this process alive?" check.
        return {"status": "ok"}  # Answer.

    @app.post("/v1/payments")
    def create_payment(body: PaymentIn, idempotency_key: str = Header(min_length=1, max_length=255), merchant_id=Depends(current_merchant)):  # Create and run a payment.
        code, response, headers = payments.create(pool, http, merchant_id, idempotency_key, body.model_dump())  # Run the flow.
        if "state" in response and not headers:  # A new answer, not a replay or an error.
            obs.PAYMENTS.labels(response["state"]).inc()
            obs.log("payment.answered", payment_id=response["id"], state=response["state"], attempts=len(response["attempts"]))
        return JSONResponse(response, code, headers)  # Reply.

    @app.post("/v1/webhooks/{gateway}")
    async def receive_webhook(gateway: str, request: Request):  # Receives a signed result from a gateway.
        raw = await request.body()  # CAREFUL: the signature covers these exact bytes.
        code = await run_in_threadpool(webhooks.receive, pool, gateway, raw, request.headers.get("X-Sim-Timestamp", ""), request.headers.get("X-Sim-Signature", ""), secret, time.time())  # Verify and store.
        return JSONResponse({"accepted": code == 200}, code)  # Reply quickly.

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: uuid.UUID, merchant_id=Depends(current_merchant)):  # Read one payment.
        with pool.connection() as conn:  # Open a connection.
            # CAREFUL: filtering by merchant stops one merchant from reading another's payment.
            if not conn.execute("SELECT 1 FROM payments WHERE id = %s AND merchant_id = %s", (payment_id, merchant_id)).fetchone():
                raise HTTPException(404, "Payment not found")  # The same answer whether it is missing or private.
            return payments.view(conn, payment_id)  # Return the public view.

    return app  # Hand back the finished app.
