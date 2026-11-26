"""
WHAT: tests for Phase 9 observability: JSON logs, trace IDs and the /metrics page.
WHY: logs nobody can search, or a trace ID that gets lost, are useless during an incident.
"""

import json  # Reads log lines.
import logging  # Builds a log record.

from fastapi.testclient import TestClient  # Calls the API in-process.

from paisa import observability as obs  # Code under test.
from paisa.api.app import create_app  # The API.


def test_log_line_is_json_with_trace_id():  # One line, machine-readable, with the request's trace ID.
    token = obs.trace_id.set("abc123")
    record = logging.LogRecord("paisa", logging.INFO, __file__, 1, "payment.answered", None, None)
    record.fields = {"payment_id": "p1"}
    line = json.loads(obs.JsonFormatter().format(record))
    obs.trace_id.reset(token)
    assert line["trace_id"] == "abc123" and line["event"] == "payment.answered" and line["payment_id"] == "p1"


def test_trace_id_is_returned_and_metrics_are_served(database_url):  # The caller's ID comes back; Prometheus can scrape.
    with TestClient(create_app(database_url, webhook_secret="x")) as client:
        assert client.get("/healthz", headers={"X-Trace-Id": "t-1"}).headers["X-Trace-Id"] == "t-1"  # Reused.
        assert len(client.get("/healthz").headers["X-Trace-Id"]) == 32  # Made up when missing.
        page = client.get("/metrics").text
    assert 'paisa_http_requests_total{route="/healthz",status="200"}' in page and "paisa_breakers_open" in page
