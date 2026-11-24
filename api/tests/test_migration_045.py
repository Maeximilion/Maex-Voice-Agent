"""Migration 045: the callback reason `order_open` (#977), up and down. The
cards of an order read back and never answered move to it and back."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

UNANSWERED = "Anruf endete vor dem Ja zur vorgelesenen Bestellung."


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "044")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _card(engine, reason: str, summary: str) -> uuid.UUID:
    tenant, call, card = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name, timezone) VALUES (:id, 'T', 'UTC')"),
            {"id": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO calls (id, tenant_id, external_session_id, started_at,"
                " delete_after) VALUES (:id, :t, :s, :at, :day)"
            ),
            {
                "id": call,
                "t": tenant,
                "s": call.hex,
                "at": datetime.now(UTC),
                "day": datetime.now(UTC).date(),
            },
        )
        connection.execute(
            text(
                "INSERT INTO callbacks (id, tenant_id, call_id, phone, reason, summary)"
                " VALUES (:id, :t, :c, '+4917612345678', :r, :s)"
            ),
            {"id": card, "t": tenant, "c": call, "r": reason, "s": summary},
        )
    return card


def _reason(engine, card: uuid.UUID) -> str:
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT reason FROM callbacks WHERE id = :id"), {"id": card}
        )


def test_up_moves_the_unanswered_order_cards_and_allows_the_reason(
    engine, scratch_db_url
):
    with pytest.raises(IntegrityError):
        _card(engine, "order_open", UNANSWERED)
    open_order = _card(engine, "not_understood", f"{UNANSWERED} Offene Bestellung")
    marked = _card(engine, "not_understood", f"Allergie erwähnt. {UNANSWERED}")
    other = _card(engine, "not_understood", "Anliegen konnte nicht geklärt werden.")

    command.upgrade(_config(scratch_db_url), "045")

    assert _reason(engine, open_order) == "order_open"
    # A kitchen marker may lead the card (#940): the sentence anywhere counts.
    assert _reason(engine, marked) == "order_open"
    assert _reason(engine, other) == "not_understood"
    assert _reason(engine, _card(engine, "order_open", UNANSWERED)) == "order_open"
    with pytest.raises(IntegrityError):
        _card(engine, "unknown_reason", "x")


def test_down_gives_the_cards_back_as_not_understood(engine, scratch_db_url):
    command.upgrade(_config(scratch_db_url), "045")
    card = _card(engine, "order_open", UNANSWERED)

    command.downgrade(_config(scratch_db_url), "044")

    assert _reason(engine, card) == "not_understood"
    with pytest.raises(IntegrityError):
        _card(engine, "order_open", UNANSWERED)


def test_down_and_up_again(engine, scratch_db_url):
    card = _card(engine, "not_understood", UNANSWERED)
    command.upgrade(_config(scratch_db_url), "045")
    command.downgrade(_config(scratch_db_url), "044")
    command.upgrade(_config(scratch_db_url), "045")
    assert _reason(engine, card) == "order_open"
