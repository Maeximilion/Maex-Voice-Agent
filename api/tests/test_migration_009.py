"""Migration 009: where the privacy notice is, said in the greeting (T-11.5), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

FIELD = "privacy_notice_address"


def _config_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("service_config")}
    finally:
        engine.dispose()


def _tenant(conn, name: str):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def test_upgrade_leaves_an_existing_tenant_without_an_address(scratch_db_url):
    """No address is invented: `example.com` is only the repository's
    placeholder, the real one comes from the restaurant."""
    command.upgrade(_config(scratch_db_url), "008")
    engine = create_engine(scratch_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO service_config (tenant_id, team_phone) "
                    "VALUES (:t, '+4900000000')"
                ),
                {"t": _tenant(conn, "Old")},
            )
        command.upgrade(_config(scratch_db_url), "009")
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT {FIELD} FROM service_config")).one() == (
                None,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t", " \r\n "])
def test_a_blank_address_is_refused_and_a_real_one_is_kept(scratch_db_url, blank):
    """A blank one would make the greeting say "finden Sie unter ."; a line
    break or a tab is as blank as a space (ultrareview of PR #372)."""
    command.upgrade(_config(scratch_db_url), "009")
    engine = create_engine(scratch_db_url)
    insert = text(
        f"INSERT INTO service_config (tenant_id, team_phone, {FIELD}) "
        "VALUES (:t, '+4900000000', :v)"
    )
    try:
        with engine.begin() as conn:
            empty, real = _tenant(conn, "Blank"), _tenant(conn, "Real")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"t": empty, "v": blank})
        with engine.begin() as conn:
            conn.execute(insert, {"t": real, "v": "example.com/datenschutz"})
            kept = conn.execute(
                text(f"SELECT {FIELD} FROM service_config WHERE tenant_id = :t"),
                {"t": real},
            ).scalar_one()
        assert kept == "example.com/datenschutz"
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_column(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "009")
    assert FIELD in _config_columns(scratch_db_url)
    command.downgrade(_config(scratch_db_url), "008")
    columns = _config_columns(scratch_db_url)
    assert FIELD not in columns
    assert "order_approval_overdue_minutes" in columns
    command.upgrade(_config(scratch_db_url), "head")
    assert FIELD in _config_columns(scratch_db_url)
