"""Render the public tablet demo from the real operations view (#892, task B1).

    python -m scripts.demo.tablet_demo_build [--out api/site/tablet-demo]

Needs `DATABASE_URL` naming a PostgreSQL server (the local stack's is enough).
The build makes a scratch database of its own there, migrates it, seeds the
invented restaurant of `tablet_demo_data.py`, and asks the real application for
every page and answer `tablet_demo_routes.json` marks as rendered. The answers
are written as plain files; the scratch database is dropped at the end. No
other database is read: the application's session factory is bound to the
scratch one for the whole run.

The output is generated, not committed (`.gitignore`): the public site serves it
at `/tablet-demo` (`api/site/app.py`), the fake backend of B2 answers the
requests of the pages from it. What changes in it:

- `/gui/static/` becomes `/static/`, where the site serves the same files;
- a link to a rendered page leads to its file (`/gui/kennzahlen` to
  `/tablet-demo/kennzahlen/`); every `hx-*` target stays the real route, the
  fake backend answers it by the manifest;
- every page gets `tablet-demo.css`, the notice for phone-sized screens and the
  fake backend (`demo-backend.js`, compiled from `api/gui/ts/`).

`captures.json` lists what was rendered: method, route, address, form body and
file, so the fake backend finds the file for a request. `state.json` holds the
data the four board fragments were rendered from (header, orders, callbacks,
reservations of the day, the sheet of done callbacks), so the fake backend
starts from the very state the real view showed and renders the same markup
from it (B2, `api/gui/ts/demo-board.ts`).
"""

import argparse
import json
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update

from api import db
from api.config import settings
from api.core.time import business_day
from api.models import MenuItem, Order
from scripts.demo import tablet_demo_data as data
from scripts.demo.tablet_demo_routes import MANIFEST, manifest, pattern

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "api" / "site" / "tablet-demo"
BASE = "/tablet-demo"
# Not a secret: the demo has no server that checks it. Fixed, so a rebuild
# writes the same pages.
CSRF = "tablet-demo"
NOTICE = (
    '<p class="demo-notice" role="note">Die Tablet-Demo ist für Tablet und '
    "Computer gebaut, nicht für das Smartphone. Auf einem größeren Bildschirm "
    "lässt sie sich so bedienen wie im Restaurant.</p>"
)
STYLESHEET = '<link rel="stylesheet" href="/static/tablet-demo.css">'
# In the head, so it runs before the board's own module and replaces the
# browser's request objects before the first request.
BACKEND = '<script type="module" src="/static/demo-backend.js"></script>'
BODY = re.compile(r"<body\b[^>]*>")
HREF = re.compile(r'\bhref="(/gui(?:/[^"?#]*)?)"')
CALL_ROW = re.compile(
    r"""hx-post="/gui/anrufe/anruf" hx-vals='\{"anruf": "([0-9a-f-]+)"\}'"""
)
ZONE = ZoneInfo(data.TIMEZONE)
# A phone number in E.164 as the view prints it.
PHONE = re.compile(r"\+[1-9][0-9]{7,14}")
# Table sizes in cm by shape and seats; a round table has one size.
SIZES = {
    ("rect", 2): (80, 80),
    ("rect", 4): (120, 80),
    ("rect", 6): (180, 80),
    ("round", 4): (100, 100),
}


# The fragments whose data the fake backend renders itself (B2): what the view
# handed the template is written to `state.json`.
RECORDED = {
    "fragments/kopfzeile.html": "header",
    "fragments/bestellungen.html": "orders",
    "fragments/rueckrufe.html": "callbacks",
    "fragments/heute.html": "today",
    "fragments/rueckruf_verlauf.html": "history",
}
# The table the invented reservation of the example call sits at (a table of the
# first room `drive` makes).
EXAMPLE_TABLE = "T3"


def _plain(value: Any) -> Any:
    """What a template context holds, as JSON: the settings row as the one fact
    the header reads from it, the tenant as nothing, a dataclass as its fields."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if getattr(value, "call_mode", None) is not None:
        return {"call_mode": value.call_mode}
    if getattr(value, "timezone", None) is not None:
        return None
    return str(value)


@contextmanager
def recording() -> Iterator[dict[str, Any]]:
    """The data of the fragments of `RECORDED`, as the view hands it to the
    template, for the last answer of each (a failed answer is not kept)."""
    from api.gui import router as view

    seen: dict[str, Any] = {}
    original = view.templates.TemplateResponse

    def record(
        request: Any, name: str, context: Any = None, *rest: Any, **more: Any
    ) -> Any:
        response = original(request, name, context, *rest, **more)
        if name in RECORDED and response.status_code == 200 and context is not None:
            plain = json.loads(json.dumps(context, default=_plain))
            # Starlette adds the request; the header reads nothing of the tenant.
            seen[RECORDED[name]] = {
                k: v for k, v in plain.items() if k not in ("request", "tenant")
            }
        return response

    with mock.patch.object(view.templates, "TemplateResponse", record):
        yield seen


def board_state(seen: dict[str, Any]) -> dict[str, Any]:
    """`state.json`: the recorded data plus what the example call needs: the
    menu (invented, from the seed), numbers of the example range for new callers,
    the words of the modes and callback reasons, the demo clock's start."""
    from api.gui.router import CALLBACK_LABELS, MODE_LABELS

    missing = [name for name in RECORDED.values() if name not in seen]
    if missing:
        raise SystemExit(f"the view did not render: {', '.join(missing)}")
    menu = [
        {
            "category": category,
            "number": number,
            "name": name,
            "cents": int(price.replace(",", "")),
        }
        for category, dishes in data.DISHES.items()
        for number, name, price in dishes
    ]
    return {
        **seen,
        "menu": menu,
        "sold_out": data.SOLD_OUT,
        "phones": [data.example_number(n) for n in range(200, 210)],
        "mode_labels": {k: list(v) for k, v in MODE_LABELS.items()},
        "callback_labels": {k: list(v) for k, v in CALLBACK_LABELS.items()},
        "clock": "12:00",
        "table": EXAMPLE_TABLE,
    }


@dataclass(frozen=True)
class Capture:
    method: str
    path: str  # the route of the manifest
    url: str  # the address asked for
    body: dict[str, str] | None
    file: str  # relative to the output folder


def demo_now() -> datetime:
    """Noon of the business day running now: the board shows today, and no
    order or reservation of the demo falls across the 05:00 day boundary."""
    return datetime.combine(
        business_day(tz_name=data.TIMEZONE), time(12, 0), tzinfo=ZONE
    ).astimezone(UTC)


def file_for(url: str, json_answer: bool = False) -> str:
    """Where the answer to `url` is written: a folder with `index.html`, so a
    page is reached by its plain address, or a `.json` file."""
    rest = url.split("?", 1)[0].removeprefix("/gui").strip("/")
    if json_answer:
        return f"{rest}.json"
    return f"{rest}/index.html" if rest else "index.html"


def page_paths() -> set[str]:
    return {
        r["path"].rstrip("/") for r in manifest()["routes"] if r["answer"] == "page"
    }


def rewrite(html: str, whole_page: bool) -> str:
    """The real page as the demo serves it (see the module text)."""
    pages = page_paths()

    def link(m: re.Match[str]) -> str:
        path = m.group(1).rstrip("/") or "/gui"
        if path not in pages:
            return m.group(0)
        return f'href="{BASE}/{file_for(path).removesuffix("index.html")}"'

    html = html.replace('"/gui/static/', '"/static/')
    html = HREF.sub(link, html)
    if whole_page:
        html = html.replace("</head>", f"{STYLESHEET}\n{BACKEND}\n</head>", 1)
        html = BODY.sub(lambda m: f"{m.group(0)}\n{NOTICE}", html, count=1)
    return html


@contextmanager
def frozen_clock(now: datetime) -> Iterator[None]:
    """`utcnow` returns `now` in every loaded module, also where it was imported
    by name, so the times on the pages follow the demo's clock."""
    import api.main  # noqa: F401  (loads every module the view uses)
    from api.core import time as clock

    original = clock.utcnow
    holders = [
        m for m in list(sys.modules.values()) if getattr(m, "utcnow", None) is original
    ]
    with ExitStack() as stack:
        for module in holders:
            stack.enter_context(mock.patch.object(module, "utcnow", lambda: now))
        yield


@contextmanager
def signed_in() -> Iterator[TestClient]:
    """The real application as a signed-in device with the owner's PIN open, as
    the tests of the view run it (`api/tests/conftest.py`)."""
    from api.core.auth import require_gui_proxy
    from api.domain.access.sessions import Access
    from api.gui.session import require_admin, require_gui_session
    from api.main import app

    device = uuid.UUID(int=0)
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        require_gui_proxy: lambda: None,
        require_gui_session: lambda: Access(device, locked=False, admin=False),
        require_admin: lambda: Access(device, locked=False, admin=True),
    }
    app.dependency_overrides.update(overrides)
    try:
        client = TestClient(app)
        client.cookies.set("csrf_token", CSRF)
        client.headers.update({"HX-Request": "true", "X-CSRF-Token": CSRF})
        yield client
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


def _ok(response: Any, what: str) -> Any:
    if response.status_code != 200:
        raise SystemExit(f"{what}: {response.status_code} {response.text[:300]}")
    return response


def _room(
    client: TestClient,
    name: str,
    kind: str,
    size: tuple[int, int],
    tables: list[tuple[str, int, int, int, str]],
) -> None:
    """A room of the floor plan, made through the editor's own two requests."""
    width, depth = size
    made = _ok(
        client.post(
            "/gui/raumplan/bereiche",
            content=json.dumps(
                {"name": name, "kind": kind, "width_cm": width, "depth_cm": depth}
            ),
            headers={"Content-Type": "application/json"},
        ),
        f"room {name}",
    ).json()["area"]
    area = {k: v for k, v in made.items() if k not in ("id", "revision")}
    area["tables"] = [
        {
            "id": str(uuid.uuid4()),
            "name": table,
            "shape": shape,
            "seats": seats,
            "width_cm": SIZES[shape, seats][0],
            "depth_cm": SIZES[shape, seats][1],
            "x_cm": x,
            "y_cm": y,
        }
        for table, seats, x, y, shape in tables
    ]
    _ok(
        client.put(
            f"/gui/raumplan/bereiche/{made['id']}",
            content=json.dumps({"revision": made["revision"], "area": area}),
            headers={"Content-Type": "application/json"},
        ),
        f"tables of {name}",
    )


def drive(client: TestClient, seeded: data.Seeded) -> None:
    """Taps of the team before the visitor arrives, through the real routes: a
    dish marked off, an order typed into the register, a callback done, two rooms."""
    with db.SessionLocal() as session:
        dish = session.scalars(
            select(MenuItem.id).where(
                MenuItem.tenant_id == seeded.tenant_id, MenuItem.number == data.SOLD_OUT
            )
        ).one()
    _ok(client.post(f"/gui/gericht-aus/{dish}/aus"), "dish off")
    # "Passt" on two orders; their slips then lie at the register, which the
    # print bridge reports in operation and the demo has none for.
    released = [seeded.orders["auswahl"], seeded.orders["kasse"]]
    for order in released:
        _ok(client.post(f"/gui/bestellungen/{order}/passt"), "passt")
    with db.SessionLocal() as session:
        session.execute(
            update(Order).where(Order.id.in_(released)).values(handover_state="sent")
        )
        session.commit()
    board = _ok(client.get("/gui/fragments/bestellungen"), "orders").text
    order = seeded.orders["kasse"]
    mark = re.search(rf'"/gui/bestellungen/{order}/in-kasse/([^"]+)"', board)
    if mark is None:
        raise SystemExit("the order for the register has no button")
    _ok(client.post(f"/gui/bestellungen/{order}/in-kasse/{mark.group(1)}"), "register")
    _ok(
        client.post(f"/gui/rueckrufe/{seeded.callbacks['erledigt']}/erledigt"),
        "callback done",
    )
    _room(
        client,
        "Gastraum",
        "inside",
        (1000, 700),
        [
            ("T1", 2, 150, 150, "rect"),
            ("T2", 2, 350, 150, "rect"),
            ("T3", 4, 600, 150, "rect"),
            ("T4", 4, 150, 450, "round"),
            ("T5", 6, 450, 450, "rect"),
            ("T6", 4, 800, 450, "round"),
        ],
    )
    _room(
        client,
        "Terrasse",
        "terrace",
        (800, 400),
        [("T10", 4, 150, 150, "round"), ("T11", 4, 450, 150, "round")],
    )


def captures(client: TestClient) -> list[Capture]:
    """What the manifest marks as rendered: every page, fragment and state
    answer of a GET route, the routes with an id once per id the pages offer,
    and one call detail per call of the list."""
    routes = [
        r
        for r in manifest()["routes"]
        if r["answer"] in ("page", "fragment", "state")
        and r["path"] not in ("/gui/", "/gui/events")
    ]
    plain = [r for r in routes if r["method"] == "GET" and "{" not in r["path"]]
    found = [Capture("GET", r["path"], r["path"], None, "") for r in plain]
    seen = "".join(
        client.get(url).text
        for url in ("/gui", "/gui/fragments/bestellungen", "/gui/fragments/kopfzeile")
    )
    for route in routes:
        if route["method"] != "GET" or "{" not in route["path"]:
            continue
        route_pattern = pattern(route["path"])
        urls = sorted(
            {
                m
                for m in re.findall(r'"(/gui/[^"]+)"', seen)
                if route_pattern.fullmatch(m)
            }
        )
        found += [Capture("GET", route["path"], url, None, "") for url in urls]
    calls = _ok(client.get("/gui/anrufe"), "calls").text
    found += [
        Capture("POST", "/gui/anrufe/anruf", "/gui/anrufe/anruf", {"anruf": call}, "")
        for call in sorted(set(CALL_ROW.findall(calls)))
    ]
    return found


def render(client: TestClient, wanted: list[Capture], out: Path) -> list[Capture]:
    done = []
    for capture in wanted:
        response = _ok(
            client.request(capture.method, capture.url, data=capture.body),
            f"{capture.method} {capture.url}",
        )
        json_answer = response.headers["content-type"].startswith("application/json")
        name = file_for(capture.url, json_answer)
        if capture.body:
            name = (
                f"{name.removesuffix('index.html')}{capture.body['anruf']}/index.html"
            )
        text = response.text
        if not json_answer:
            text = rewrite(text, whole_page=text.lstrip().startswith("<!DOCTYPE"))
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        done.append(
            Capture(capture.method, capture.path, capture.url, capture.body, name)
        )
    return done


def leaks(out: Path, real: list[str]) -> list[str]:
    """Phone numbers outside the example range, and values of this machine's
    settings, in the rendered files: each one would be published."""
    found = []
    for file in sorted(out.rglob("*")):
        if not file.is_file():
            continue
        text = file.read_text(encoding="utf-8")
        name = file.relative_to(out).as_posix()
        found += [
            f"{name}: {number}"
            for number in sorted(set(PHONE.findall(text)))
            if not number.startswith(data.EXAMPLE_PREFIX)
        ]
        found += [f"{name}: a value of the settings" for value in real if value in text]
    return found


def build(out: Path, now: datetime | None = None) -> list[Capture]:
    """Seed and render into `out`, on the database `api.db.SessionLocal` is
    bound to; it must hold no restaurant (`tablet_demo_data.fill`). Fails, with
    the folder removed, when a rendered file holds anything of `leaks`."""
    now = now or demo_now()
    # The view offers the numbers of the environment (the team number sheet)
    # and builds links from its domain: a developer's .env must not reach the page.
    real = [
        v.strip()
        for v in (
            settings.team_phone,
            *settings.transfer_targets.split(","),
            settings.agent_domain,
        )
        if len(v.strip()) >= 6
    ]
    with (
        frozen_clock(now),
        mock.patch.object(settings, "team_phone", data.example_number(900)),
        mock.patch.object(settings, "transfer_targets", data.example_number(901)),
        mock.patch.object(settings, "agent_domain", ""),
    ):
        with db.SessionLocal() as session:
            seeded = data.fill(session, now)
        with signed_in() as client, recording() as seen:
            drive(client, seeded)
            wanted = captures(client)
            if out.exists():
                shutil.rmtree(out)
            out.mkdir(parents=True)
            done = render(client, wanted, out)
    (out / "state.json").write_text(
        json.dumps(board_state(seen), indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out / "captures.json").write_text(
        json.dumps([asdict(c) for c in done], indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(MANIFEST, out / "routes.json")
    found = leaks(out, real)
    if found:
        shutil.rmtree(out)
        raise SystemExit("not published, the demo would show:\n" + "\n".join(found))
    return done


@contextmanager
def scratch_database() -> Iterator[None]:
    """A migrated scratch database on the server of DATABASE_URL, bound to the
    application's session factory while the build runs, then dropped."""
    from evals.scratch_db import create_scratch_db, drop_scratch_db, migrate

    url = create_scratch_db(prefix="maex_tablet_demo")
    engine = create_engine(url)
    try:
        migrate(url)
        db.SessionLocal.configure(bind=engine)
        yield
    finally:
        db.SessionLocal.configure(bind=db.engine)
        engine.dispose()
        drop_scratch_db(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    with scratch_database():
        done = build(args.out)
    print(f"{len(done)} files in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
