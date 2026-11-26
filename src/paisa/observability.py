"""
WHAT: structured JSON logs with trace IDs, and the Prometheus metrics every process shares.
WHY: when something breaks at 2 a.m., one trace ID should find every log line of that request,
     and dashboards should show trouble before customers report it.
USES: Python's logging and prometheus-client. No other Paisa-X layer.
USED BY: the API, the payment flow, the Recovery Worker and the Outbox Relay.
SAFE TO CHANGE: add a metric here, then update it where the event happens.
CAREFUL: never log secrets, API keys or instrument tokens. Log IDs and states only.
"""

import contextvars  # Holds the current trace ID, separately for every request and thread.
import json  # Writes each log line as JSON.
import logging  # Python's standard logging.
import sys  # Logs go to standard error.
import time  # Log timestamps.

from prometheus_client import Counter, Gauge, Histogram, start_http_server  # Metric types.

trace_id = contextvars.ContextVar("trace_id", default="-")  # "-" means "not inside a request".
SERVICE = {"name": "paisa"}  # Which process is logging, set by setup().

# --- Metrics. Names end in _total for counters, as Prometheus expects. ---
HTTP_REQUESTS = Counter("paisa_http_requests_total", "API requests", ["route", "status"])  # Traffic and errors.
HTTP_SECONDS = Histogram("paisa_http_request_seconds", "API request time", ["route"], buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8))  # Latency.
PAYMENTS = Counter("paisa_payments_total", "Payment answers by state", ["state"])  # Outcomes the merchant saw.
ATTEMPTS = Counter("paisa_gateway_attempts_total", "Gateway calls by result", ["gateway", "result"])  # Gateway health.
BREAKERS_OPEN = Gauge("paisa_breakers_open", "Open circuit breakers in this API process")  # Gateways being avoided.
RECOVERY_ROUNDS = Counter("paisa_recovery_rounds_total", "Recovery Worker rounds", ["result"])  # ok or error.
EVENTS_RELAYED = Counter("paisa_events_relayed_total", "Outbox events sent to Kafka")  # Relay progress.
OUTBOX_WAITING = Gauge("paisa_outbox_waiting", "Outbox events not yet in Kafka")  # Backlog.


class JsonFormatter(logging.Formatter):  # Turns a log record into one JSON line.
    def format(self, record):
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",  # UTC time.
                "level": record.levelname, "service": SERVICE["name"], "event": record.getMessage(), "trace_id": trace_id.get()}
        line.update(getattr(record, "fields", {}))  # Extra details, such as payment_id.
        return json.dumps(line, default=str)  # default=str handles UUIDs and dates.


def setup(service, metrics_port=None):  # Call once at process start.
    SERVICE["name"] = service
    handler = logging.StreamHandler(sys.stderr)  # One line per event.
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)  # Replace any earlier setup.
    logging.getLogger("httpx").setLevel(logging.WARNING)  # One line per gateway call is noise; our own events say more.
    if metrics_port:  # Workers have no web app, so they serve /metrics on their own port.
        try:
            start_http_server(metrics_port, addr="127.0.0.1")  # CAREFUL: this laptop only.
        except OSError:  # Port taken (a second copy, or a drill). Work matters more than metrics.
            log("metrics.port_busy", logging.WARNING, port=metrics_port)


def log(event, level=logging.INFO, **fields):  # log("payment.created", payment_id=...)
    logging.getLogger("paisa").log(level, event, extra={"fields": fields})
