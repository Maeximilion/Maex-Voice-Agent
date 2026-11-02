"""seed_zones (T-6.9): the zone list reaches `delivery_zones` whole or not at all."""

import json
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from api.core.errors import OutOfZone
from api.domain.delivery import check_delivery
from api.models import DeliveryZone
from scripts import seed_zones
from scripts.seed import seed
from scripts.seed_zones import apply, main, parse

HEADER = "name;postal_codes;fee;min_order;eta_minutes\n"
ZONE_1 = "Zone 1;76530, 76532;2,50;20,00;50\n"
ZONE_2 = "Zone 2;76534;4,00;30,00;70\n"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def zones_of(session, tenant_id):
    session.expire_all()
    return {
        row.name: row
        for row in session.scalars(
            select(DeliveryZone).where(DeliveryZone.tenant_id == tenant_id)
        )
    }


def planned(text):
    zones, errors = parse(HEADER + text)
    assert errors == []
    return zones


def test_a_row_becomes_a_zone_in_cents():
    zone = planned(ZONE_1)[0]

    assert (zone.name, zone.postal_codes) == ("Zone 1", ["76530", "76532"])
    assert (zone.fee_cents, zone.min_order_cents, zone.eta_minutes) == (250, 2000, 50)


def test_codes_may_be_split_by_spaces_and_repeat_in_a_row():
    assert planned("Z;76530 76532,76530;0,00;0,00;0\n")[0].postal_codes == [
        "76530",
        "76532",
    ]


@pytest.mark.parametrize(
    ("row", "wanted"),
    [
        (";76530;2,50;20,00;50\n", "name is empty"),
        ("Z;;2,50;20,00;50\n", "no postal code"),
        ("Z;7653;2,50;20,00;50\n", "no postal code: '7653'"),
        ("Z;76530;;20,00;50\n", "fee is no amount"),
        ("Z;76530;-1,00;20,00;50\n", "fee is no amount"),
        ("Z;76530;2,50;viel;50\n", "min_order is no amount"),
        ("Z;76530;2,50;20,00;bald\n", "eta_minutes is no number"),
        ("Z;76530;2,50\n", "min_order is no amount"),
    ],
)
def test_a_bad_row_is_named_with_its_line(row, wanted):
    _, errors = parse(HEADER + row)

    assert any(wanted in error and error.startswith("line 2") for error in errors)


def test_a_code_in_two_zones_is_refused():
    """check_delivery could only hand such a call to the team."""
    _, errors = parse(HEADER + ZONE_1 + "Zone 2;76532;4,00;30,00;70\n")

    assert errors == ["line 3: 76532 is in zone 'Zone 1' too"]


def test_a_zone_named_twice_is_refused():
    _, errors = parse(HEADER + ZONE_1 + "Zone 1;76534;4,00;30,00;70\n")

    assert errors == ["line 3: zone 'Zone 1' named twice"]


def test_a_file_without_a_row_is_refused():
    """It would switch every zone off."""
    assert parse(HEADER)[1] == ["no zone in the file"]


def test_a_missing_column_is_refused():
    assert parse("name;postal_codes\nZ;76530\n")[1] == [
        "columns missing: fee, min_order, eta_minutes"
    ]


def test_check_delivery_finds_what_was_seeded(session, tenant_id):
    assert apply(session, tenant_id, planned(ZONE_1 + ZONE_2)) == {
        "created": 2,
        "updated": 0,
        "deactivated": 0,
    }
    session.commit()

    found = check_delivery(session, tenant_id, "76534")

    assert (found.zone_name, found.fee_cents) == ("Zone 2", 400)


def test_a_second_run_keeps_the_rows_and_takes_the_new_values(session, tenant_id):
    apply(session, tenant_id, planned(ZONE_1))
    session.commit()
    first = zones_of(session, tenant_id)["Zone 1"].id

    report = apply(session, tenant_id, planned("Zone 1;76530;3,00;25,00;60\n"))
    session.commit()

    row = zones_of(session, tenant_id)["Zone 1"]
    assert report == {"created": 0, "updated": 1, "deactivated": 0}
    assert (row.id, row.fee_cents, row.postal_codes) == (first, 300, ["76530"])


def test_a_zone_the_file_no_longer_names_goes_inactive(session, tenant_id):
    apply(session, tenant_id, planned(ZONE_1 + ZONE_2))
    session.commit()

    report = apply(session, tenant_id, planned(ZONE_1))
    session.commit()

    assert report["deactivated"] == 1
    assert zones_of(session, tenant_id)["Zone 2"].active is False
    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, "76534")


def test_a_zone_that_comes_back_is_active_again(session, tenant_id):
    apply(session, tenant_id, planned(ZONE_1 + ZONE_2))
    apply(session, tenant_id, planned(ZONE_1))
    session.commit()

    apply(session, tenant_id, planned(ZONE_1 + ZONE_2))
    session.commit()

    assert zones_of(session, tenant_id)["Zone 2"].active is True


def test_a_polygon_zone_is_left_alone(session, tenant_id):
    session.add(
        DeliveryZone(
            tenant_id=tenant_id,
            name="Form",
            match_type="polygon",
            polygon={"type": "Polygon"},
            fee_cents=100,
            min_order_cents=0,
            eta_minutes=30,
        )
    )
    session.commit()

    apply(session, tenant_id, planned(ZONE_1))
    session.commit()

    assert zones_of(session, tenant_id)["Form"].active is True


def test_another_tenant_keeps_its_zones(session, tenant_id):
    other = uuid.UUID(seed(session, "Anderer", "Europe/Berlin").tenant_id)
    apply(session, other, planned(ZONE_2))
    session.commit()

    apply(session, tenant_id, planned(ZONE_1))
    session.commit()

    assert zones_of(session, other)["Zone 2"].active is True


@pytest.fixture
def cli(engine, session, tenant_id, tmp_path, monkeypatch):
    monkeypatch.setattr(seed_zones, "SessionLocal", sessionmaker(bind=engine))
    path = tmp_path / "zones.csv"

    def run(text, *flags):
        path.write_text(text, encoding="utf-8")
        return main([str(path), "--tenant-name", "Testbetrieb", *flags])

    return run


def test_the_cli_writes_and_reports(cli, session, tenant_id, capsys):
    assert cli(HEADER + ZONE_1) == 0

    assert json.loads(capsys.readouterr().out)["created"] == 1
    assert "Zone 1" in zones_of(session, tenant_id)


def test_a_dry_run_writes_nothing(cli, session, tenant_id, capsys):
    assert cli(HEADER + ZONE_1, "--dry-run") == 0

    assert json.loads(capsys.readouterr().out)["created"] == 1
    assert zones_of(session, tenant_id) == {}


def test_a_file_with_an_error_writes_nothing(cli, session, tenant_id, capsys):
    assert cli(HEADER + ZONE_1 + "Zone 2;7653;4,00;30,00;70\n") == 1

    assert "nothing written" in capsys.readouterr().err
    assert zones_of(session, tenant_id) == {}


def test_a_mark_at_the_start_of_the_file_is_no_part_of_the_header(cli):
    """Excel writes one."""
    assert cli("﻿" + HEADER + ZONE_1) == 0


def test_an_unknown_tenant_writes_nothing(cli, engine, session, tenant_id, tmp_path):
    path = tmp_path / "other.csv"
    path.write_text(HEADER + ZONE_1, encoding="utf-8")

    assert main([str(path), "--tenant-name", "Niemand"]) == 2
    assert zones_of(session, tenant_id) == {}


def test_a_missing_file_ends_with_two(tmp_path):
    assert main([str(tmp_path / "fehlt.csv")]) == 2
