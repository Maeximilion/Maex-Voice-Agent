"""Migration 028: the floor plan tables (T-3.11), up and down."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.tests.conftest import alembic_config as _config

TABLES = {"floor_areas", "dining_tables", "table_combinations"}
_TENANT = text("INSERT INTO tenants (name) VALUES ('T') RETURNING id")
_AREA = text(
    "INSERT INTO floor_areas (tenant_id, name, width_cm, depth_cm)"
    " VALUES (:t, :name, 1000, 700) RETURNING id"
)
_TABLE = text(
    "INSERT INTO dining_tables (tenant_id, area_id, name, seats, width_cm, depth_cm,"
    " x_cm, y_cm) VALUES (:t, :a, :name, 4, 120, 80, 200, 200) RETURNING id"
)


@pytest.fixture
def engine(scratch_db_url):
    command.upgrade(_config(scratch_db_url), "028")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def test_up_creates_the_three_tables_with_their_defaults(engine):
    with engine.begin() as conn:
        tenant = conn.execute(_TENANT).scalar_one()
        area = conn.execute(_AREA, {"t": tenant, "name": "Gastraum"}).scalar_one()
        table = conn.execute(
            _TABLE, {"t": tenant, "a": area, "name": "T1"}
        ).scalar_one()
        other = conn.execute(
            _TABLE, {"t": tenant, "a": area, "name": "T2"}
        ).scalar_one()
        low, high = sorted([table, other])
        conn.execute(
            text(
                "INSERT INTO table_combinations (tenant_id, table_a_id, table_b_id)"
                " VALUES (:t, :a, :b)"
            ),
            {"t": tenant, "a": low, "b": high},
        )
        row = conn.execute(
            text("SELECT kind, shape, wall_cm, fixtures, revision FROM floor_areas")
        ).one()
        assert tuple(row) == ("inside", "rectangle", 20, [], 0)
        seat = conn.execute(
            text("SELECT shape, rotation, end_seat FROM dining_tables WHERE id = :i"),
            {"i": table},
        ).one()
        assert tuple(seat) == ("rect", 0, "left")


@pytest.mark.parametrize(
    "statement",
    [
        # A second live table of the same name, in other case.
        "INSERT INTO dining_tables (tenant_id, area_id, name, seats, width_cm,"
        " depth_cm, x_cm, y_cm) VALUES (:t, :a, 't1', 2, 80, 80, 100, 100)",
        # A square room with two different sides.
        "INSERT INTO floor_areas (tenant_id, name, shape, width_cm, depth_cm)"
        " VALUES (:t, 'Q', 'square', 800, 700)",
        # A round table that is not round.
        "INSERT INTO dining_tables (tenant_id, area_id, name, shape, seats, width_cm,"
        " depth_cm, x_cm, y_cm) VALUES (:t, :a, 'R', 'round', 4, 100, 90, 100, 100)",
        # A table turned by 45 degrees, and a pair in the wrong order.
        "INSERT INTO dining_tables (tenant_id, area_id, name, seats, width_cm,"
        " depth_cm, x_cm, y_cm, rotation) VALUES (:t, :a, 'S', 4, 80, 80, 1, 1, 45)",
        "INSERT INTO table_combinations (tenant_id, table_a_id, table_b_id)"
        " VALUES (:t, :hi, :lo)",
        # A second room of the same name.
        "INSERT INTO floor_areas (tenant_id, name, width_cm, depth_cm)"
        " VALUES (:t, 'GASTRAUM', 800, 700)",
        # No seat, and a name of blanks.
        "INSERT INTO dining_tables (tenant_id, area_id, name, seats, width_cm,"
        " depth_cm, x_cm, y_cm) VALUES (:t, :a, 'Z', 0, 80, 80, 1, 1)",
        "INSERT INTO floor_areas (tenant_id, name, width_cm, depth_cm)"
        " VALUES (:t, '   ', 800, 700)",
    ],
)
def test_the_checks_refuse_what_the_plan_cannot_be(engine, statement):
    with engine.begin() as conn:
        tenant = conn.execute(_TENANT).scalar_one()
        area = conn.execute(_AREA, {"t": tenant, "name": "Gastraum"}).scalar_one()
        one = conn.execute(_TABLE, {"t": tenant, "a": area, "name": "T1"}).scalar_one()
        two = conn.execute(_TABLE, {"t": tenant, "a": area, "name": "T2"}).scalar_one()
    lo, hi = sorted([one, two])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(statement), {"t": tenant, "a": area, "lo": lo, "hi": hi})


def test_a_removed_table_frees_its_name(engine):
    with engine.begin() as conn:
        tenant = conn.execute(_TENANT).scalar_one()
        area = conn.execute(_AREA, {"t": tenant, "name": "Gastraum"}).scalar_one()
        old = conn.execute(_TABLE, {"t": tenant, "a": area, "name": "T1"}).scalar_one()
        conn.execute(
            text("UPDATE dining_tables SET deleted_at = now() WHERE id = :i"),
            {"i": old},
        )
        conn.execute(_TABLE, {"t": tenant, "a": area, "name": "T1"})
        count = conn.execute(text("SELECT count(*) FROM dining_tables")).scalar_one()
    assert count == 2


def test_down_drops_the_three_and_keeps_the_rest(engine, scratch_db_url):
    with engine.begin() as conn:
        tenant = conn.execute(_TENANT).scalar_one()
        area = conn.execute(_AREA, {"t": tenant, "name": "Gastraum"}).scalar_one()
        conn.execute(_TABLE, {"t": tenant, "a": area, "name": "T1"})

    command.downgrade(_config(scratch_db_url), "027")

    names = set(inspect(engine).get_table_names())
    assert not TABLES & names
    assert {"tenants", "capacity", "reservations"} <= names
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 1
