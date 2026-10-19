"""Migration 001 gegen eine Wegwerf-Datenbank: up, down, Constraints greifen.

Gegen Revision 001, nicht head: seit 002 liegen weitere Tabellen darueber.
Den Vergleich Modelle gegen Schema macht test_migration_002 auf head.
"""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

STUFE_1_TABELLEN = {
    "tenants",
    "service_config",
    "opening_hours",
    "special_days",
    "capacity",
    "reservations",
    "calls",
    "callbacks",
    "outbox",
    "audit_log",
}


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_erzeugt_alle_stufe1_tabellen(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "001")
    assert _table_names(scratch_db_url) == STUFE_1_TABELLEN | {"alembic_version"}


def test_downgrade_entfernt_alles_und_upgrade_geht_erneut(scratch_db_url):
    command.downgrade(_config(scratch_db_url), "base")
    assert _table_names(scratch_db_url) <= {"alembic_version"}
    command.upgrade(_config(scratch_db_url), "001")
    assert _table_names(scratch_db_url) == STUFE_1_TABELLEN | {"alembic_version"}


@pytest.fixture
def conn(scratch_db_url):
    engine = create_engine(scratch_db_url)
    with engine.connect() as connection:
        yield connection
        connection.rollback()
    engine.dispose()


def _tenant_und_call(conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = conn.execute(
        text(
            "INSERT INTO tenants (name, timezone) VALUES ('Test', 'Europe/Berlin') RETURNING id"
        )
    ).scalar_one()
    call_id = conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, started_at, delete_after) "
            "VALUES (:t, 'ext-1', now(), current_date + 30) RETURNING id"
        ),
        {"t": tenant_id},
    ).scalar_one()
    return tenant_id, call_id


def test_doppelter_idempotency_key_scheitert_an_der_db(conn):
    tenant_id, call_id = _tenant_und_call(conn)
    insert = text(
        "INSERT INTO reservations (tenant_id, call_id, guest_name, phone, party_size, reserved_for, idempotency_key) "
        "VALUES (:t, :c, 'Müller', '+4972215551234', 4, now() + interval '1 day', 'key-1')"
    )
    conn.execute(insert, {"t": tenant_id, "c": call_id})
    with pytest.raises(IntegrityError):
        conn.execute(insert, {"t": tenant_id, "c": call_id})


def test_reservierung_ohne_call_id_scheitert(conn):
    tenant_id, _ = _tenant_und_call(conn)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO reservations (tenant_id, guest_name, phone, party_size, reserved_for, idempotency_key) "
                "VALUES (:t, 'Müller', '+4972215551234', 2, now(), 'key-2')"
            ),
            {"t": tenant_id},
        )


def test_reservierung_startet_als_draft(conn):
    tenant_id, call_id = _tenant_und_call(conn)
    status = conn.execute(
        text(
            "INSERT INTO reservations (tenant_id, call_id, guest_name, phone, party_size, reserved_for, idempotency_key) "
            "VALUES (:t, :c, 'Müller', '+4972215551234', 2, now(), 'key-3') RETURNING status"
        ),
        {"t": tenant_id, "c": call_id},
    ).scalar_one()
    assert status == "draft"


def test_ungueltiger_outbox_status_scheitert(conn):
    tenant_id, _ = _tenant_und_call(conn)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO outbox (tenant_id, event_type, payload, status) "
                "VALUES (:t, 'reservation.confirmed', '{}'::jsonb, 'irgendwas')"
            ),
            {"t": tenant_id},
        )


def test_outbox_eintrag_startet_pending_mit_null_versuchen(conn):
    tenant_id, _ = _tenant_und_call(conn)
    row = conn.execute(
        text(
            "INSERT INTO outbox (tenant_id, event_type, payload) "
            "VALUES (:t, 'reservation.confirmed', '{\"x\": 1}'::jsonb) "
            "RETURNING status, attempts, next_attempt_at IS NOT NULL"
        ),
        {"t": tenant_id},
    ).one()
    assert tuple(row) == ("pending", 0, True)
