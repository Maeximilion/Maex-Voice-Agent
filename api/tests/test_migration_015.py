"""Migration 015: holiday suggestions in special_days and the region (T-9.5), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


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


def test_upgrade_keeps_an_existing_special_day_counting(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "014")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            _day(conn, _tenant(conn, "Old"), "2026-12-24", closed=True)
        command.upgrade(_config(scratch_db_url), "015")
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT status, like_weekday, closed FROM special_days")
            ).one() == ("set", None, True)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "maybe"},
        {"like_weekday": 7},
        {"like_weekday": 6, "closed": True},
        {"like_weekday": 6, "opens_at": "12:00", "closes_at": "15:00"},
    ],
)
def test_a_row_with_two_meanings_or_unknown_values_is_refused(scratch_db_url, fields):
    command.upgrade(_config(scratch_db_url), "015")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Check")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _day(conn, tenant, "2026-11-01", **fields)
    finally:
        engine.dispose()


def test_one_suggestion_per_date_but_set_rows_are_free(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "015")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Twice")
            _day(conn, tenant, "2026-11-01", status="suggested")
            # A set row on the same date is what an answer leaves; no conflict.
            _day(conn, tenant, "2026-11-01", closed=True)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _day(conn, tenant, "2026-11-01", status="dismissed")
    finally:
        engine.dispose()


def test_an_unknown_region_is_refused(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "015")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Region")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone, holiday_region) "
                    "VALUES (:t, '+4900000000', 'XX')"
                ),
                {"t": tenant},
            )
    finally:
        engine.dispose()


def test_downgrade_drops_suggestions_and_keeps_set_days(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "015")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            tenant = _tenant(conn, "Down")
            _day(conn, tenant, "2026-12-24", closed=True)
            _day(conn, tenant, "2026-11-01", status="suggested", like_weekday=6)
            _day(conn, tenant, "2026-10-03", status="dismissed", like_weekday=6)
        command.downgrade(_config(scratch_db_url), "014")
        assert not {"status", "like_weekday"} & _columns(scratch_db_url, "special_days")
        assert "holiday_region" not in _columns(scratch_db_url, "service_config")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT date::text FROM special_days")).all() == [
                ("2026-12-24",)
            ]
        command.upgrade(_config(scratch_db_url), "head")
        assert "status" in _columns(scratch_db_url, "special_days")
    finally:
        engine.dispose()
