"""gui: the floor plan editor's page and JSON routes (T-3.11, docs/06 §3 "Raumplan")."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.domain.reservations import floor_plan
from api.main import app
from api.models import AuditLog, FloorArea, Tenant
from api.schemas.floor_plan import NewArea
from api.tests.conftest import p95_ms
from scripts.seed import seed

HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(engine):
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        client.cookies.set("csrf_token", "testtoken")
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _tenant(db, name="Testbetrieb") -> uuid.UUID:
    tenant = Tenant(name=name, timezone="Europe/Berlin")
    db.add(tenant)
    db.commit()
    return tenant.id


@pytest.fixture
def tenant_id(db) -> uuid.UUID:
    return _tenant(db)


def _create(client, name="Gastraum", **fields) -> dict:
    body = {"name": name, "kind": "inside", "width_cm": 1000, "depth_cm": 700, **fields}
    res = client.post("/gui/raumplan/bereiche", json=body, headers=HX)
    assert res.status_code == 200, res.text
    return res.json()["area"]


def _table(name="T1", x=200) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "shape": "rect",
        "seats": 6,
        "width_cm": 160,
        "depth_cm": 80,
        "x_cm": x,
        "y_cm": 300,
        "rotation": 0,
        "end_seat": "left",
    }


def _body(room: dict, **changes) -> dict:
    keys = ("name", "kind", "shape", "width_cm", "depth_cm", "wall_cm")
    area = {key: room[key] for key in keys}
    area |= {
        "tables": room["tables"],
        "fixtures": room["fixtures"],
        "combinations": room["combinations"],
    }
    return {"revision": room["revision"], "area": area | changes}


def _put(client, room: dict, headers=HX, **changes):
    return client.put(
        f"/gui/raumplan/bereiche/{room['id']}",
        content=json.dumps(_body(room, **changes)),
        headers={**headers, "Content-Type": "application/json"},
    )


def test_the_page_loads_its_scripts_and_issues_the_csrf_cookie(client, tenant_id):
    client.cookies.clear()
    res = client.get("/gui/raumplan")

    assert res.status_code == 200
    # One module script; it imports the geometry itself (docs/22).
    assert '<script type="module" src="/gui/static/raumplan.js"></script>' in res.text
    assert "/gui/static/raumplan-geometry.js" not in res.text
    assert "/gui/static/raumplan.css" in res.text
    # The proxy's CSP refuses inline style attributes and inline scripts.
    assert "style=" not in res.text
    assert "<script>" not in res.text
    assert res.cookies.get("csrf_token")


def test_the_editor_modules_reach_the_browser_as_javascript(client, tenant_id):
    """A browser runs a module script only under a JavaScript MIME type, unlike a
    classic script, and follows the import by its relative URL (docs/22)."""
    for name in ("raumplan.js", "raumplan-geometry.js"):
        res = client.get(f"/gui/static/{name}")
        assert res.status_code == 200
        assert res.headers["content-type"].split(";")[0] in (
            "text/javascript",
            "application/javascript",
        )
    assert 'from "./raumplan-geometry.js"' in client.get("/gui/static/raumplan.js").text


def test_the_operations_header_links_to_the_floor_plan(client, db):
    seed(db, tenant_name="Testbetrieb", timezone="Europe/Berlin")

    res = client.get("/gui")

    assert res.status_code == 200
    assert 'href="/gui/raumplan"' in res.text


def test_a_new_room_comes_back_in_the_plan(client, tenant_id):
    assert client.get("/gui/raumplan/plan").json() == {"ok": True, "areas": []}

    room = _create(client)

    plan = client.get("/gui/raumplan/plan")
    assert plan.headers["cache-control"] == "no-store"
    assert plan.json()["areas"] == [room]


def test_a_save_stores_the_room_and_returns_the_new_revision(client, tenant_id, db):
    room = _create(client)
    table = _table()

    res = _put(client, room, tables=[table])

    assert res.status_code == 200
    saved = res.json()["area"]
    assert saved["revision"] == 1
    assert saved["tables"] == [table]
    entry = db.scalars(
        select(AuditLog).where(AuditLog.action == floor_plan.ACTION_SAVED)
    ).one()
    assert entry.actor == "gui:tablet"


@pytest.mark.parametrize(
    "headers",
    [{}, {"HX-Request": "true"}, {"HX-Request": "true", "X-CSRF-Token": "other"}],
    ids=["plain", "no-token", "wrong-token"],
)
def test_writes_without_the_pages_token_change_nothing(client, tenant_id, db, headers):
    body = {"name": "Gastraum", "kind": "inside", "width_cm": 1000, "depth_cm": 700}
    res = client.post("/gui/raumplan/bereiche", json=body, headers=headers)
    assert res.status_code == 403

    room = _create(client)
    res = _put(client, room, headers=headers, tables=[_table()])
    assert res.status_code == 403
    db.expire_all()
    assert db.get(FloorArea, uuid.UUID(room["id"])).revision == 0


def test_an_older_revision_gets_409_and_the_room_as_it_stands(client, tenant_id):
    room = _create(client)
    first = _put(client, room, tables=[_table("T1")]).json()["area"]

    res = _put(client, room, tables=[_table("T2")])

    assert res.status_code == 409
    assert res.json()["area"] == first
    assert "anderes Gerät" in res.json()["say"]


@pytest.mark.parametrize(
    "changes",
    [
        {"width_cm": 50},
        {"tables": [{**_table(), "seats": 0}]},
        {"tables": [{**_table(), "x_cm": 20.5}]},
        {"fixtures": [{"kind": "window", "id": "f1"}]},
        {"extra": True},
    ],
    ids=["tiny-room", "no-seat", "half-centimetre", "unknown-fixture", "extra-field"],
)
def test_a_malformed_room_is_refused_with_a_sentence(client, tenant_id, changes):
    room = _create(client)

    res = _put(client, room, **changes)

    assert res.status_code == 422
    assert (
        res.json()["say"]
        == "Der Plan enthält einen ungültigen Wert und ist nicht gespeichert."
    )
    assert client.get("/gui/raumplan/plan").json()["areas"] == [room]


def test_a_rule_between_fields_is_refused_with_its_own_sentence(client, tenant_id):
    room = _create(client)

    res = _put(client, room, tables=[_table("T1"), _table("t1", x=600)])

    assert res.status_code == 422
    assert res.json()["say"].startswith("Den Tischnamen t1 gibt es schon.")


def test_a_body_that_is_no_json_is_refused(client, tenant_id):
    room = _create(client)
    res = client.put(
        f"/gui/raumplan/bereiche/{room['id']}",
        content=b"{not json",
        headers={**HX, "Content-Type": "application/json"},
    )
    assert res.status_code == 422


def test_another_restaurants_room_is_not_found(client, tenant_id, db):
    other = _tenant(db, "Anderer Betrieb")
    theirs = floor_plan.create_area(
        db, other, NewArea(name="Saal", kind="inside", width_cm=800, depth_cm=600)
    )

    res = _put(client, theirs, tables=[_table()])

    assert res.status_code == 404
    assert [a["id"] for a in client.get("/gui/raumplan/plan").json()["areas"]] == []
    db.expire_all()
    assert db.get(FloorArea, uuid.UUID(theirs["id"])).revision == 0


def test_a_duplicate_room_name_is_refused(client, tenant_id, db):
    _create(client)
    res = client.post(
        "/gui/raumplan/bereiche",
        json={"name": "GASTRAUM", "kind": "terrace", "width_cm": 600, "depth_cm": 400},
        headers=HX,
    )
    assert res.status_code == 422
    assert "gibt es schon" in res.json()["say"]
    assert db.scalar(select(func.count()).select_from(FloorArea)) == 1


def test_without_a_restaurant_the_page_says_so(client):
    assert client.get("/gui/raumplan").status_code == 503
    assert client.get("/gui/raumplan/plan").status_code == 503


@pytest.mark.latency
def test_a_save_stays_within_the_budget(client, tenant_id):
    room = _create(client)
    tables = [_table(f"T{i}", x=100 + (i % 8) * 110) for i in range(1, 9)]
    state = {"room": _put(client, room, tables=tables).json()["area"]}

    def once():
        current = state["room"]
        moved = [
            {**t, "y_cm": 300 if t["y_cm"] == 400 else 400} for t in current["tables"]
        ]
        res = _put(client, current, tables=moved)
        assert res.status_code == 200
        state["room"] = res.json()["area"]

    assert p95_ms(once) < 300
