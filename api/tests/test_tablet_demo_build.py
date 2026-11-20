"""The build of the public tablet demo (#892, B1): the real view renders the
invented restaurant into plain files, nothing of this machine's settings or a
real number reaches them, and the public site serves them."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api import db
from api.config import settings
from api.site import app as site_app
from api.site.config import SiteSettings
from scripts.demo import tablet_demo_build as build
from scripts.demo import tablet_demo_data as data
from scripts.demo.tablet_demo_routes import manifest
from scripts.seed import seed

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "api" / "gui" / "static"


def node_check(script: str, compiled: str, out: Path) -> None:
    """Run one of the node checks of the fake backend against the built folder."""
    node = shutil.which("node")
    if not node:
        # The api image carries no node; CI's runner does, and must run this.
        if os.environ.get("CI"):
            pytest.fail("the tablet demo checks need node")
        pytest.skip("the tablet demo checks need node")
    run = subprocess.run(
        [node, str(Path(__file__).with_name(script)), str(STATIC / compiled), str(out)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    assert run.stdout.strip() == "ok"


@pytest.fixture
def scratch(migrated_db_url):
    """The application's session factory on the test's database, as the build's
    own scratch database binds it."""
    engine = create_engine(migrated_db_url)
    db.SessionLocal.configure(bind=engine)
    try:
        yield engine
    finally:
        db.SessionLocal.configure(bind=db.engine)
        engine.dispose()


@pytest.fixture
def built(scratch, tmp_path, monkeypatch):
    # What a developer's .env could hold: it must not reach the pages.
    monkeypatch.setattr(settings, "team_phone", "+4915112345678")
    monkeypatch.setattr(settings, "transfer_targets", "+4915187654321")
    out = tmp_path / "tablet-demo"
    return out, build.build(out)


def test_every_page_and_fragment_of_the_manifest_is_rendered(built):
    out, done = built
    rendered = {c.path for c in done}

    wanted = {
        r["path"]
        for r in manifest()["routes"]
        if r["answer"] in ("page", "fragment") and r["path"] != "/gui/"
    }
    assert wanted <= rendered
    assert all((out / c.file).is_file() for c in done)
    listed = json.loads((out / "captures.json").read_text(encoding="utf-8"))
    assert len(listed) == len(done)
    assert (out / "routes.json").is_file()
    areas = json.loads((out / "raumplan" / "plan.json").read_text())["areas"]
    assert [a["name"] for a in areas] == ["Gastraum", "Terrasse"]


def test_pages_carry_the_notice_and_keep_the_real_targets(built):
    out, _ = built
    board = (out / "index.html").read_text(encoding="utf-8")

    assert 'class="demo-notice"' in board
    assert "nicht für das Smartphone" in board
    assert 'href="/static/tablet-demo.css"' in board
    assert 'href="/tablet-demo/kennzahlen/"' in board
    assert 'hx-post="/gui/kopfzeile/ki-pausieren"' in board
    assert "Beispiel-Restaurant" in board
    for file in out.rglob("*.html"):
        assert "/gui/static/" not in file.read_text(encoding="utf-8"), file


def test_the_invented_restaurant_is_what_the_board_shows(built):
    out, _ = built
    board = (out / "index.html").read_text(encoding="utf-8")
    menu = (out / "menue" / "index.html").read_text(encoding="utf-8")

    assert board.count('class="card order') == 3  # the fourth is in the register
    assert "Zettel nicht gedruckt" in board
    assert "Die Pizza kam kalt an." in board
    assert "Familie Krüger" in board
    for number, name, _price in (d for ds in data.DISHES.values() for d in ds):
        assert name in menu, number


def test_no_number_outside_the_example_range_and_no_setting_is_published(built):
    out, _ = built

    assert build.leaks(out, ["+4915112345678", "+4915187654321"]) == []
    sheet = (out / "teamnummer" / "index.html").read_text(encoding="utf-8")
    assert data.example_number(900) in sheet


def test_leaks_names_a_foreign_number_and_a_setting(tmp_path):
    (tmp_path / "a.html").write_text(
        "+493023125001 +4972155501999 maex.example.org", encoding="utf-8"
    )

    assert build.leaks(tmp_path, ["maex.example.org"]) == [
        "a.html: +4972155501999",
        "a.html: a value of the settings",
    ]


def test_the_build_refuses_a_database_with_a_restaurant(scratch, tmp_path):
    with Session(scratch) as session:
        seed(session, tenant_name="Echt", timezone="Europe/Berlin")

    with pytest.raises(SystemExit, match="already holds a restaurant"):
        build.build(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_the_example_range_has_a_thousand_numbers():
    assert data.example_number(0) == "+493023125000"
    assert data.example_number(999) == "+493023125999"
    with pytest.raises(ValueError):
        data.example_number(1000)


def test_the_site_serves_the_built_demo_under_its_policy(tmp_path, monkeypatch):
    folder = tmp_path / "tablet-demo"
    monkeypatch.setattr(site_app, "TABLET_DEMO", folder)
    client = TestClient(site_app.create_app(SiteSettings(_env_file=None)))  # type: ignore[call-arg]

    assert client.get("/tablet-demo/").status_code == 404  # not built
    (folder / "kennzahlen").mkdir(parents=True)
    (folder / "kennzahlen" / "index.html").write_text("<p>ok</p>", encoding="utf-8")
    client = TestClient(site_app.create_app(SiteSettings(_env_file=None)))  # type: ignore[call-arg]
    response = client.get("/tablet-demo/kennzahlen/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_every_page_loads_the_fake_backend_before_the_board_script(built):
    out, _ = built
    board = (out / "index.html").read_text(encoding="utf-8")

    backend = '<script type="module" src="/static/demo-backend.js"></script>'
    assert board.count(backend) == 1
    assert board.index(backend) < board.index('src="/static/app.js"')
    assert "<script>" not in board  # no inline script (the site's CSP allows none)
    for file in out.rglob("index.html"):
        text = file.read_text(encoding="utf-8")
        if text.lstrip().startswith("<!DOCTYPE"):
            assert backend in text, file
        else:
            assert backend not in text, f"{file} is a fragment"
    assert (STATIC / "demo-backend.js").is_file()
    assert (STATIC / "demo-board.js").is_file()


def test_the_state_of_the_board_is_recorded_without_tenant_or_request(built):
    out, _ = built
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))

    assert {"header", "orders", "callbacks", "today", "history", "menu"} <= set(state)
    assert "tenant" not in state["header"]
    assert all(
        "request" not in part for part in state.values() if isinstance(part, dict)
    )
    assert len(state["menu"]) == 25
    assert {d["category"] for d in state["menu"]} == set(data.DISHES)
    assert all(p.startswith(data.EXAMPLE_PREFIX) for p in state["phones"])
    assert state["header"]["config"] == {"call_mode": "primary"}
    assert len(state["orders"]["orders"]) == 3
    assert len(state["history"]["callbacks"]) == 1
    assert state["table"] == build.EXAMPLE_TABLE


def test_the_site_serves_the_fake_backend_as_a_script():
    client = TestClient(site_app.create_app(SiteSettings(_env_file=None)))  # type: ignore[call-arg]
    for name in ("demo-backend.js", "demo-board.js"):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
    # The page's own policy: scripts from this origin only, so no network target.
    assert (
        "script-src 'self'"
        in client.get("/static/demo-backend.js").headers["content-security-policy"]
    )


def test_the_board_renders_the_real_fragments_and_follows_the_real_rules(built):
    out, _ = built
    node_check("demo_board.mjs", "demo-board.js", out)


def test_the_fake_backend_answers_every_request_of_the_view_in_the_browser(built):
    out, _ = built
    node_check("demo_backend.mjs", "demo-backend.js", out)


# --- B3: the sheets and the owner's part ---

PIN_PAGES = ("zeiten", "lieferung", "daten", "geraete")
CONTROL = re.compile(r"<(?:button|select|textarea|input)\b[^>]*>")


def test_the_sheets_render_the_real_templates_and_follow_the_real_rules(built):
    out, _ = built
    node_check("demo_sheets.mjs", "demo-board.js", out)


def test_the_owners_pages_are_locked_and_say_so(built):
    out, _ = built
    for page in PIN_PAGES:
        text = (out / page / "index.html").read_text(encoding="utf-8")
        assert 'class="banner warn demo-locked"' in text, page
        assert "Inhaber-PIN verfügbar" in text, page
        # Nothing below the tabs can be pressed or typed into, by mouse or keyboard.
        body = text.split("</nav>", 1)[1]
        for control in CONTROL.findall(body):
            assert "disabled" in control or 'type="hidden"' in control, (page, control)
    # The pages a visitor may use are not locked.
    for page in ("kennzahlen", "anrufe", "menue", "raumplan"):
        text = (out / page / "index.html").read_text(encoding="utf-8")
        assert "demo-locked" not in text, page


def test_example_figures_and_the_example_menu_carry_their_label(built):
    out, _ = built
    for page in ("kennzahlen", "anrufe", "menue", "zeiten", "lieferung"):
        text = (out / page / "index.html").read_text(encoding="utf-8")
        assert build.EXAMPLE_LABEL in text, page
    kennzahlen = (out / "kennzahlen" / "index.html").read_text(encoding="utf-8")
    assert f"Letzte 7 Tage {build.EXAMPLE_BADGE}" in kennzahlen
    assert f"<h2>Verlauf {build.EXAMPLE_BADGE}</h2>" in kennzahlen
    assert "kein Angebot" in (out / "menue" / "index.html").read_text(encoding="utf-8")


def test_every_day_of_the_call_list_and_each_call_is_captured(built):
    out, done = built
    page = (out / "anrufe" / "index.html").read_text(encoding="utf-8")
    days = {c.url for c in done if c.url.startswith("/gui/anrufe?tag=")}
    assert days == {f"/gui/anrufe?tag={d}" for d in build.DAY_OPTION.findall(page)}
    assert len(days) == 14
    # A detail for every row of every day, so no "Details" button is dead.
    shown = set()
    for c in done:
        if c.url.startswith("/gui/anrufe?tag="):
            text = (out / c.file).read_text(encoding="utf-8")
            shown |= set(build.CALL_ROW.findall(text))
    assert shown
    assert shown <= {c.body["anruf"] for c in done if c.body}


def test_the_state_carries_what_the_sheets_need(built):
    out, _ = built
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))

    assert {"team", "hours", "entry"} <= set(state)
    assert all(d["id"] and "groups" in d for d in state["menu"])
    pizza = next(d for d in state["menu"] if d["number"] == "201")
    assert [o["name"] for o in pizza["groups"][0]["options"]] == ["Normal", "Groß"]
    assert pizza["groups"][0]["required"] is True
    assert [d["number"] for d in state["menu"] if d["sold_out"]] == [data.SOLD_OUT]
    assert state["entry"]["choices"]


def test_the_lock_refuses_a_page_without_a_tab_row_and_leaves_hidden_fields():
    with pytest.raises(SystemExit, match="no tab row"):
        build.lock("<main><button>x</button></main>")
    locked = build.lock(
        '<nav class="tabs"></nav><button>a</button><input type="hidden" name="c">'
        '<input name="d"><select name="e"></select>'
    )
    assert (
        locked.count(" disabled aria-disabled") == 3
    )  # the hidden field stays as it is
    assert "demo-locked" in locked


def test_a_variant_of_a_page_gets_a_file_of_its_own():
    assert build.file_for("/gui/anrufe") == "anrufe/index.html"
    assert build.file_for("/gui/anrufe?tag=2026-11-19") == (
        "anrufe/tag-2026-11-19/index.html"
    )
    assert (
        build.file_for("/gui/raumplan/plan", json_answer=True) == "raumplan/plan.json"
    )
