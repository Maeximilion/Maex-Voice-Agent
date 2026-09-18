"""Migration 002 gegen eine Wegwerf-Datenbank: up, down, Modelle ohne Diff, Constraints greifen (T-4.1)."""

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.models import Base
from api.tests.conftest import alembic_config as _config
from api.tests.test_migration_001 import STUFE_1_TABELLEN

STUFE_2_TABELLEN = {
    "menu_items",
    "item_options",
    "item_allergens",
    "item_aliases",
    "orders",
    "order_items",
}


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_erzeugt_stufe2_tabellen(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "head")
    assert _table_names(scratch_db_url) == (
        STUFE_1_TABELLEN | STUFE_2_TABELLEN | {"alembic_version"}
    )


def test_modelle_und_migration_beschreiben_dasselbe_schema(scratch_db_url):
    engine = create_engine(scratch_db_url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"Modelle und Schema weichen ab: {diff}"


def test_downgrade_auf_001_laesst_stufe1_samt_daten_stehen(scratch_db_url):
    """Zurueck auf 001 nimmt nur Stufe 2 weg; Reservierungen und Anrufe bleiben."""
    engine = create_engine(scratch_db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (name) VALUES ('Bleibt')"))
    command.downgrade(_config(scratch_db_url), "001")
    try:
        assert _table_names(scratch_db_url) == STUFE_1_TABELLEN | {"alembic_version"}
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM tenants WHERE name = 'Bleibt'")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
    command.upgrade(_config(scratch_db_url), "head")
    assert _table_names(scratch_db_url) >= STUFE_2_TABELLEN


@pytest.fixture
def conn(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    with engine.connect() as connection:
        yield connection
        connection.rollback()
    engine.dispose()


def _tenant(conn, name: str = "Test") -> uuid.UUID:
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def _call(conn, tenant_id) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, started_at, delete_after) "
            "VALUES (:t, :e, now(), current_date + 30) RETURNING id"
        ),
        {"t": tenant_id, "e": uuid.uuid4().hex},
    ).scalar_one()


def _item(conn, tenant_id, number="23", name="Frühlingsrollen (4 Stück)", price=690):
    return conn.execute(
        text(
            "INSERT INTO menu_items (tenant_id, number, name, category, price_cents) "
            "VALUES (:t, :n, :name, 'Vorspeisen', :p) RETURNING id"
        ),
        {"t": tenant_id, "n": number, "name": name, "p": price},
    ).scalar_one()


def _order(conn, tenant_id, call_id, *, items=1380, fee=0, total=1380, key=None):
    return conn.execute(
        text(
            "INSERT INTO orders (tenant_id, call_id, type, phone, customer_name, "
            "items_total_cents, delivery_fee_cents, total_cents, idempotency_key) "
            "VALUES (:t, :c, 'pickup', '+4972215551234', 'Müller', :i, :f, :tot, :k) "
            "RETURNING id"
        ),
        {
            "t": tenant_id,
            "c": call_id,
            "i": items,
            "f": fee,
            "tot": total,
            "k": key or uuid.uuid4().hex,
        },
    ).scalar_one()


def test_kartennummer_eindeutig_je_mandant(conn):
    a, b = _tenant(conn, "A"), _tenant(conn, "B")
    _item(conn, a, "23")
    # Anderer Mandant, gleiche Nummer: erlaubt.
    _item(conn, b, "23")
    with pytest.raises(IntegrityError):
        _item(conn, a, "23", name="Doppelt")


def test_kartennummer_als_text_mit_buchstabe(conn):
    t = _tenant(conn)
    _item(conn, t, "23")
    _item(conn, t, "23a")


def test_negativer_preis_scheitert(conn):
    with pytest.raises(IntegrityError):
        _item(conn, _tenant(conn), price=-1)


def test_negative_optionsdifferenz_ist_erlaubt(conn):
    item = _item(conn, _tenant(conn))
    conn.execute(
        text(
            "INSERT INTO item_options (menu_item_id, group_name, option_name, price_delta_cents) "
            "VALUES (:i, 'Größe', 'klein', -100)"
        ),
        {"i": item},
    )


def test_neues_gericht_ist_aktiv_und_nicht_ausverkauft(conn):
    item = _item(conn, _tenant(conn))
    row = conn.execute(
        text("SELECT active, sold_out_until FROM menu_items WHERE id = :i"),
        {"i": item},
    ).one()
    assert tuple(row) == (True, None)


def test_allergen_ohne_pruefer_scheitert(conn):
    """Ein Allergen-Wert ohne Namen dahinter ist keine Auskunft."""
    item = _item(conn, _tenant(conn))
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO item_allergens (menu_item_id, allergen_code, confirmed_at) "
                "VALUES (:i, 'A', now())"
            ),
            {"i": item},
        )


def test_alias_quelle_und_eindeutigkeit(conn):
    t = _tenant(conn)
    rollen, suppe = _item(conn, t, "23"), _item(conn, t, "12", name="Wan-Tan-Suppe")
    insert = text(
        "INSERT INTO item_aliases (menu_item_id, alias, source) VALUES (:i, :a, :s)"
    )
    conn.execute(insert, {"i": rollen, "a": "die knusprigen", "s": "import"})
    # Derselbe Alias an einem zweiten Gericht: erlaubt, der Importer warnt.
    conn.execute(insert, {"i": suppe, "a": "die knusprigen", "s": "import"})
    with pytest.raises(IntegrityError):
        conn.execute(insert, {"i": rollen, "a": "die knusprigen", "s": "call"})


def test_alias_mit_unbekannter_quelle_scheitert(conn):
    item = _item(conn, _tenant(conn))
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO item_aliases (menu_item_id, alias, source) "
                "VALUES (:i, 'x', 'geraten')"
            ),
            {"i": item},
        )


def test_trigram_findet_trotz_tippfehler(conn):
    """pg_trgm ist da und der Index traegt die Aehnlichkeitssuche (search_menu, T-4.3)."""
    t = _tenant(conn)
    _item(conn, t, "23", name="Frühlingsrollen (4 Stück)")
    _item(conn, t, "47", name="Ente knusprig")
    treffer = conn.execute(
        text(
            "SELECT number FROM menu_items WHERE tenant_id = :t "
            "ORDER BY similarity(name, 'Frühlingsrolle') DESC LIMIT 1"
        ),
        {"t": t},
    ).scalar_one()
    assert treffer == "23"


def test_bestellung_startet_als_draft_ohne_uebergabe(conn):
    t = _tenant(conn)
    order = _order(conn, t, _call(conn, t))
    row = conn.execute(
        text("SELECT status, handover_state, pickup_code FROM orders WHERE id = :o"),
        {"o": order},
    ).one()
    assert tuple(row) == ("draft", None, None)


def test_summe_muss_aufgehen(conn):
    """Ein Rechenfehler im Code scheitert an der Tabelle, nicht erst auf dem Bon."""
    t = _tenant(conn)
    with pytest.raises(IntegrityError):
        _order(conn, t, _call(conn, t), items=1380, fee=250, total=1380)


def test_unbekannter_status_und_uebergabezustand_scheitern(conn):
    t = _tenant(conn)
    order = _order(conn, t, _call(conn, t))
    for spalte, wert in (("status", "geliefert"), ("handover_state", "irgendwie")):
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                text(f"UPDATE orders SET {spalte} = :w WHERE id = :o"),
                {"w": wert, "o": order},
            )


def test_doppelter_idempotency_key_scheitert(conn):
    t = _tenant(conn)
    call = _call(conn, t)
    _order(conn, t, call, key="order-1")
    with pytest.raises(IntegrityError):
        _order(conn, t, call, key="order-1")


def test_bestellung_ohne_call_id_scheitert(conn):
    t = _tenant(conn)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO orders (tenant_id, type, phone, customer_name, "
                "items_total_cents, total_cents, idempotency_key) "
                "VALUES (:t, 'pickup', '+4972215551234', 'Müller', 0, 0, 'k')"
            ),
            {"t": t},
        )


def test_position_ohne_gericht_scheitert(conn):
    """CLAUDE.md §2 Regel 2: ohne menu_item_id keine Position - auch nicht in der DB."""
    t = _tenant(conn)
    order = _order(conn, t, _call(conn, t))
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO order_items (order_id, quantity, unit_price_cents) "
                "VALUES (:o, 1, 690)"
            ),
            {"o": order},
        )


@pytest.mark.parametrize("menge", [0, -2])
def test_menge_muss_positiv_sein(conn, menge):
    t = _tenant(conn)
    order, item = _order(conn, t, _call(conn, t)), _item(conn, t)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price_cents) "
                "VALUES (:o, :i, :q, 690)"
            ),
            {"o": order, "i": item, "q": menge},
        )


def test_bestelltes_gericht_laesst_sich_nicht_loeschen(conn):
    """Die Position friert Preis und Gericht ein; das Gericht verschwindet nicht darunter."""
    t = _tenant(conn)
    order, item = _order(conn, t, _call(conn, t)), _item(conn, t)
    conn.execute(
        text(
            "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price_cents) "
            "VALUES (:o, :i, 2, 690)"
        ),
        {"o": order, "i": item},
    )
    with pytest.raises(IntegrityError):
        conn.execute(text("DELETE FROM menu_items WHERE id = :i"), {"i": item})


def test_optionen_standardmaessig_leere_liste(conn):
    t = _tenant(conn)
    order, item = _order(conn, t, _call(conn, t)), _item(conn, t)
    options = conn.execute(
        text(
            "INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price_cents) "
            "VALUES (:o, :i, 1, 690) RETURNING options"
        ),
        {"o": order, "i": item},
    ).scalar_one()
    assert options == []
