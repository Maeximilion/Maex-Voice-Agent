"""domain/reservations/floor_plan.py: the plan the team draws (T-3.11, docs/06 §3)."""

import threading
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.reservations import floor_plan
from api.domain.reservations.floor_plan import StaleRevision
from api.models import AuditLog, DiningTable, TableCombination, Tenant
from api.schemas.floor_plan import NewArea, SaveArea


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _tenant(session, name="Testbetrieb") -> uuid.UUID:
    tenant = Tenant(name=name, timezone="Europe/Berlin")
    session.add(tenant)
    session.commit()
    return tenant.id


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session)


def _room(session, tenant_id, name="Gastraum", kind="inside", w=1000, d=700) -> dict:
    return floor_plan.create_area(
        session,
        tenant_id,
        NewArea(name=name, kind=kind, width_cm=w, depth_cm=d),
    )


def _table(name="T1", x=200, y=200, **fields) -> dict:
    return {
        "id": str(fields.pop("id", uuid.uuid4())),
        "name": name,
        "shape": "rect",
        "seats": 4,
        "width_cm": 120,
        "depth_cm": 80,
        "x_cm": x,
        "y_cm": y,
        "rotation": 0,
        "end_seat": "left",
        **fields,
    }


FIXTURES = [
    {
        "kind": "counter",
        "id": "f1",
        "name": "Theke",
        "x_cm": 880,
        "y_cm": 450,
        "width_cm": 80,
        "depth_cm": 260,
        "rotation": 0,
    },
    {
        "kind": "door",
        "id": "f2",
        "name": "Eingang",
        "side": "bottom",
        "offset_cm": 150,
        "width_cm": 110,
    },
    {
        "kind": "wall",
        "id": "f3",
        "name": "Trennwand",
        "x_cm": 800,
        "y_cm": 170,
        "length_cm": 200,
        "angle": 90,
        "thickness_cm": 20,
    },
    {
        "kind": "label",
        "id": "f4",
        "text": "Fensterseite",
        "x_cm": 200,
        "y_cm": 55,
        "size": "normal",
        "rotation": 270,
    },
]


def _save(session, tenant_id, room: dict, **changes) -> dict:
    area = {
        key: room[key]
        for key in ("name", "kind", "shape", "width_cm", "depth_cm", "wall_cm")
    }
    area |= {
        "tables": room["tables"],
        "fixtures": room["fixtures"],
        "combinations": room["combinations"],
    }
    area |= changes
    request = SaveArea.model_validate({"revision": room["revision"], "area": area})
    return floor_plan.save_area(session, tenant_id, uuid.UUID(room["id"]), request)


def _audits(session, action) -> list[AuditLog]:
    return session.scalars(
        select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    ).all()


def test_a_saved_room_comes_back_as_it_was_sent(session, tenant_id):
    room = _room(session, tenant_id)
    # T2's id sorts first: the audit names a pair in name order, not id order.
    one = _table("T1", id=uuid.UUID(int=2**64 + 2))
    two = _table("T2", x=500, id=uuid.UUID(int=2**64 + 1))
    saved = _save(
        session,
        tenant_id,
        room,
        tables=[one, two],
        fixtures=FIXTURES,
        combinations=[[two["id"], one["id"]]],
    )

    assert saved["revision"] == 1
    assert [t["name"] for t in saved["tables"]] == ["T1", "T2"]
    assert saved["fixtures"] == FIXTURES
    assert saved["combinations"] == [sorted([one["id"], two["id"]])]
    assert floor_plan.plan(session, tenant_id) == [saved]
    entry = _audits(session, floor_plan.ACTION_SAVED)[-1]
    assert entry.payload["tables_added"] == ["T1", "T2"]
    assert entry.payload["combinations_added"] == [["T1", "T2"]]
    assert entry.payload["fixtures"] == [0, 4]


def test_each_room_keeps_its_own_plan(session, tenant_id):
    inside = _room(session, tenant_id)
    terrace = _room(session, tenant_id, name="Terrasse", kind="terrace", w=800, d=500)
    _save(session, tenant_id, inside, tables=[_table("T1")])
    _save(session, tenant_id, terrace, tables=[_table("T2", x=300)])

    rooms = floor_plan.plan(session, tenant_id)
    assert [(r["name"], r["kind"]) for r in rooms] == [
        ("Gastraum", "inside"),
        ("Terrasse", "terrace"),
    ]
    assert [[t["name"] for t in r["tables"]] for r in rooms] == [["T1"], ["T2"]]


def test_a_save_on_an_older_revision_is_refused_with_the_room_as_it_stands(
    session, tenant_id
):
    room = _room(session, tenant_id)
    first = _save(session, tenant_id, room, tables=[_table("T1")])

    with pytest.raises(StaleRevision) as refused:
        _save(session, tenant_id, room, tables=[_table("T9")])

    assert refused.value.status == 409
    assert refused.value.current == first
    assert [t["name"] for t in floor_plan.plan(session, tenant_id)[0]["tables"]] == [
        "T1"
    ]


def test_the_same_save_twice_is_no_conflict(session, tenant_id):
    room = _room(session, tenant_id)
    table = _table("T1")
    first = _save(session, tenant_id, room, tables=[table])

    again = _save(session, tenant_id, room, tables=[table])

    assert again == first
    assert len(_audits(session, floor_plan.ACTION_SAVED)) == 1


def test_a_save_without_a_change_moves_nothing(session, tenant_id):
    room = _room(session, tenant_id)
    first = _save(session, tenant_id, room, tables=[_table("T1")])

    same = _save(session, tenant_id, first)

    assert same["revision"] == 1
    assert len(_audits(session, floor_plan.ACTION_SAVED)) == 1


def test_a_table_name_is_unique_across_rooms_whatever_its_case(session, tenant_id):
    inside = _room(session, tenant_id)
    terrace = _room(session, tenant_id, name="Terrasse", kind="terrace")
    saved = _save(session, tenant_id, inside, tables=[_table("T5")])

    with pytest.raises(InvalidInput) as refused:
        _save(session, tenant_id, terrace, tables=[_table("t5")])

    assert refused.value.say == (
        "Den Tischnamen t5 gibt es schon. Bitte einen anderen wählen."
    )
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, saved, tables=[_table("T1"), _table("T1", x=500)])


def test_a_room_name_is_unique(session, tenant_id):
    _room(session, tenant_id)
    with pytest.raises(InvalidInput):
        _room(session, tenant_id, name="gastraum")
    other = _room(session, tenant_id, name="Nebenraum")
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, other, name="GASTRAUM")


@pytest.mark.parametrize(
    ("table", "fits"),
    [
        # 120 x 80 at x 50: half its width reaches past the left wall.
        (_table(x=50, y=200), False),
        # Turned, it is 80 wide and fits.
        (_table(x=50, y=200, rotation=90), True),
        (_table(x=60, y=680), False),
        (_table(x=940, y=660), True),
    ],
)
def test_a_table_stays_inside_its_room(session, tenant_id, table, fits):
    room = _room(session, tenant_id)
    if fits:
        assert _save(session, tenant_id, room, tables=[table])["revision"] == 1
        return
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, room, tables=[table])


def test_a_smaller_room_cannot_leave_a_table_outside(session, tenant_id):
    room = _room(session, tenant_id)
    saved = _save(session, tenant_id, room, tables=[_table("T1", x=900, y=200)])
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, saved, width_cm=600)


@pytest.mark.parametrize(
    "fixture",
    [
        {**FIXTURES[1], "offset_cm": 20},
        {**FIXTURES[2], "length_cm": 500},
        {**FIXTURES[3], "x_cm": 1200},
        {**FIXTURES[0], "rotation": 90, "x_cm": 100},
    ],
    ids=["door-off-wall", "wall-through-wall", "label-outside", "counter-outside"],
)
def test_a_fixture_stays_inside_its_room(session, tenant_id, fixture):
    room = _room(session, tenant_id)
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, room, fixtures=[fixture])


def test_a_square_room_has_equal_sides_and_a_round_table_one_size(session, tenant_id):
    room = _room(session, tenant_id)
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, room, shape="square")
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, room, tables=[_table(shape="round", depth_cm=100)])
    square = _save(session, tenant_id, room, shape="square", depth_cm=1000)
    assert (square["shape"], square["depth_cm"]) == ("square", 1000)


def test_a_combination_names_two_tables_of_this_room(session, tenant_id):
    room = _room(session, tenant_id)
    one = _table("T1")
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, room, tables=[one], combinations=[[one["id"]] * 2])
    with pytest.raises(InvalidInput):
        _save(
            session,
            tenant_id,
            room,
            tables=[one],
            combinations=[[one["id"], str(uuid.uuid4())]],
        )


def test_a_removed_table_is_kept_and_an_undo_brings_it_back(session, tenant_id):
    room = _room(session, tenant_id)
    one, two = _table("T1"), _table("T2", x=500)
    full = _save(
        session,
        tenant_id,
        room,
        tables=[one, two],
        combinations=[[one["id"], two["id"]]],
    )

    without = _save(session, tenant_id, full, tables=[one], combinations=[])

    assert [t["name"] for t in without["tables"]] == ["T1"]
    removed = session.get(DiningTable, uuid.UUID(two["id"]))
    assert removed.deleted_at is not None
    assert session.scalar(select(func.count()).select_from(TableCombination)) == 0
    assert _audits(session, floor_plan.ACTION_SAVED)[-1].payload["tables_removed"] == [
        "T2"
    ]

    back = _save(
        session,
        tenant_id,
        without,
        tables=[one, two],
        combinations=[[one["id"], two["id"]]],
    )

    assert back["tables"] == full["tables"]
    assert back["combinations"] == full["combinations"]
    session.refresh(removed)
    assert removed.deleted_at is None


def test_a_removed_tables_name_is_free_for_a_new_table(session, tenant_id):
    room = _room(session, tenant_id)
    old = _table("T1")
    saved = _save(session, tenant_id, room, tables=[old])
    new = _table("T1", x=500)

    swapped = _save(session, tenant_id, saved, tables=[new])

    assert [t["id"] for t in swapped["tables"]] == [new["id"]]


def test_a_changed_table_is_in_the_audit_log_with_before_and_after(session, tenant_id):
    room = _room(session, tenant_id)
    table = _table("T1")
    saved = _save(session, tenant_id, room, tables=[table])

    _save(session, tenant_id, saved, tables=[{**table, "seats": 6, "x_cm": 300}])

    payload = _audits(session, floor_plan.ACTION_SAVED)[-1].payload
    assert payload["revision"] == 2
    assert payload["tables_changed"] == {"T1": {"seats": [4, 6], "x_cm": [200, 300]}}


def test_another_restaurants_room_and_tables_stay_out_of_reach(session, tenant_id):
    ours = _room(session, tenant_id)
    other_tenant = _tenant(session, "Anderer Betrieb")
    theirs = _room(session, other_tenant)
    their_table = _table("T1")
    _save(session, other_tenant, theirs, tables=[their_table])

    assert [r["id"] for r in floor_plan.plan(session, tenant_id)] == [ours["id"]]
    with pytest.raises(NotFound):
        _save(session, tenant_id, theirs, tables=[])
    with pytest.raises(InvalidInput):
        _save(session, tenant_id, ours, tables=[{**their_table, "name": "T7"}])

    stored = session.get(DiningTable, uuid.UUID(their_table["id"]))
    session.refresh(stored)
    assert (stored.tenant_id, stored.name, stored.deleted_at) == (
        other_tenant,
        "T1",
        None,
    )
    # The other restaurant's T1 does not take the name away from ours.
    assert _save(session, tenant_id, ours, tables=[_table("T1")])["revision"] == 1


def test_a_table_cannot_move_to_another_room(session, tenant_id):
    inside = _room(session, tenant_id)
    terrace = _room(session, tenant_id, name="Terrasse", kind="terrace")
    table = _table("T1")
    _save(session, tenant_id, inside, tables=[table])

    with pytest.raises(InvalidInput):
        _save(session, tenant_id, terrace, tables=[table])


def test_a_new_room_is_logged_and_starts_square_when_its_sides_are_equal(
    session, tenant_id
):
    room = _room(session, tenant_id, name="Saal", w=900, d=900)

    assert (room["shape"], room["revision"], room["tables"]) == ("square", 0, [])
    entry = _audits(session, floor_plan.ACTION_AREA_CREATED)[-1]
    assert entry.payload["name"] == "Saal"
    assert entry.entity_id == uuid.UUID(room["id"])


def test_two_tablets_cannot_both_take_one_table_name(migrated_db_url, tenant_id):
    """Deterministic: the second save waits for the restaurant's lock, which the
    first holds until its commit, so it always sees the first T5."""
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as setup:
            inside = _room(setup, tenant_id)
            terrace = _room(setup, tenant_id, name="Terrasse", kind="terrace")
        outcome: dict = {}

        def second_tablet():
            with Session(engine) as s:
                try:
                    _save(s, tenant_id, terrace, tables=[_table("T5")])
                    outcome["saved"] = True
                except InvalidInput as exc:
                    outcome["refused"] = exc.say

        with Session(engine) as first:
            first.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
                {"key": f"floor_plan:{tenant_id}"},
            )
            thread = threading.Thread(target=second_tablet)
            thread.start()
            thread.join(0.3)
            assert thread.is_alive(), "the second save waits for the lock"
            _save(first, tenant_id, inside, tables=[_table("T5")])
        thread.join(10)
        assert outcome == {"refused": floor_plan.SAY_TABLE_TAKEN.format(name="T5")}
    finally:
        engine.dispose()


def test_two_names_swapped_in_one_save_are_refused_and_nothing_is_stored(
    session, tenant_id
):
    room = _room(session, tenant_id)
    one, two = _table("T1"), _table("T2", x=500)
    saved = _save(session, tenant_id, room, tables=[one, two])

    with pytest.raises(InvalidInput) as refused:
        _save(
            session,
            tenant_id,
            saved,
            tables=[{**one, "name": "T2"}, {**two, "name": "T1"}],
        )

    assert refused.value.status == 422
    assert floor_plan.plan(session, tenant_id) == [saved]
