"""JSON-Logging: jede Zeile trägt request_id und call_id."""

import io
import json
import logging

from fastapi.testclient import TestClient

from api.core.logging import JsonFormatter, bind_call_id, call_id_var, request_id_var
from api.main import app

client = TestClient(app)


def _capture(logger_name: str) -> tuple[logging.Handler, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger(logger_name).addHandler(handler)
    return handler, stream


def test_formatter_schreibt_json_mit_kontext():
    request_id_var.set("req-1")
    bind_call_id("call-1")
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "hallo %s", ("welt",), None
    )
    record.extra = {"duration_ms": 12.5}
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "hallo welt"
    assert line["request_id"] == "req-1"
    assert line["call_id"] == "call-1"
    assert line["duration_ms"] == 12.5
    assert line["level"] == "INFO"
    request_id_var.set(None)
    call_id_var.set(None)


def test_request_id_aus_header_wird_uebernommen_und_geloggt():
    handler, stream = _capture("api.request")
    try:
        r = client.get("/health", headers={"X-Request-ID": "abc-123"})
        assert r.headers["X-Request-ID"] == "abc-123"
        line = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert line["request_id"] == "abc-123"
        assert line["path"] == "/health"
        assert line["status"] == 200
        assert line["duration_ms"] >= 0
    finally:
        logging.getLogger("api.request").removeHandler(handler)


def test_request_id_wird_erzeugt_wenn_header_fehlt():
    r = client.get("/health")
    assert len(r.headers["X-Request-ID"]) == 32


def test_kontext_bleibt_nicht_am_naechsten_request_kleben():
    handler, stream = _capture("api.request")
    try:
        client.get("/health", headers={"X-Request-ID": "erster"})
        client.get("/health")
        lines = [json.loads(x) for x in stream.getvalue().strip().splitlines()]
        assert lines[-2]["request_id"] == "erster"
        assert lines[-1]["request_id"] != "erster"
        assert lines[-1]["call_id"] is None
    finally:
        logging.getLogger("api.request").removeHandler(handler)
