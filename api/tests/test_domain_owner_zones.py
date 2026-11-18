"""The owner's delivery zones (#571, plan number 8): postal codes, fee, minimum
order and delivery time behind the admin PIN. Every save is checked against
what the call path reads (`check_delivery`, `seed_zones`), so no save leaves a
state those readers refuse or guess from."""

import contextlib
import uuid

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.core.errors import (
    Conflict,
    FieldErrors,
    InvalidInput,
    NotFound,
    OutOfZone,
    ServiceUnavailable,
)
from api.domain.delivery import owner_zones
from api.domain.delivery.owner_zones import Typed
from api.domain.delivery.zones import check_delivery
from api.models import (
    Address,
    AuditLog,
    Customer,
    DeliveryZone,
    ServiceConfig,
    Tenant,
)
from scripts import seed_zones
from scripts.seed import seed


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def _typed(**changes) -> Typed:
    fields = {
        "name": "Zone 1",
        "postal_codes": "76530, 76532",
        "fee": "2,50",
        "min_order": "15",
        "eta": "40",
        "active": True,
    }
    return Typed(**{**fields, **changes})


def _new(session, tenant_id, **changes) -> DeliveryZone:
    assert owner_zones.save_zone(session, tenant_id, None, _typed(**changes), "")
    name = changes.get("name", "Zone 1").strip()
    return session.scalars(
        select(DeliveryZone).where(
            DeliveryZone.tenant_id == tenant_id, DeliveryZone.name == name
        )
    ).one()


def _change(session, tenant_id, zone, seen=None, **changes) -> bool:
    shown = owner_zones.token(owner_zones.zone(session, tenant_id, zone.id))
    return owner_zones.save_zone(
        session,
        tenant_id,
        zone.id,
        _typed(**changes),
        shown if seen is None else seen,
    )


def _audits(session, action) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
    )


def _say(error) -> str:
    return error.value.say or ""


# --- a new zone -------------------------------------------------------------------


def test_a_new_zone_is_what_the_call_path_reads(session, tenant_id):
    zone = _new(session, tenant_id)
    assert zone.match_type == "postal_code" and zone.active
    assert zone.postal_codes == ["76530", "76532"]
    assert (zone.fee_cents, zone.min_order_cents, zone.eta_minutes) == (250, 1500, 40)
    found = check_delivery(session, tenant_id, "76532")
    assert (found.zone_id, found.fee_cents, found.min_order_cents) == (
        zone.id,
        250,
        1500,
    )
    assert found.eta_minutes == 40

    (audit,) = _audits(session, "delivery_zone.created")
    assert audit.actor == "gui:owner" and audit.entity == "delivery_zones"
    assert audit.entity_id == zone.id
    assert audit.payload == {
        "from": None,
        "to": {
            "name": "Zone 1",
            "postal_codes": ["76530", "76532"],
            "fee_cents": 250,
            "min_order_cents": 1500,
            "eta_minutes": 40,
            "active": True,
        },
    }


def test_the_list_holds_every_postal_code_zone_active_ones_first(session, tenant_id):
    _new(session, tenant_id, name="Zone B", postal_codes="76534")
    _new(session, tenant_id, name="Zone A", postal_codes="76530", active=False)
    _new(session, tenant_id, name="Zone C", postal_codes="76532")
    listed = owner_zones.zones(session, tenant_id)
    assert [zone.name for zone in listed] == ["Zone B", "Zone C", "Zone A"]


@pytest.mark.parametrize(
    ("typed", "cents"),
    [("0", 0), ("0,00", 0), ("3", 300), ("3,5", 350), ("3,05", 305), (" 12,90 ", 1290)],
)
def test_an_amount_is_euros_with_a_decimal_comma(session, tenant_id, typed, cents):
    zone = _new(session, tenant_id, fee=typed, min_order=typed)
    assert (zone.fee_cents, zone.min_order_cents) == (cents, cents)


@pytest.mark.parametrize(
    "typed",
    [
        "",  # an empty field is no free delivery
        "2.50",  # a dot is no decimal comma: 2.50 must never become 250 euros
        "2,505",
        "-1",
        "+1",
        "1e2",
        "2,50 €",
        "zwei",
        "٢,٥٠",  # noqa: RUF001  digits `int` reads and the form never shows
    ],
)
@pytest.mark.parametrize("field", ["fee", "min_order"])
def test_what_is_no_amount_is_refused_by_name(session, tenant_id, field, typed):
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(session, tenant_id, None, _typed(**{field: typed}), "")
    word = "Lieferpauschale" if field == "fee" else "Mindestbestellwert"
    assert word in _say(error) and "2,50" in _say(error)
    assert owner_zones.zones(session, tenant_id) == []


def test_an_amount_above_the_bound_of_the_form_is_refused(session, tenant_id):
    """A bound on a typing error (250 for 2,50), far below what an INT holds."""
    for field, most, over in (
        ("fee", "50,00", "50,01"),
        ("min_order", "500", "500,01"),
    ):
        with pytest.raises(InvalidInput) as error:
            owner_zones.save_zone(session, tenant_id, None, _typed(**{field: over}), "")
        assert "höchstens" in _say(error)
        with pytest.raises(InvalidInput):
            owner_zones.save_zone(
                session, tenant_id, None, _typed(**{field: "99999999999999999999"}), ""
            )
        zone = _new(
            session,
            tenant_id,
            name=f"Zone {field}",
            postal_codes="76534",
            **{field: most},
        )
        zone.active = False
        session.commit()


@pytest.mark.parametrize(
    "typed", ["", "-5", "181", "9999", "12345", "4,5", "vierzig", "1e2", "٤٠"]
)
def test_a_delivery_time_is_whole_minutes_up_to_the_longest_wait(
    session, tenant_id, typed
):
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(session, tenant_id, None, _typed(eta=typed), "")
    assert "Lieferzeit" in _say(error) and "180" in _say(error)
    assert _new(session, tenant_id, eta="0").eta_minutes == 0
    assert _new(session, tenant_id, name="Zone 2", postal_codes="76534", eta="180")


@pytest.mark.parametrize(
    "typed",
    ["7653", "765300", "7653O", "76530;76532", "D-76530", "٧٦٥٣٠"],
)
def test_a_postal_code_is_five_digits(session, tenant_id, typed):
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(session, tenant_id, None, _typed(postal_codes=typed), "")
    assert "fünf Ziffern" in _say(error)
    assert owner_zones.zones(session, tenant_id) == []


def test_postal_codes_are_split_on_commas_and_spaces_and_kept_once(session, tenant_id):
    zone = _new(session, tenant_id, postal_codes=" 76532,76530\n76532   76534, ")
    assert zone.postal_codes == ["76532", "76530", "76534"]


def test_a_zone_needs_a_postal_code_and_a_name(session, tenant_id):
    for changes, word in (
        ({"postal_codes": " , "}, "Postleitzahl"),
        ({"name": "   "}, "Namen"),
        ({"name": "x" * 41}, "40 Zeichen"),
        ({"name": "Zone\x001"}, "nicht angezeigt"),
        ({"name": "Zone\u202e1"}, "nicht angezeigt"),
        ({"name": "\u200bZone 1"}, "nicht angezeigt"),
        ({"name": "---"}, "Namen"),
        ({"name": "\u2800"}, "Namen"),
    ):
        with pytest.raises(InvalidInput) as error:
            owner_zones.save_zone(session, tenant_id, None, _typed(**changes), "")
        assert word in _say(error), changes
    assert owner_zones.zones(session, tenant_id) == []
    assert _new(session, tenant_id, name="  " + "x" * 40 + " ").name == "x" * 40


def test_more_postal_codes_than_the_form_takes_are_refused(session, tenant_id):
    many = ",".join(f"{code:05d}" for code in range(10000, 10000 + 201))
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(session, tenant_id, None, _typed(postal_codes=many), "")
    assert "200" in _say(error)


# --- what the readers refuse: a code in two active zones --------------------------


def test_a_postal_code_of_another_active_zone_is_refused(session, tenant_id):
    """`check_delivery` answers a code in two active zones with "not
    available": the fee would be whichever row came first."""
    _new(session, tenant_id)
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(
            session,
            tenant_id,
            None,
            _typed(name="Zone 2", postal_codes="76534 76532"),
            "",
        )
    assert "76532" in _say(error) and "Zone 1" in _say(error)
    assert len(owner_zones.zones(session, tenant_id)) == 1
    assert check_delivery(session, tenant_id, "76532").zone_name == "Zone 1"

    other = _new(session, tenant_id, name="Zone 2", postal_codes="76534")
    with pytest.raises(InvalidInput) as error:
        _change(session, tenant_id, other, name="Zone 2", postal_codes="76534,76530")
    assert "76530" in _say(error)
    assert check_delivery(session, tenant_id, "76530").zone_name == "Zone 1"


def test_an_inactive_zone_may_hold_a_code_and_cannot_come_back_onto_it(
    session, tenant_id
):
    first = _new(session, tenant_id)
    # Switched off, its codes are free for another zone.
    assert _change(session, tenant_id, first, active=False)
    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, "76530")
    _new(session, tenant_id, name="Zone 2", postal_codes="76530", fee="4")
    assert check_delivery(session, tenant_id, "76530").fee_cents == 400

    # Switched on again it would be the second active zone of 76530.
    with pytest.raises(InvalidInput) as error:
        _change(session, tenant_id, first, active=True)
    assert "76530" in _say(error) and "Zone 2" in _say(error)
    assert check_delivery(session, tenant_id, "76530").fee_cents == 400
    # Without the code it comes back.
    assert _change(session, tenant_id, first, postal_codes="76532", active=True)
    assert check_delivery(session, tenant_id, "76532").zone_name == "Zone 1"
    # Still switched off, a zone may be given a code another one delivers to.
    third = _new(session, tenant_id, name="Zone 3", postal_codes="76534", active=False)
    assert _change(
        session,
        tenant_id,
        third,
        name="Zone 3",
        postal_codes="76534,76530",
        active=False,
    )
    assert check_delivery(session, tenant_id, "76530").zone_name == "Zone 2"


def test_no_save_leaves_a_code_the_call_path_refuses(session, tenant_id):
    """Every code of every active zone resolves to exactly one zone, whatever
    order the saves came in."""
    first = _new(session, tenant_id)
    second = _new(session, tenant_id, name="Zone 2", postal_codes="76534 76547")
    attempts = (
        (first, {"postal_codes": "76530,76534"}),
        (second, {"name": "Zone 2", "postal_codes": "76532"}),
        (first, {"postal_codes": "76547,76549"}),
        (second, {"name": "Zone 2", "postal_codes": "76549,76534", "active": False}),
        (second, {"name": "Zone 2", "postal_codes": "76549,76534"}),
    )
    for zone, changes in attempts:
        with contextlib.suppress(InvalidInput):
            _change(session, tenant_id, zone, **changes)
        for row in owner_zones.zones(session, tenant_id):
            for code in row.postal_codes or []:
                if row.active:
                    assert check_delivery(session, tenant_id, code).zone_id == row.id


def test_a_zone_stored_with_a_shared_code_is_refused_until_the_code_goes(
    session, tenant_id
):
    """A state only SQL can make: the save names it instead of keeping it."""
    first = _new(session, tenant_id)
    second = _new(session, tenant_id, name="Zone 2", postal_codes="76534")
    session.execute(
        update(DeliveryZone)
        .where(DeliveryZone.id == second.id)
        .values(postal_codes=["76534", "76530"])
    )
    session.commit()
    with pytest.raises(ServiceUnavailable):
        check_delivery(session, tenant_id, "76530")
    with pytest.raises(InvalidInput) as error:
        _change(session, tenant_id, first, fee="3")
    assert "76530" in _say(error)
    assert _change(session, tenant_id, first, postal_codes="76532", fee="3")
    assert check_delivery(session, tenant_id, "76530").zone_name == "Zone 2"


# --- names: the import finds a zone by its name -----------------------------------


def test_a_name_is_taken_once_whatever_the_case_and_the_state(session, tenant_id):
    """`seed_zones` keeps one row per name: a second zone of a name would be
    the one it neither updates nor switches off."""
    first = _new(session, tenant_id)
    _change(session, tenant_id, first, active=False)
    for name in ("Zone 1", "zone 1", " ZONE 1 "):
        with pytest.raises(InvalidInput) as error:
            owner_zones.save_zone(
                session, tenant_id, None, _typed(name=name, postal_codes="76534"), ""
            )
        assert "Namen gibt es schon" in _say(error)
    other = _new(session, tenant_id, name="Zone 2", postal_codes="76534")
    with pytest.raises(InvalidInput):
        _change(session, tenant_id, other, name="zone 1", postal_codes="76534")
    # Its own name in another case is a rename, not a clash.
    assert _change(session, tenant_id, other, name="ZONE 2", postal_codes="76534")


def test_the_import_after_the_tab_leaves_no_code_in_two_zones(session, tenant_id):
    first = _new(session, tenant_id)
    _new(session, tenant_id, name="Zone 2", postal_codes="76534")
    assert _change(session, tenant_id, first, name="Innenstadt")
    zones, errors = seed_zones.parse(
        "name;postal_codes;fee;min_order;eta_minutes\n"
        "Zone 1;76530 76534;3,00;20,00;30\n"
    )
    assert errors == []
    seed_zones.apply(session, tenant_id, zones)
    session.commit()
    for code in ("76530", "76534"):
        assert check_delivery(session, tenant_id, code).zone_name == "Zone 1"
    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, "76532")


def test_a_stored_name_the_form_would_refuse_does_not_block_the_fee(session, tenant_id):
    zone = _new(session, tenant_id)
    long_name = "Zone " + "x" * 60
    session.execute(
        update(DeliveryZone).where(DeliveryZone.id == zone.id).values(name=long_name)
    )
    session.commit()
    assert _change(session, tenant_id, zone, name=long_name, fee="3")
    assert check_delivery(session, tenant_id, "76530").fee_cents == 300


def test_values_the_import_wrote_beyond_the_bounds_stay_until_they_are_changed(
    session, tenant_id
):
    """The bounds catch a typing error. A zone stored beyond them (the import
    takes any amount and up to 9999 minutes) still takes a change of its
    other fields, and a new value for the field itself meets the bound."""
    zone = _new(session, tenant_id)
    session.execute(
        update(DeliveryZone)
        .where(DeliveryZone.id == zone.id)
        .values(fee_cents=6000, min_order_cents=60000, eta_minutes=240)
    )
    session.commit()
    stored = {"fee": "60,00", "min_order": "600,00", "eta": "240"}
    assert _change(session, tenant_id, zone, **stored) is False
    assert _change(session, tenant_id, zone, **stored, postal_codes="76530")
    assert check_delivery(session, tenant_id, "76530").fee_cents == 6000
    for field, other in (("fee", "61"), ("min_order", "601"), ("eta", "241")):
        with pytest.raises(InvalidInput):
            _change(
                session,
                tenant_id,
                zone,
                **{**stored, field: other},
                postal_codes="76530",
            )
    assert _change(
        session,
        tenant_id,
        zone,
        postal_codes="76530",
        fee="5",
        min_order="20",
        eta="60",
    )
    # Back within the bounds, the old values are no way around them.
    with pytest.raises(InvalidInput):
        _change(session, tenant_id, zone, postal_codes="76530", **stored)


# --- a change ---------------------------------------------------------------------


def test_a_change_holds_for_the_next_call_and_leaves_an_audit_row(session, tenant_id):
    zone = _new(session, tenant_id)
    assert _change(
        session,
        tenant_id,
        zone,
        name="Innenstadt",
        postal_codes="76530",
        fee="3",
        min_order="20,50",
        eta="55",
    )
    found = check_delivery(session, tenant_id, "76530")
    assert (found.zone_name, found.fee_cents) == ("Innenstadt", 300)
    assert (found.min_order_cents, found.eta_minutes) == (2050, 55)
    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, "76532")

    (audit,) = _audits(session, "delivery_zone.changed")
    assert audit.actor == "gui:owner" and audit.entity_id == zone.id
    payload = audit.payload or {}
    assert payload["from"]["fee_cents"] == 250
    assert payload["from"]["postal_codes"] == ["76530", "76532"]
    assert payload["to"] == {
        "name": "Innenstadt",
        "postal_codes": ["76530"],
        "fee_cents": 300,
        "min_order_cents": 2050,
        "eta_minutes": 55,
        "active": True,
    }


def test_an_untouched_save_writes_nothing_whatever_the_token(session, tenant_id):
    zone = _new(session, tenant_id)
    before = zone.updated_at
    assert _change(session, tenant_id, zone, seen="stale") is False
    # The same values written another way are no change either.
    assert (
        _change(
            session,
            tenant_id,
            zone,
            seen="stale",
            postal_codes="76530 76532 76530",
            fee="2,5",
            min_order="15,00",
            eta="040",
        )
        is False
    )
    session.refresh(zone)
    assert zone.updated_at == before
    assert _audits(session, "delivery_zone.changed") == []


def test_a_zone_changed_since_it_was_shown_is_not_written_over(session, tenant_id):
    zone = _new(session, tenant_id)
    shown = owner_zones.token(zone)
    assert _change(session, tenant_id, zone, fee="3")
    with pytest.raises(Conflict) as error:
        _change(session, tenant_id, zone, seen=shown, fee="4")
    assert "gerade geändert" in _say(error)
    assert check_delivery(session, tenant_id, "76530").fee_cents == 300
    assert len(_audits(session, "delivery_zone.changed")) == 1


def test_a_new_zone_sent_twice_is_one_zone(session, tenant_id):
    _new(session, tenant_id)
    assert owner_zones.save_zone(session, tenant_id, None, _typed(), "") is False
    assert len(owner_zones.zones(session, tenant_id)) == 1
    assert len(_audits(session, "delivery_zone.created")) == 1


def test_a_refused_save_writes_nothing(session, tenant_id):
    zone = _new(session, tenant_id)
    for changes in (
        {"fee": "viel"},
        {"postal_codes": "7653"},
        {"eta": "999"},
        {"name": ""},
    ):
        with pytest.raises(InvalidInput):
            _change(session, tenant_id, zone, **changes)
    session.refresh(zone)
    assert (zone.name, zone.fee_cents, zone.postal_codes) == (
        "Zone 1",
        250,
        ["76530", "76532"],
    )
    assert _audits(session, "delivery_zone.changed") == []


def test_no_zone_of_that_id_and_no_zone_of_another_restaurant(session, tenant_id):
    with pytest.raises(NotFound):
        owner_zones.save_zone(session, tenant_id, uuid.uuid4(), _typed(), "")
    other = Tenant(name="Other", timezone="Europe/Berlin")
    session.add(other)
    session.flush()
    theirs = DeliveryZone(
        tenant_id=other.id,
        name="Zone 1",
        match_type="postal_code",
        postal_codes=["76530"],
        fee_cents=100,
        min_order_cents=0,
        eta_minutes=30,
    )
    session.add(theirs)
    session.commit()
    with pytest.raises(NotFound):
        owner_zones.zone(session, tenant_id, theirs.id)
    with pytest.raises(NotFound):
        owner_zones.save_zone(session, tenant_id, theirs.id, _typed(fee="9"), "")
    # Their name and their code are theirs: no clash across restaurants.
    assert _new(session, tenant_id).postal_codes == ["76530", "76532"]
    session.refresh(theirs)
    assert theirs.fee_cents == 100
    assert owner_zones.zones(session, tenant_id)[0].tenant_id == tenant_id


def test_a_polygon_zone_is_neither_listed_nor_changed(session, tenant_id):
    polygon = DeliveryZone(
        tenant_id=tenant_id,
        name="Karte",
        match_type="polygon",
        polygon={"type": "Polygon", "coordinates": []},
        fee_cents=100,
        min_order_cents=0,
        eta_minutes=30,
    )
    session.add(polygon)
    session.commit()
    assert owner_zones.zones(session, tenant_id) == []
    with pytest.raises(NotFound):
        owner_zones.save_zone(session, tenant_id, polygon.id, _typed(), "")
    # Its name is taken all the same.
    with pytest.raises(InvalidInput):
        owner_zones.save_zone(session, tenant_id, None, _typed(name="Karte"), "")


def test_a_zone_switched_off_keeps_the_addresses_that_point_at_it(session, tenant_id):
    zone = _new(session, tenant_id)
    customer = Customer(tenant_id=tenant_id, phone="+4972215551234", name="Kim")
    session.add(customer)
    session.flush()
    address = Address(
        tenant_id=tenant_id,
        customer_id=customer.id,
        street="Hauptstraße",
        house_number="1",
        postal_code="76530",
        city="Baden-Baden",
        zone_id=zone.id,
    )
    session.add(address)
    session.commit()
    assert _change(session, tenant_id, zone, active=False)
    session.refresh(address)
    assert address.zone_id == zone.id


def test_a_save_waits_for_the_other_save_of_the_restaurant(
    session, tenant_id, migrated_db_url
):
    """Two devices cannot each add one code to a different zone: the check
    reads the other zones under the lock of the settings row."""
    _new(session, tenant_id)
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as other:
            other.execute(
                select(ServiceConfig)
                .where(ServiceConfig.tenant_id == tenant_id)
                .with_for_update()
            )
            session.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError):
                owner_zones.save_zone(
                    session,
                    tenant_id,
                    None,
                    _typed(name="Zone 2", postal_codes="76534"),
                    "",
                )
            session.rollback()
    finally:
        engine.dispose()
    assert len(owner_zones.zones(session, tenant_id)) == 1


# --- found by the review --------------------------------------------------------------


def test_the_import_waits_for_a_save_on_the_tablet(session, tenant_id, migrated_db_url):
    """The import takes the lock of the tab, so its rows and the tab's check
    for a code in two zones never interleave."""
    zones, errors = seed_zones.parse(
        "name;postal_codes;fee;min_order;eta_minutes\nZone 1;76530;3,00;20,00;30\n"
    )
    assert errors == []
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as other:
            other.execute(
                select(ServiceConfig)
                .where(ServiceConfig.tenant_id == tenant_id)
                .with_for_update()
            )
            session.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError):
                seed_zones.apply(session, tenant_id, zones)
            session.rollback()
    finally:
        engine.dispose()
    assert owner_zones.zones(session, tenant_id) == []
    # Without a save in its way it writes as before.
    assert seed_zones.apply(session, tenant_id, zones)["created"] == 1
    session.commit()
    assert check_delivery(session, tenant_id, "76530").fee_cents == 300


def test_stored_postal_codes_beyond_the_bounds_stay_until_they_are_changed(
    session, tenant_id
):
    """The import knows no limit of codes per zone. Such a zone still takes a
    new fee and, above all, can be switched off."""
    zone = _new(session, tenant_id)
    many = [f"{code:05d}" for code in range(20000, 20000 + 201)]
    session.execute(
        update(DeliveryZone).where(DeliveryZone.id == zone.id).values(postal_codes=many)
    )
    session.commit()
    stored = ", ".join(many)
    assert _change(session, tenant_id, zone, postal_codes=stored) is False
    assert _change(session, tenant_id, zone, postal_codes=stored, fee="3")
    assert _change(session, tenant_id, zone, postal_codes=stored, fee="3", active=False)
    assert _change(session, tenant_id, zone, postal_codes=stored, fee="3", active=True)
    assert check_delivery(session, tenant_id, "20200").fee_cents == 300
    # One code more is a change of the list, and meets the bound.
    with pytest.raises(InvalidInput) as error:
        _change(session, tenant_id, zone, postal_codes=stored + ", 30000", fee="3")
    assert "200" in _say(error)


def test_a_stored_code_that_is_no_postal_code_does_not_block_the_zone(
    session, tenant_id
):
    zone = _new(session, tenant_id)
    session.execute(
        update(DeliveryZone)
        .where(DeliveryZone.id == zone.id)
        .values(postal_codes=["76530", "7653"])
    )
    session.commit()
    assert _change(session, tenant_id, zone, postal_codes="76530, 7653", active=False)
    # A changed list is read whole: the stored code that is none is named, and
    # has to go before another code is added.
    with pytest.raises(InvalidInput) as error:
        _change(
            session, tenant_id, zone, postal_codes="76530, 7653, 76532", active=False
        )
    assert "„7653“" in _say(error)
    assert _change(session, tenant_id, zone, postal_codes="76530, 76532", active=False)
    with pytest.raises(InvalidInput) as error:
        _change(
            session, tenant_id, zone, postal_codes="76530, 76532, 765", active=False
        )
    assert "„765“" in _say(error)


def test_a_sign_that_turns_the_order_of_a_number_is_refused(session, tenant_id):
    """A direction override shows "05,2" as "2,50". Dropped like a zero-width
    space, the fee saved would be 5,20 where the owner read 2,50."""
    override = chr(0x202E)
    for changes in (
        {"fee": override + "05,2"},
        {"min_order": override + "51"},
        {"postal_codes": override + "03567"},
        {"eta": override + "04"},
    ):
        with pytest.raises(InvalidInput):
            owner_zones.save_zone(session, tenant_id, None, _typed(**changes), "")
    # Every other mark of direction is refused too: only the six characters
    # of `UNSEEN` are dropped.
    for mark in (0x200E, 0x200F, 0x202A, 0x202D, 0x2066, 0x2067, 0x2069, 0x061C):
        with pytest.raises(InvalidInput):
            owner_zones.save_zone(
                session, tenant_id, None, _typed(fee=chr(mark) + "2,50"), ""
            )
    assert owner_zones.zones(session, tenant_id) == []


def test_two_ways_to_write_an_umlaut_are_one_name(session, tenant_id):
    """A file from a Mac spells an umlaut as a letter and two dots, the tablet
    as one sign. Both are one name, and a typed name is kept as one sign."""
    two_signs, one_sign = "Su\u0308d", "Süd"
    assert owner_zones.save_zone(session, tenant_id, None, _typed(name=two_signs), "")
    (zone,) = owner_zones.zones(session, tenant_id)
    assert zone.name == one_sign
    # As the import would have written it:
    session.execute(
        update(DeliveryZone).where(DeliveryZone.id == zone.id).values(name=two_signs)
    )
    session.commit()
    for name in (one_sign, "SÜD", two_signs.upper()):
        with pytest.raises(InvalidInput) as error:
            owner_zones.save_zone(
                session, tenant_id, None, _typed(name=name, postal_codes="76534"), ""
            )
        assert "Namen gibt es schon" in _say(error)
    # Sent back as it is stored, the name stays as it is.
    assert _change(session, tenant_id, zone, name=two_signs, fee="3")
    session.refresh(zone)
    assert zone.name == two_signs


def test_names_stored_twice_in_another_case_do_not_block_a_fee(session, tenant_id):
    """The import matches names exactly, so "Nord" and "nord" can both exist.
    Each still takes a change that leaves its name alone."""
    first = _new(session, tenant_id, name="Nord")
    second = _new(session, tenant_id, name="West", postal_codes="76534")
    session.execute(
        update(DeliveryZone).where(DeliveryZone.id == second.id).values(name="nord")
    )
    session.commit()
    assert _change(session, tenant_id, first, name="Nord", fee="3")
    assert _change(
        session, tenant_id, second, name="nord", postal_codes="76534", fee="4"
    )
    assert check_delivery(session, tenant_id, "76534").fee_cents == 400
    # A rename onto the other's name is refused as ever.
    with pytest.raises(InvalidInput) as error:
        _change(session, tenant_id, second, name="NORD", postal_codes="76534", fee="4")
    assert "Namen gibt es schon" in _say(error)


def test_a_form_typed_against_a_state_that_is_gone_gets_the_conflict(
    session, tenant_id
):
    """Whatever else is wrong with it: its fields were checked against values
    the owner never saw, so the fresh form is the answer."""
    zone = _new(session, tenant_id)
    shown = owner_zones.token(zone)
    assert _change(session, tenant_id, zone, fee="3")
    for changes in (
        {"fee": "viel"},
        {"postal_codes": "7653"},
        {"eta": "999"},
        {"name": ""},
    ):
        with pytest.raises(Conflict) as error:
            _change(session, tenant_id, zone, seen=shown, **changes)
        assert "gerade geändert" in _say(error)

    # A fee the import wrote above the bound, lowered on another device: the
    # first device sends the old fee back and is told of the change, not of a
    # bound on a value it did not touch.
    session.execute(
        update(DeliveryZone).where(DeliveryZone.id == zone.id).values(fee_cents=8000)
    )
    session.commit()
    shown = owner_zones.token(owner_zones.zone(session, tenant_id, zone.id))
    assert _change(session, tenant_id, zone, fee="3", eta="45")
    with pytest.raises(Conflict):
        _change(session, tenant_id, zone, seen=shown, fee="80,00", eta="50")
    assert check_delivery(session, tenant_id, "76530").fee_cents == 300


def test_a_name_is_kept_with_single_spaces(session, tenant_id):
    """A space copied from a page is often not the plain one."""
    assert owner_zones.save_zone(
        session,
        tenant_id,
        None,
        _typed(name=" Zone\xa0\xa0 1\t"),
        "",
    )
    assert [zone.name for zone in owner_zones.zones(session, tenant_id)] == ["Zone 1"]
    with pytest.raises(InvalidInput) as error:
        owner_zones.save_zone(
            session,
            tenant_id,
            None,
            _typed(name="zone  1", postal_codes="76534"),
            "",
        )
    assert "Namen gibt es schon" in _say(error)


def test_invisible_signs_in_a_number_are_not_the_owners_typing_error(
    session, tenant_id
):
    """Copied from a page or a file, a number often carries a zero-width
    space or a soft hyphen that nobody sees: the refusal would repeat a
    postal code that looks right."""
    zone = _new(
        session,
        tenant_id,
        postal_codes="76530\u200b, 765\xad32\ufeff",
        fee="\u200b2,50",
        min_order="15\u200b",
        eta="\ufeff40",
    )
    assert zone.postal_codes == ["76530", "76532"]
    assert (zone.fee_cents, zone.min_order_cents, zone.eta_minutes) == (250, 1500, 40)


def test_every_fault_of_a_zone_is_named_at_once_by_its_field(session, tenant_id):
    """All faults of one save, each by its field (#821, #829-3): the owner
    corrects them in one go instead of one per save."""
    typed = _typed(name="", postal_codes="abc", fee="2.5x", min_order="", eta="500")
    with pytest.raises(FieldErrors) as refused:
        owner_zones.save_zone(session, tenant_id, None, typed, "")
    assert refused.value.faults == {
        "name": owner_zones.NO_NAME,
        "postal_codes": owner_zones.NO_CODE.format(code="abc"),
        "fee": owner_zones.NO_AMOUNT.format(field="die Lieferpauschale"),
        "min_order": owner_zones.NO_AMOUNT.format(field="den Mindestbestellwert"),
        "eta": owner_zones.NO_MINUTES,
    }
    assert refused.value.say == owner_zones.NO_NAME
    assert owner_zones.zones(session, tenant_id) == []


def test_a_taken_name_and_a_taken_code_are_named_together(session, tenant_id):
    _new(session, tenant_id)
    with pytest.raises(FieldErrors) as refused:
        owner_zones.save_zone(
            session, tenant_id, None, _typed(name="zone 1", postal_codes="76532"), ""
        )
    assert set(refused.value.faults) == {"name", "postal_codes"}
    assert "Zone 1" in refused.value.faults["postal_codes"]


@pytest.mark.parametrize(
    "name",
    [
        # Hangul fillers: letters by their category that draw nothing (#821).
        chr(0x3164),
        "Zone" + chr(0x3164),
        chr(0x115F) + "Nord",
        "Nord" + chr(0xFFA0),
        # A variation selector after a letter, a combining grapheme joiner.
        "Nord" + chr(0xFE0F),
        "No" + chr(0x034F) + "rd",
    ],
)
def test_a_name_with_a_sign_that_draws_nothing_is_refused(session, tenant_id, name):
    with pytest.raises(InvalidInput) as refused:
        _new(session, tenant_id, name=name)
    assert _say(refused) == owner_zones.NAME_UNREADABLE
    assert owner_zones.zones(session, tenant_id) == []
