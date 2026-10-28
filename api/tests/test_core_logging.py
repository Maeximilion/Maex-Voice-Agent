"""JSON-Logging: jede Zeile trägt request_id und call_id."""

import io
import json
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from api.core.logging import (
    JsonFormatter,
    bind_call_id,
    call_id_var,
    error_text,
    request_id_var,
)
from api.jobs.retention import purge
from api.main import app, unhandled_exception_handler

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


class _FakeDiag:
    constraint_name = "orders_phone_check"


class _FakePgError(Exception):
    diag = _FakeDiag()


ROW_VALUES = ("Erika Muster", "+4972215551234", "Nussallergie", "Failing row")


def _db_error() -> IntegrityError:
    orig = _FakePgError(
        'new row for relation "orders" violates check constraint '
        '"orders_phone_check"\nDETAIL:  Failing row contains '
        "(Erika Muster, +4972215551234, Nussallergie)."
    )
    try:
        raise IntegrityError("INSERT INTO orders ...", None, orig)
    except IntegrityError as exc:
        return exc


def test_a_database_error_reaches_the_log_without_its_row_values():
    """Repo audit 27.10.2026, finding 11: Postgres puts the failing row into
    the message, and hide_parameters does not reach it."""
    handler, stream = _capture("api")
    try:
        request = Request({"type": "http", "headers": [], "path": "/v1/tools/x"})
        unhandled_exception_handler(request, _db_error())
    finally:
        logging.getLogger("api").removeHandler(handler)
    text = stream.getvalue()
    assert not [value for value in ROW_VALUES if value in text]
    line = json.loads(text)
    assert line["exc"] == "IntegrityError (_FakePgError) on orders_phone_check"


def test_an_error_raised_from_a_database_error_hides_it_too():
    """Starlette re-raises after the handler and uvicorn logs it once more;
    an error that wraps a database error carries its message in the chain."""
    try:
        try:
            raise _db_error()
        except IntegrityError as inner:
            raise RuntimeError("write failed") from inner
    except RuntimeError as exc:
        outer = exc
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "Exception", (), None
    )
    record.exc_info = (type(outer), outer, outer.__traceback__)
    text = JsonFormatter().format(record)
    assert not [value for value in ROW_VALUES if value in text]
    line = json.loads(text)
    assert line["exc"] == (
        "RuntimeError from IntegrityError (_FakePgError) on orders_phone_check"
    )


def test_any_other_error_keeps_its_traceback_and_text():
    try:
        raise ValueError("no database involved")
    except ValueError as exc:
        error = exc
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", (), None)
    record.exc_info = (type(error), error, error.__traceback__)
    line = json.loads(JsonFormatter().format(record))
    assert "Traceback" in line["exc"] and "no database involved" in line["exc"]
    assert error_text(error) == "no database involved"


def test_the_error_field_of_the_cold_path_hides_row_values(caplog):
    """The retention job, the watchdog and the dispatcher log `error=` instead
    of a traceback; a database error there names the same row."""
    caplog.set_level(logging.ERROR, logger="api.jobs.retention")
    session = MagicMock()
    session.begin_nested.side_effect = _db_error()

    purge(session, datetime(2026, 10, 27, tzinfo=UTC))

    errors = [r.extra["error"] for r in caplog.records if hasattr(r, "extra")]
    assert errors and not [e for e in errors for v in ROW_VALUES if v in e]
    assert "IntegrityError (_FakePgError) on orders_phone_check" in errors
