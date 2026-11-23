"""The drift rule of the public tablet demo (#892, docs/06 §11).

The demo answers the real pages' requests in the browser, by the manifest
`scripts/demo/tablet_demo_routes.json`. These tests fail when the view gains a
route, a template or the TypeScript names a target, or a route moves behind the
owner's PIN, without the manifest saying how the demo answers it."""

from fastapi.routing import APIRoute

from api.gui.access import router as access_router
from api.gui.router import router as view_router
from api.gui.session import require_admin
from scripts.demo import tablet_demo_routes as routes
from scripts.demo.tablet_demo_routes import Target

ANSWERS = {"page", "fragment", "state", "locked", "absent"}


def view_routes() -> dict[tuple[str, str], APIRoute]:
    """Every route of the operations view and its sign-in; the simulator
    console (`gui/dev.py`, ENV=dev only) is no part of the demo."""
    found = {}
    for route in [*view_router.routes, *access_router.routes]:
        if not isinstance(route, APIRoute):
            continue
        for method in (route.methods or set()) - {"HEAD"}:
            found[method, route.path] = route
    return found


def manifest_routes() -> dict[tuple[str, str], str]:
    return {(r["method"], r["path"]): r["answer"] for r in routes.manifest()["routes"]}


def behind_pin(route: APIRoute) -> bool:
    return any(d.call is require_admin for d in route.dependant.dependencies)


def test_every_target_of_the_templates_and_the_typescript_is_answered():
    targets = routes.template_targets() + routes.script_targets()

    assert len(targets) > 80  # the scan reads the real files
    assert routes.unanswered(targets) == []


def test_every_route_of_the_view_is_in_the_manifest_and_nothing_else():
    view, listed = set(view_routes()), set(manifest_routes())

    assert view - listed == set(), "routes the demo does not know how to answer"
    assert listed - view == set(), "manifest routes the view no longer has"
    assert set(manifest_routes().values()) <= ANSWERS


def test_a_route_behind_the_pin_is_shown_or_locked_except_the_floor_plan():
    """Maxi, 2026-11-20: PIN parts are visible but not usable; the floor plan
    is the one PIN page the demo opens (plan comment on #892)."""
    answers = manifest_routes()
    wrong = [
        (method, path, answers[method, path])
        for (method, path), route in view_routes().items()
        if behind_pin(route)
        and not path.startswith("/gui/raumplan")
        and answers[method, path] not in ("page", "locked")
    ]

    assert wrong == []
    assert answers["PUT", "/gui/raumplan/bereiche/{area_id}"] == "state"


def test_a_new_hx_route_in_a_template_is_reported(tmp_path):
    (tmp_path / "neu.html").write_text(
        '<button hx-post="/gui/bestellungen/{{ o.id }}/verschenken">x</button>\n'
        '<button hx-post="/gui/bestellungen/{{ o.id }}/passt">ok</button>',
        encoding="utf-8",
    )

    problems = routes.unanswered(routes.template_targets(tmp_path))

    assert problems == ["neu.html:1: POST /gui/bestellungen/{}/verschenken"]


def test_a_jinja_expression_with_quotes_is_read_as_one_part(tmp_path):
    (tmp_path / "kopf.html").write_text(
        "<button hx-post=\"/gui/kopfzeile/{{ key }}/{{ 'aus' if on else 'an' }}\">"
        '</button>\n<form method="post" action="/gui/inhaber/schliessen"></form>',
        encoding="utf-8",
    )

    targets = routes.template_targets(tmp_path)

    assert [(t.method, t.path) for t in targets] == [
        ("POST", "/gui/kopfzeile/{}/{}"),
        ("POST", "/gui/inhaber/schliessen"),
    ]
    assert routes.unanswered(targets) == []


def test_a_computed_target_must_be_listed_and_a_sign_in_link_is_refused(tmp_path):
    (tmp_path / "zone.html").write_text(
        '<form hx-post="{{ action }}"></form>\n<a href="/gui/anmelden">Anmelden</a>',
        encoding="utf-8",
    )

    problems = routes.unanswered(routes.template_targets(tmp_path))

    assert problems == [
        "zone.html:2: GET /gui/anmelden is not in the demo",
        "zone.html:1: POST {}",
    ]


def test_typescript_targets_carry_their_method_and_computed_part(tmp_path):
    (tmp_path / "plan.ts").write_text(
        'send("PUT", "/gui/raumplan/bereiche/" + room.id, body);\n'
        'fetch("/gui/raumplan/plan");\n'
        "const s = new EventSource(`/gui/events`);\n"
        'window.htmx.ajax("GET", "/gui/fragments/neu", {});\n',
        encoding="utf-8",
    )

    targets = routes.script_targets(tmp_path)

    assert [(t.method, t.path) for t in targets] == [
        ("PUT", "/gui/raumplan/bereiche/{}"),
        ("GET", "/gui/raumplan/plan"),
        ("GET", "/gui/events"),
        ("GET", "/gui/fragments/neu"),
    ]
    assert routes.unanswered(targets) == ["plan.ts:4: GET /gui/fragments/neu"]


def test_a_computed_part_matches_one_segment_only():
    assert routes.answer(Target("POST", "/gui/rueckrufe/{}/erledigt", "t")) == "state"
    assert routes.answer(Target("POST", "/gui/rueckrufe/a/b/erledigt", "t")) is None
    assert routes.answer(Target("GET", "/gui/rueckrufe/{}/erledigt", "t")) is None
