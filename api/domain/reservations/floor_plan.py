"""The floor plan the team draws on the tablet (T-3.11, #496, docs/06 §3 "Raumplan").

Rooms and terraces with their tables, which tables combine, and the walls,
doors, counters and labels that only help to find one's way. The browser moves
things locally and sends the whole room after each finished step (a drop, a
field, an undo); this module checks it and stores it. Nothing here books or
counts a table: the agent keeps to the seats in `capacity` (D28), and T-3.12
seats a reservation on these tables.

One room is one document with a revision. A save names the revision it started
from: when another tablet saved in between, the save is refused with the room as
it now stands, so nobody overwrites what they have not seen. A table that leaves
the plan is soft deleted, so a later reservation on it keeps its name.
"""

import math
import uuid
from typing import Any, NoReturn

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import Conflict, InvalidInput, NotFound
from api.domain.status.config import ACTOR_TABLET
from api.models import AuditLog, DiningTable, FloorArea, TableCombination
from api.schemas.floor_plan import Area, NewArea, SaveArea

ACTION_AREA_CREATED = "floor_plan.area_created"
ACTION_SAVED = "floor_plan.saved"
# Bound against a runaway tablet, not a limit agreed with a restaurant (assumption).
MAX_AREAS = 20
# Furniture may touch its room's edge by rounding, never cross it.
TOLERANCE_CM = 1.0
AREA_FIELDS = ("name", "kind", "shape", "width_cm", "depth_cm", "wall_cm")
TABLE_FIELDS = (
    "name",
    "shape",
    "seats",
    "width_cm",
    "depth_cm",
    "x_cm",
    "y_cm",
    "rotation",
    "end_seat",
)
SAY_NAME_TAKEN = "Den Raumnamen {name} gibt es schon. Bitte einen anderen wählen."
SAY_TABLE_TAKEN = "Den Tischnamen {name} gibt es schon. Bitte einen anderen wählen."


class StaleRevision(Conflict):
    """Another tablet saved this room since the editor loaded it."""

    def __init__(self, current: dict) -> None:
        super().__init__(
            "floor area changed since its revision",
            say=(
                "Ein anderes Gerät hat diesen Raum inzwischen geändert. Der "
                "aktuelle Stand ist geladen, die letzte Änderung hier ist nicht "
                "gespeichert."
            ),
            status=409,
        )
        self.current = current


def plan(session: Session, tenant_id: uuid.UUID) -> list[dict]:
    """Every room and terrace of the restaurant, oldest first."""
    areas = session.scalars(
        select(FloorArea)
        .where(FloorArea.tenant_id == tenant_id)
        .order_by(FloorArea.created_at, FloorArea.id)
    ).all()
    return [_document(session, area) for area in areas]


def create_area(
    session: Session, tenant_id: uuid.UUID, new: NewArea, actor: str = ACTOR_TABLET
) -> dict:
    """A new, empty room or terrace. A room of equal sides starts as a square."""
    _lock(session, tenant_id)
    areas = session.scalars(
        select(FloorArea).where(FloorArea.tenant_id == tenant_id)
    ).all()
    if len(areas) >= MAX_AREAS:
        raise InvalidInput(
            "too many floor areas",
            say=f"Es gehen höchstens {MAX_AREAS} Räume und Terrassen.",
            status=422,
        )
    if any(a.name.casefold() == new.name.casefold() for a in areas):
        raise InvalidInput(
            "floor area name taken",
            say=SAY_NAME_TAKEN.format(name=new.name),
            status=422,
        )
    area = FloorArea(
        tenant_id=tenant_id,
        name=new.name,
        kind=new.kind,
        shape="square" if new.width_cm == new.depth_cm else "rectangle",
        width_cm=new.width_cm,
        depth_cm=new.depth_cm,
        wall_cm=20,
        fixtures=[],
        revision=0,
    )
    session.add(area)
    session.flush()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=ACTION_AREA_CREATED,
            entity="floor_area",
            entity_id=area.id,
            payload={field: getattr(area, field) for field in AREA_FIELDS},
        )
    )
    session.commit()
    return _document(session, area)


def save_area(
    session: Session,
    tenant_id: uuid.UUID,
    area_id: uuid.UUID,
    request: SaveArea,
    actor: str = ACTOR_TABLET,
) -> dict:
    """Store the whole room as the tablet sends it, or nothing.

    Raises `StaleRevision` when another save came first, `InvalidInput` when the
    room breaks a rule, `NotFound` for a room of another restaurant."""
    _lock(session, tenant_id)
    area = session.scalars(
        select(FloorArea)
        .where(FloorArea.id == area_id, FloorArea.tenant_id == tenant_id)
        .with_for_update()
    ).one_or_none()
    if area is None:
        raise NotFound(
            "floor area not found", say="Diesen Raum gibt es nicht mehr.", status=404
        )
    current = _document(session, area)
    wanted = request.area
    if request.revision != area.revision:
        # The same room twice (a retry after a lost answer) is no conflict.
        if _content(current) == _content(_from_request(wanted)):
            session.rollback()
            return current
        raise StaleRevision(current)

    rows = _check(session, tenant_id, area, wanted)
    changes = _changes(current, _from_request(wanted))
    if not changes:
        session.rollback()
        return current
    try:
        _apply(session, tenant_id, area, wanted, rows)
        area.revision += 1
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor,
                action=ACTION_SAVED,
                entity="floor_area",
                entity_id=area.id,
                payload={"revision": area.revision, **changes},
            )
        )
        session.commit()
    except IntegrityError as exc:
        # A rule the checks above missed (two renames swapping names): nothing stored.
        session.rollback()
        raise InvalidInput(
            "floor plan refused by the database",
            say="Der Plan passt so nicht und ist nicht gespeichert. Bitte die letzte "
            "Änderung prüfen.",
            status=422,
        ) from exc
    session.refresh(area)
    return _document(session, area)


def _lock(session: Session, tenant_id: uuid.UUID) -> None:
    """One writer per restaurant: table names are unique across its rooms."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"),
        {"key": f"floor_plan:{tenant_id}"},
    )


def _check(
    session: Session, tenant_id: uuid.UUID, area: FloorArea, wanted: Area
) -> dict[uuid.UUID, DiningTable]:
    """Every rule between fields; returns the stored rows of the wanted tables."""
    if wanted.shape == "square" and wanted.width_cm != wanted.depth_cm:
        _refuse("square with two sides", "Ein Quadrat hat zwei gleich lange Seiten.")
    taken = session.scalars(
        select(FloorArea.name).where(
            FloorArea.tenant_id == tenant_id, FloorArea.id != area.id
        )
    ).all()
    if wanted.name.casefold() in {name.casefold() for name in taken}:
        _refuse("floor area name taken", SAY_NAME_TAKEN.format(name=wanted.name))

    ids = [t.id for t in wanted.tables]
    if len(set(ids)) != len(ids):
        _refuse("table twice in one room", "Ein Tisch steht doppelt im Plan.")
    rows = {
        row.id: row
        for row in session.scalars(
            select(DiningTable).where(DiningTable.id.in_(ids))
        ).all()
    }
    if any(r.tenant_id != tenant_id or r.area_id != area.id for r in rows.values()):
        # Another room's table (or another restaurant's id): never moved or shown.
        _refuse(
            "table of another area",
            "Ein Tisch gehört zu einem anderen Raum. Bitte die Seite neu laden.",
        )
    live_elsewhere = {
        name.casefold()
        for name in session.scalars(
            select(DiningTable.name).where(
                DiningTable.tenant_id == tenant_id,
                DiningTable.area_id != area.id,
                DiningTable.deleted_at.is_(None),
            )
        ).all()
    }
    seen: set[str] = set()
    for table in wanted.tables:
        key = table.name.casefold()
        if key in seen or key in live_elsewhere:
            _refuse("table name taken", SAY_TABLE_TAKEN.format(name=table.name))
        seen.add(key)
        if table.shape == "round" and table.width_cm != table.depth_cm:
            _refuse("round table not round", f"Tisch {table.name} ist rund: ein Maß.")
        _inside(wanted, table.name, table.x_cm, table.y_cm, *_turned(table))

    pairs = [tuple(sorted(pair)) for pair in wanted.combinations]
    if any(a == b or a not in ids or b not in ids for a, b in pairs):
        _refuse(
            "combination outside the room",
            "Kombinieren geht nur mit Tischen dieses Raums.",
        )

    names = [f.id for f in wanted.fixtures]
    if len(set(names)) != len(names):
        _refuse("fixture twice", "Ein Objekt steht doppelt im Plan.")
    for fixture in wanted.fixtures:
        if fixture.kind == "counter":
            _inside(wanted, fixture.name, fixture.x_cm, fixture.y_cm, *_turned(fixture))
        elif fixture.kind == "label":
            _inside(wanted, fixture.text, fixture.x_cm, fixture.y_cm, 0, 0)
        elif fixture.kind == "wall":
            _wall_inside(wanted, fixture)
        else:
            extent = (
                wanted.width_cm
                if fixture.side in ("top", "bottom")
                else wanted.depth_cm
            )
            half = fixture.width_cm / 2
            if (
                not half - TOLERANCE_CM
                <= fixture.offset_cm
                <= extent - half + TOLERANCE_CM
            ):
                _refuse("door off its wall", f"{fixture.name} liegt nicht an der Wand.")
    return rows


def _turned(piece: Any) -> tuple[float, float]:
    """Width and depth as drawn: a quarter turn swaps them."""
    if piece.rotation in (90, 270):
        return piece.depth_cm, piece.width_cm
    return piece.width_cm, piece.depth_cm


def _inside(area: Area, name: str, x: float, y: float, w: float, d: float) -> None:
    if not (
        w / 2 - TOLERANCE_CM <= x <= area.width_cm - w / 2 + TOLERANCE_CM
        and d / 2 - TOLERANCE_CM <= y <= area.depth_cm - d / 2 + TOLERANCE_CM
    ):
        _refuse("piece outside its room", f"{name} liegt außerhalb des Raums.")


def _wall_inside(area: Area, wall: Any) -> None:
    dx = math.cos(math.radians(wall.angle)) * wall.length_cm / 2
    dy = math.sin(math.radians(wall.angle)) * wall.length_cm / 2
    for x, y in ((wall.x_cm - dx, wall.y_cm - dy), (wall.x_cm + dx, wall.y_cm + dy)):
        _inside(area, wall.name, x, y, 0, 0)


def _refuse(message: str, say: str) -> NoReturn:
    raise InvalidInput(message, say=say, status=422)


def _apply(
    session: Session,
    tenant_id: uuid.UUID,
    area: FloorArea,
    wanted: Area,
    rows: dict[uuid.UUID, DiningTable],
) -> None:
    for field in AREA_FIELDS:
        setattr(area, field, getattr(wanted, field))
    area.fixtures = [f.model_dump(mode="json") for f in wanted.fixtures]

    kept = {t.id for t in wanted.tables}
    now = clock.utcnow()
    live = session.scalars(
        select(DiningTable).where(
            DiningTable.area_id == area.id, DiningTable.deleted_at.is_(None)
        )
    ).all()
    for row in live:
        if row.id not in kept:
            row.deleted_at = now
    # Names leave before they arrive: a table renamed to a removed one's name.
    session.flush()
    for table in wanted.tables:
        stored_row = rows.get(table.id)
        if stored_row is None:
            stored_row = DiningTable(id=table.id, tenant_id=tenant_id, area_id=area.id)
            session.add(stored_row)
        for field in TABLE_FIELDS:
            setattr(stored_row, field, getattr(table, field))
        # An undo brings a removed table back under its old id.
        stored_row.deleted_at = None
    session.flush()

    area_tables = select(DiningTable.id).where(DiningTable.area_id == area.id)
    stored = {
        (c.table_a_id, c.table_b_id): c
        for c in session.scalars(
            select(TableCombination).where(
                or_(
                    TableCombination.table_a_id.in_(area_tables),
                    TableCombination.table_b_id.in_(area_tables),
                )
            )
        ).all()
    }
    wanted_pairs = {tuple(sorted(pair)) for pair in wanted.combinations}
    for pair, combination in stored.items():
        if pair not in wanted_pairs:
            session.delete(combination)
    for a, b in wanted_pairs - stored.keys():
        session.add(TableCombination(tenant_id=tenant_id, table_a_id=a, table_b_id=b))


def _document(session: Session, area: FloorArea) -> dict:
    """The room as the editor reads it; ids as text, like the browser sends them."""
    tables = session.scalars(
        select(DiningTable)
        .where(DiningTable.area_id == area.id, DiningTable.deleted_at.is_(None))
        .order_by(DiningTable.created_at, DiningTable.name, DiningTable.id)
    ).all()
    ids = [t.id for t in tables]
    pairs = session.execute(
        select(TableCombination.table_a_id, TableCombination.table_b_id).where(
            TableCombination.table_a_id.in_(ids), TableCombination.table_b_id.in_(ids)
        )
    ).all()
    return {
        "id": str(area.id),
        "revision": area.revision,
        **{field: getattr(area, field) for field in AREA_FIELDS},
        "tables": [
            {"id": str(t.id), **{field: getattr(t, field) for field in TABLE_FIELDS}}
            for t in tables
        ],
        "fixtures": list(area.fixtures),
        "combinations": sorted([str(a), str(b)] for a, b in pairs),
    }


def _from_request(wanted: Area) -> dict:
    """The wanted room in the shape of `_document`, to compare the two."""
    dumped = wanted.model_dump(mode="json")
    dumped["combinations"] = sorted(sorted(pair) for pair in dumped["combinations"])
    return dumped


def _content(document: dict) -> dict:
    """What a room holds, without its id, revision and the order of its lists."""
    return {
        **{field: document[field] for field in AREA_FIELDS},
        "tables": sorted(document["tables"], key=lambda t: t["id"]),
        "fixtures": sorted(document["fixtures"], key=lambda f: f["id"]),
        "combinations": sorted(document["combinations"]),
    }


def _changes(before: dict, after: dict) -> dict:
    """The audit entry: what changed, before and after, by the names on the plan."""
    changes: dict[str, Any] = {}
    area = {f: [before[f], after[f]] for f in AREA_FIELDS if before[f] != after[f]}
    if area:
        changes["area"] = area
    old = {t["id"]: t for t in before["tables"]}
    new = {t["id"]: t for t in after["tables"]}
    if added := [new[i]["name"] for i in new.keys() - old.keys()]:
        changes["tables_added"] = sorted(added)
    if removed := [old[i]["name"] for i in old.keys() - new.keys()]:
        changes["tables_removed"] = sorted(removed)
    changed = {}
    for i in old.keys() & new.keys():
        fields = {
            f: [old[i][f], new[i][f]] for f in TABLE_FIELDS if old[i][f] != new[i][f]
        }
        if fields:
            changed[old[i]["name"]] = fields
    if changed:
        changes["tables_changed"] = changed
    names = {
        **{i: t["name"] for i, t in old.items()},
        **{i: t["name"] for i, t in new.items()},
    }
    pairs_before = {tuple(p) for p in before["combinations"]}
    pairs_after = {tuple(p) for p in after["combinations"]}
    if added_pairs := pairs_after - pairs_before:
        changes["combinations_added"] = sorted(
            [names[a], names[b]] for a, b in added_pairs
        )
    if removed_pairs := pairs_before - pairs_after:
        changes["combinations_removed"] = sorted(
            [names[a], names[b]] for a, b in removed_pairs
        )
    if sorted(before["fixtures"], key=lambda f: f["id"]) != sorted(
        after["fixtures"], key=lambda f: f["id"]
    ):
        changes["fixtures"] = [len(before["fixtures"]), len(after["fixtures"])]
    return changes
