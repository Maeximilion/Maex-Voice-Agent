"""Migration 018: customers, addresses, delivery zones and the objection to
recognition (T-6.1, L12), up and down."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

TABLES = {"customers", "addresses", "delivery_zones", "recognition_objections"}
PHONE = "+4972215551234"


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "018")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _tenant(conn, name="One"):
    return conn.execute(
        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
    ).scalar_one()


def _customer(conn, tenant, phone=PHONE, name="Müller"):
    return conn.execute(
        text(
            "INSERT INTO customers (tenant_id, phone, name) "
            "VALUES (:t, :p, :n) RETURNING id"
        ),
        {"t": tenant, "p": phone, "n": name},
    ).scalar_one()


def _address(conn, tenant, customer, is_default=False):
    return conn.execute(
        text(
            "INSERT INTO addresses (tenant_id, customer_id, street, house_number, "
            "postal_code, city, is_default) "
            "VALUES (:t, :c, 'Hauptstr.', '1', '76530', 'Ort', :d) RETURNING id"
        ),
        {"t": tenant, "c": customer, "d": is_default},
    ).scalar_one()


def _order(conn, tenant, kind="delivery", customer=None, address=None):
    call = conn.execute(
        text(
            "INSERT INTO calls (tenant_id, external_session_id, started_at, "
            "delete_after) VALUES (:t, :e, now(), current_date) RETURNING id"
        ),
        {"t": tenant, "e": uuid.uuid4().hex},
    ).scalar_one()
    return conn.execute(
        text(
            "INSERT INTO orders (tenant_id, call_id, type, phone, customer_name, "
            "customer_id, address_id, items_total_cents, total_cents, "
            "idempotency_key) "
            "VALUES (:t, :call, :kind, :p, 'Müller', :c, :a, 0, 0, :k) RETURNING id"
        ),
        {
            "t": tenant,
            "call": call,
            "kind": kind,
            "p": PHONE,
            "c": customer,
            "a": address,
            "k": uuid.uuid4().hex,
        },
    ).scalar_one()


def _zone(conn, tenant, match_type="postal_code", codes="{76530}", polygon=None):
    conn.execute(
        text(
            "INSERT INTO delivery_zones (tenant_id, name, match_type, postal_codes, "
            "polygon, fee_cents, min_order_cents, eta_minutes) VALUES "
            "(:t, 'Zone 1', :m, CAST(:codes AS text[]), CAST(:polygon AS jsonb), "
            "250, 1500, 40)"
        ),
        {"t": tenant, "m": match_type, "codes": codes, "polygon": polygon},
    )


def test_several_customers_share_one_phone(engine):
    """L12: a name that does not match means a new customer record."""
    with engine.begin() as conn:
        tenant = _tenant(conn)
        first = _customer(conn, tenant, name="Müller")
        second = _customer(conn, tenant, name="Schmidt")

    assert first != second


def test_last_contact_starts_with_the_record(engine):
    """The 24-month period of docs/03 runs from it, so it is never empty."""
    with engine.begin() as conn:
        tenant = _tenant(conn)
        customer = _customer(conn, tenant)
        row = (
            conn.execute(
                text(
                    "SELECT last_contact_at, order_count, blocked FROM customers "
                    "WHERE id = :c"
                ),
                {"c": customer},
            )
            .mappings()
            .one()
        )

    assert row["last_contact_at"] is not None
    assert (row["order_count"], row["blocked"]) == (0, False)


@pytest.mark.parametrize("phone", ["07221 5551234", "", "+0721555"])
def test_a_customer_needs_a_phone_in_e164(engine, phone):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _customer(conn, _tenant(conn), phone=phone)


def _object(conn, tenant, phone=PHONE):
    conn.execute(
        text("INSERT INTO recognition_objections (tenant_id, phone) VALUES (:t, :p)"),
        {"t": tenant, "p": phone},
    )


def test_an_objection_holds_once_per_number_and_tenant(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        other = _tenant(conn, "Two")
        _object(conn, tenant)
        _object(conn, other)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _object(conn, tenant)


def test_an_objection_outlives_the_customer(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        customer = _customer(conn, tenant)
        _object(conn, tenant)
        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer})
        left = conn.execute(
            text("SELECT count(*) FROM recognition_objections")
        ).scalar_one()

    assert left == 1


def test_an_objection_in_another_spelling_is_refused(engine):
    """Recognition compares in E.164: stored differently, the objection would
    switch nothing off, and nobody would notice."""
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _object(conn, _tenant(conn), phone="07221 5551234")


def test_one_default_address_per_customer(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        customer = _customer(conn, tenant)
        _address(conn, tenant, customer, is_default=True)
        _address(conn, tenant, customer)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _address(conn, tenant, customer, is_default=True)


def test_an_address_stays_with_the_customers_tenant(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        other = _tenant(conn, "Two")
        customer = _customer(conn, tenant)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _address(conn, other, customer)


def test_deleting_a_customer_takes_the_addresses_and_leaves_the_order(engine):
    """T-6.7 deletes the customer after 24 months. The order is a tax row
    (L18): it stays, without the links."""
    with engine.begin() as conn:
        tenant = _tenant(conn)
        customer = _customer(conn, tenant)
        address = _address(conn, tenant, customer)
        order = _order(conn, tenant, customer=customer, address=address)

        conn.execute(text("DELETE FROM customers WHERE id = :c"), {"c": customer})

        row = (
            conn.execute(
                text(
                    "SELECT tenant_id, customer_id, address_id FROM orders "
                    "WHERE id = :o"
                ),
                {"o": order},
            )
            .mappings()
            .one()
        )
        addresses = conn.execute(text("SELECT count(*) FROM addresses")).scalar_one()

    assert addresses == 0
    assert (row["tenant_id"], row["customer_id"], row["address_id"]) == (
        tenant,
        None,
        None,
    )


def test_an_order_takes_no_customer_of_another_tenant(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        other = _tenant(conn, "Two")
        customer = _customer(conn, other)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _order(conn, tenant, customer=customer)


def test_an_address_on_an_order_needs_a_delivery(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        address = _address(conn, tenant, _customer(conn, tenant))
        _order(conn, tenant, address=address)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _order(conn, tenant, kind="pickup", address=address)


@pytest.mark.parametrize(
    "match_type, codes, polygon",
    [
        ("postal_code", None, None),
        ("postal_code", "{}", None),
        ("polygon", "{76530}", None),
        ("radius", "{76530}", None),
    ],
)
def test_a_zone_needs_what_it_matches_on(engine, match_type, codes, polygon):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _zone(conn, _tenant(conn), match_type, codes, polygon)


def test_a_zone_takes_postal_codes_or_a_polygon(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        _zone(conn, tenant)
        _zone(conn, tenant, "polygon", None, '{"type": "Polygon"}')
        count = conn.execute(text("SELECT count(*) FROM delivery_zones")).scalar_one()

    assert count == 2


@pytest.mark.parametrize("column", ["fee_cents", "min_order_cents", "eta_minutes"])
def test_a_zone_refuses_a_negative_amount(engine, column):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        _zone(conn, tenant)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(f"UPDATE delivery_zones SET {column} = -1"))


def test_deleting_a_zone_leaves_the_address(engine):
    with engine.begin() as conn:
        tenant = _tenant(conn)
        _zone(conn, tenant)
        zone = conn.execute(text("SELECT id FROM delivery_zones")).scalar_one()
        address = _address(conn, tenant, _customer(conn, tenant))
        conn.execute(
            text("UPDATE addresses SET zone_id = :z WHERE id = :a"),
            {"z": zone, "a": address},
        )

        conn.execute(text("DELETE FROM delivery_zones"))

        row = conn.execute(
            text("SELECT tenant_id, zone_id FROM addresses WHERE id = :a"),
            {"a": address},
        ).one()

    assert row == (tenant, None)


def _schema(url: str) -> tuple[set[str], set[str]]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        return (
            set(inspector.get_table_names()),
            {c["name"] for c in inspector.get_columns("orders")},
        )
    finally:
        engine.dispose()


def test_downgrade_removes_only_this_step(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "017")
    tables, columns = _schema(scratch_db_url)
    assert not TABLES & tables
    assert "address_id" not in columns

    command.upgrade(_config(scratch_db_url), "018")
    assert _schema(scratch_db_url)[0] == tables | TABLES
    assert _schema(scratch_db_url)[1] == columns | {"address_id"}

    command.downgrade(_config(scratch_db_url), "017")
    assert _schema(scratch_db_url) == (tables, columns)

    command.upgrade(_config(scratch_db_url), "head")
    assert _schema(scratch_db_url)[0] >= TABLES
