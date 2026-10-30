"""Migration 017: one counting row per date in special_days (#441), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

KEY = "uq_special_days_set"


def _tenant(conn, name: str):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def _day(conn, tenant, day: str, **fields) -> None:
    names = ", ".join(["tenant_id", "date", *fields])
    values = ", ".join([":t", ":d", *(f":{k}" for k in fields)])
    conn.execute(
        text(f"INSERT INTO special_days ({names}) VALUES ({values})"),
        {"t": tenant, "d": day, **fields},
    )


def _indexes(engine) -> set[str]:
    return {i["name"] for i in inspect(engine).get_indexes("special_days")}


def test_one_set_row_per_tenant_and_date(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "017")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant, other = _tenant(conn, "One"), _tenant(conn, "Other")
            _day(conn, tenant, "2026-11-01", closed=True)
            # What stays free: another tenant, another date, a suggestion beside it.
            _day(conn, other, "2026-11-01", closed=True)
            _day(conn, tenant, "2026-11-02", closed=True)
            _day(conn, tenant, "2026-11-01", status="suggested", like_weekday=6)
        with pytest.raises(IntegrityError, match=KEY), engine.begin() as conn:
            _day(conn, tenant, "2026-11-01", like_weekday=6)
    finally:
        engine.dispose()


def test_upgrade_stops_on_two_set_rows_and_names_them(scratch_db_url):
    """On a database with restaurant data a duplicate is reported, whole, and
    nothing changes; with one row left the upgrade goes through."""
    command.upgrade(_config(scratch_db_url), "016")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Twice")
            _day(conn, tenant, "2026-12-24", closed=True, note="Heiligabend")
            _day(conn, tenant, "2026-12-24", like_weekday=6, note="drill")
            _day(conn, tenant, "2026-12-31", closed=True)
        with pytest.raises(RuntimeError, match=r"2026-12-24.*2 rows") as stopped:
            command.upgrade(_config(scratch_db_url), "017")
        assert "2026-12-31" not in str(stopped.value)
        assert "backup.sh" in str(stopped.value)
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).all() == [("016",)]
        assert KEY not in _indexes(engine)

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM special_days WHERE note = 'drill'"))
        command.upgrade(_config(scratch_db_url), "017")
        assert KEY in _indexes(engine)
    finally:
        engine.dispose()


def test_downgrade_drops_the_key(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "017")
    command.downgrade(_config(scratch_db_url), "016")
    engine = create_engine(scratch_db_url)
    try:
        assert KEY not in _indexes(engine)
        with engine.begin() as conn:
            tenant = _tenant(conn, "Down")
            _day(conn, tenant, "2026-11-01", closed=True)
            _day(conn, tenant, "2026-11-01", like_weekday=6)
    finally:
        engine.dispose()
