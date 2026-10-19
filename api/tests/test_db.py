"""Tests für api/db.py gegen eine echte Postgres (DATABASE_URL)."""

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from api import db as db_module
from api.db import SessionLocal, engine, get_db
from api.main import app


def test_get_db_liefert_arbeitsfaehige_session():
    gen = get_db()
    session = next(gen)
    assert isinstance(session, Session)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    gen.close()


def test_get_db_rollt_bei_fehler_zurueck_und_schliesst():
    gen = get_db()
    session = next(gen)
    session.execute(text("SELECT 1"))
    assert session.in_transaction()
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("Fachfehler"))
    assert not session.in_transaction()
    # Nach close() holt sich die Session bei Bedarf eine frische Verbindung.
    assert session.execute(text("SELECT 2")).scalar_one() == 2
    session.close()


def test_db_nicht_erreichbar_liefert_huelle_statt_stacktrace(monkeypatch):
    dead_engine = create_engine(
        "postgresql+psycopg://maex:maex@127.0.0.1:1/maex_agent",
        connect_args={"connect_timeout": 1},
    )
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=dead_engine))

    @app.get("/_test/db-down")
    def db_down(session: Session = Depends(get_db)) -> dict:
        session.execute(text("SELECT 1"))
        return {"unreachable": True}

    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/_test/db-down")
        body = r.json()
        assert r.status_code == 200
        assert body["ok"] is False
        assert body["error"]["code"] == "service_unavailable"
        assert "Traceback" not in r.text
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/_test/db-down"
        ]


def test_engine_pingt_verbindungen():
    assert engine.pool._pre_ping is True
    assert SessionLocal.kw["bind"] is engine


def test_engine_faehrt_read_committed():
    """Voraussetzung des Sperr-Protokolls, nicht dem Serverdefault ueberlassen.

    Unter REPEATABLE READ sieht eine Transaktion nach dem Warten auf eine
    Advisory-Sperre den fremden Commit nicht und legt denselben Vorgang erneut an.
    """
    with engine.connect() as conn:
        stufe = conn.execute(text("SHOW transaction_isolation")).scalar_one()
    assert stufe == "read committed", f"erwartet read committed, bekam {stufe}"
