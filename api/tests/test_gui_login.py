"""The restaurant's login on the operations view over HTTP (#570, D34, docs/23 §6),
with the owner's admin PIN and device list (#570 part b).

These tests switch off the signed-in device every other GUI test acts as
(conftest `_gui_signed_in`), so they meet the view as a browser does."""

import re
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core import time as clock
from api.db import get_db
from api.domain.access.admin import AdminRefused
from api.domain.access.sessions import IDLE, stack_tenant_id
from api.domain.access.setup import issue_link, use_link
from api.domain.customers.requests import log_request
from api.gui.router import router as gui_router
from api.gui.session import csrf_matches
from api.gui.sse import _signed_in
from api.main import app
from api.models import (
    AuditLog,
    Call,
    DataSubjectRequest,
    DeliveryZone,
    GuiAccount,
    GuiLoginFailure,
    GuiSession,
    OpeningHours,
    SpecialDay,
    StaffChange,
    Tenant,
)
from scripts.seed import seed

pytestmark = pytest.mark.gui_login

PASSWORD = "Kuechentablet-2026"
PIN = "4711"
ADMIN_PIN = "080815"
HTML = {"Accept": "text/html,application/xhtml+xml"}
HX = {"HX-Request": "true", "X-CSRF-Token": "testtoken"}
PROXY = "p" * 32
# Fixed, not random: every xdist worker must collect the same parameters.
ANY_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    yield engine
    engine.dispose()


def _client(engine, base_url="http://testserver") -> TestClient:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, base_url=base_url, follow_redirects=False)
    client.cookies.set("csrf_token", "testtoken")
    return client


@pytest.fixture
def client(engine):
    try:
        yield _client(engine)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _setup(engine) -> str:
    with Session(engine) as session:
        tenant_id = stack_tenant_id(session)
        assert tenant_id is not None
        code, _ = issue_link(session, tenant_id, "operator:test", clock.utcnow())
    return code


def _set_up_and_sign_in(client, engine) -> None:
    code = _setup(engine)
    answer = client.post(
        "/gui/einrichten",
        data={
            "csrf_token": "testtoken",
            "code": code,
            "passwort": PASSWORD,
            "passwort2": PASSWORD,
            "pin": PIN,
            "pin2": PIN,
            "admin_pin": ADMIN_PIN,
            "admin_pin2": ADMIN_PIN,
        },
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"


def _routes():
    for route in gui_router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", ANY_ID, route.path)
        for method in route.methods or ():
            yield method, path


# --- without a session ---------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), sorted(set(_routes())))
def test_every_route_of_the_view_refuses_without_a_session(client, method, path):
    answer = client.request(method, path, headers=HX)
    assert answer.status_code == 401, path
    assert answer.headers["HX-Redirect"] == "/gui/anmelden"


def test_a_page_load_goes_to_the_sign_in(client):
    for path in (
        "/gui",
        "/gui/",
        "/gui/kennzahlen",
        "/gui/anrufe",
        "/gui/menue",
        "/gui/raumplan",
    ):
        answer = client.get(path, headers=HTML)
        assert answer.status_code == 303, path
        assert answer.headers["location"] == "/gui/anmelden"


def test_the_event_stream_refuses_without_a_session(client):
    assert client.get("/gui/events").status_code == 401


def test_the_sign_in_pages_and_static_files_need_no_session(client):
    for path in ("/gui/anmelden", "/gui/einrichten", "/gui/static/app.css"):
        assert client.get(path, headers=HTML).status_code == 200, path


def test_the_bare_host_name_leads_to_the_view(client, engine):
    """#571: `/` is the way in. Without a session it ends on the front page,
    with one on the board; the answer itself is a redirect and shows nothing."""
    answer = client.get("/", headers=HTML)
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"
    assert answer.text == "" and "set-cookie" not in answer.headers
    assert client.get("/gui/", headers=HTML).headers["location"] == "/gui/anmelden"
    _set_up_and_sign_in(client, engine)
    assert client.get("/gui/", headers=HTML).status_code == 200


def test_the_bare_host_name_takes_no_write_and_opens_no_other_path(client):
    assert client.post("/", headers=HX).status_code == 405
    for path in ("/anmelden", "/static/app.css", "/index.html"):
        assert client.get(path, headers=HTML).status_code == 404, path
    # The way on is fixed: nothing a link carries can send the browser elsewhere.
    answer = client.get("/?next=//example.org&back=https://example.org")
    assert answer.headers["location"] == "/gui/"


def test_the_front_page_shows_the_name_and_sets_only_the_csrf_cookie(client):
    page = client.get("/gui/anmelden", headers=HTML)
    assert "<h1>Testbetrieb</h1>" in page.text
    assert 'autocomplete="current-password"' in page.text
    assert 'data-zeigen="passwort"' in page.text
    cookies = page.headers.get_list("set-cookie")
    assert [c.split("=")[0] for c in cookies] == ["csrf_token"]


def test_a_name_with_markup_is_shown_as_text(client, engine):
    with Session(engine) as session:
        session.execute(update(Tenant).values(name="<script>alert(1)</script>"))
        session.commit()
    page = client.get("/gui/anmelden", headers=HTML)
    assert "<script>alert(1)" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_a_forged_cookie_opens_nothing(client, engine):
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("maex_session", "forged")
    assert client.get("/gui/", headers=HTML).status_code == 303


# --- setup link and sign-in ----------------------------------------------------


def test_the_setup_link_sets_the_password_and_signs_this_device_in(client, engine):
    page = client.get("/gui/einrichten").text
    assert "data-setup-code" in page and 'name="csrf_token" value="testtoken"' in page
    _set_up_and_sign_in(client, engine)
    assert client.get("/gui/", headers=HTML).status_code == 200


def test_setup_refuses_two_different_entries_and_a_spent_link(client, engine):
    code = _setup(engine)
    form = {
        "csrf_token": "testtoken",
        "code": code,
        "passwort": PASSWORD,
        "passwort2": PASSWORD + "x",
        "pin": PIN,
        "pin2": PIN,
        "admin_pin": ADMIN_PIN,
        "admin_pin2": ADMIN_PIN,
    }
    answer = client.post("/gui/einrichten", data=form)
    assert answer.status_code == 422 and "nicht gleich" in answer.text
    # The code stays in the form, so the next try needs no new link.
    assert f'value="{code}"' in answer.text
    assert (
        client.post("/gui/einrichten", data={**form, "passwort2": PASSWORD}).status_code
        == 303
    )
    again = client.post("/gui/einrichten", data={**form, "passwort2": PASSWORD})
    assert again.status_code == 422 and "gilt nicht mehr" in again.text


def test_sign_in_with_the_password_sets_an_httponly_session_cookie(client, engine):
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("csrf_token", "testtoken")
    page = client.get("/gui/anmelden")
    assert page.status_code == 200 and 'type="password"' in page.text

    answer = client.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": PASSWORD}
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"
    session_cookie, device_cookie = answer.headers.get_list("set-cookie")
    assert session_cookie.startswith("maex_session=")
    assert device_cookie.startswith("maex_device=")
    for cookie in (session_cookie, device_cookie):
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert client.get("/gui/", headers=HTML).status_code == 200
    # Signed in, the sign-in page leads to the board.
    assert client.get("/gui/anmelden").headers["location"] == "/gui/"


def test_a_wrong_password_says_so_and_sets_no_session(client, engine):
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("csrf_token", "testtoken")
    answer = client.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": "falsch-falsch"}
    )
    assert answer.status_code == 401 and "Das Passwort stimmt nicht." in answer.text
    assert "maex_session" not in answer.headers.get("set-cookie", "")
    # The refusal comes back on the front page, not on a second, bare one.
    assert "<h1>Testbetrieb</h1>" in answer.text


def test_a_form_post_under_no_referrer_signs_in(client, engine):
    """A browser sends `Origin: null` with a plain form post when the page's
    referrer policy is `no-referrer` (Fetch, "append a request `Origin`
    header"), and Caddy sets that policy on the whole site. The browser still
    says `Sec-Fetch-Site: same-origin`, which no page can set or change. Found
    16.11.2026: the operator's sign-in answered 403 in a real browser."""
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("csrf_token", "testtoken")
    answer = client.post(
        "/gui/anmelden",
        data={"csrf_token": "testtoken", "passwort": PASSWORD},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", ""])
def test_a_null_origin_that_is_not_this_site_stays_refused(client, engine, site):
    """`null` is also what a sandboxed frame and a cross-site redirect send."""
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("csrf_token", "testtoken")
    answer = client.post(
        "/gui/anmelden",
        data={"csrf_token": "testtoken", "passwort": PASSWORD},
        headers={"Origin": "null", "Sec-Fetch-Site": site},
    )
    assert answer.status_code == 403


@pytest.mark.parametrize(
    ("form", "headers"),
    [
        ({"passwort": PASSWORD}, {}),
        ({"passwort": PASSWORD, "csrf_token": "other"}, {}),
        (
            {"passwort": PASSWORD, "csrf_token": "testtoken"},
            {"Origin": "https://evil.example"},
        ),
        ({"passwort": PASSWORD, "csrf_token": "testtoken"}, {"Origin": "null"}),
        (
            {"passwort": PASSWORD, "csrf_token": "testtoken"},
            {"Sec-Fetch-Site": "cross-site"},
        ),
    ],
)
def test_the_sign_in_needs_its_csrf_token_and_this_site(client, engine, form, headers):
    """Login CSRF (docs/23 §6): a foreign page cannot sign the tablet into an
    account of its choosing."""
    _set_up_and_sign_in(client, engine)
    client.cookies.clear()
    client.cookies.set("csrf_token", "testtoken")
    answer = client.post("/gui/anmelden", data=form, headers=headers)
    assert answer.status_code == 403


# --- signed in: writes, idle lock and PIN ---------------------------------------


def _lock(engine) -> None:
    with Session(engine) as session:
        session.execute(
            update(GuiSession).values(
                last_active_at=clock.utcnow() - IDLE - timedelta(seconds=1)
            )
        )
        session.commit()


def test_a_write_from_another_site_is_refused(client, engine):
    _set_up_and_sign_in(client, engine)
    answer = client.post(
        "/gui/kopfzeile/ki-pausieren",
        headers={**HX, "Origin": "https://evil.example"},
    )
    assert answer.status_code == 403
    same = client.post(
        "/gui/kopfzeile/ki-pausieren", headers={**HX, "Origin": "http://testserver"}
    )
    assert same.status_code == 200


def test_a_locked_device_still_shows_the_board_and_asks_the_pin_to_change(
    client, engine
):
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    assert client.get("/gui/", headers=HTML).status_code == 200
    assert client.get("/gui/fragments/heute", headers=HX).status_code == 200

    write = client.post("/gui/kopfzeile/ki-pausieren", headers=HX)
    assert (
        write.status_code == 401 and write.headers["HX-Redirect"] == "/gui/entsperren"
    )

    assert client.get("/gui/entsperren").status_code == 200
    wrong = client.post(
        "/gui/entsperren", data={"csrf_token": "testtoken", "pin": "0000"}
    )
    assert wrong.status_code == 401 and "Noch 4 Versuche" in wrong.text
    right = client.post("/gui/entsperren", data={"csrf_token": "testtoken", "pin": PIN})
    assert right.status_code == 303 and right.headers["location"] == "/gui/"
    assert client.post("/gui/kopfzeile/ki-pausieren", headers=HX).status_code == 200
    # Unlocked, the PIN page leads back to the board.
    assert client.get("/gui/entsperren").headers["location"] == "/gui/"


def test_five_wrong_pins_sign_the_device_out(client, engine):
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    for _ in range(4):
        client.post("/gui/entsperren", data={"csrf_token": "testtoken", "pin": "0000"})
    last = client.post(
        "/gui/entsperren", data={"csrf_token": "testtoken", "pin": "0000"}
    )
    assert last.status_code == 401 and "abgemeldet" in last.text
    assert "<h1>Testbetrieb</h1>" in last.text
    assert client.get("/gui/", headers=HTML).headers["location"] == "/gui/anmelden"
    assert (
        client.post(
            "/gui/entsperren", data={"csrf_token": "testtoken", "pin": PIN}
        ).headers["location"]
        == "/gui/anmelden"
    )


def test_the_pin_form_needs_its_csrf_token(client, engine):
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    assert client.post("/gui/entsperren", data={"pin": PIN}).status_code == 403


FOREIGN = [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]


@pytest.mark.parametrize("headers", FOREIGN)
def test_the_pin_form_refuses_another_site(client, engine, headers):
    """#827 F10: a foreign page cannot unlock the tablet with a PIN it holds."""
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    answer = client.post(
        "/gui/entsperren", data={"csrf_token": "testtoken", "pin": PIN}, headers=headers
    )
    assert answer.status_code == 403
    write = client.post("/gui/kopfzeile/ki-pausieren", headers=HX)
    assert write.headers["HX-Redirect"] == "/gui/entsperren"


@pytest.mark.parametrize(
    ("token", "headers"),
    [(None, {}), ("other", {}), *(("testtoken", h) for h in FOREIGN)],
)
def test_the_setup_form_needs_its_csrf_token_and_this_site(
    client, engine, token, headers
):
    """#827 F10, proved live by the audit: a foreign page cannot use a setup
    link it knows to give the tablet a password of its choosing. The link
    stays unused."""
    code = _setup(engine)
    form = {
        "code": code,
        "passwort": PASSWORD,
        "passwort2": PASSWORD,
        "pin": PIN,
        "pin2": PIN,
        "admin_pin": ADMIN_PIN,
        "admin_pin2": ADMIN_PIN,
    }
    if token is not None:
        form["csrf_token"] = token
    answer = client.post("/gui/einrichten", data=form, headers=headers)
    assert answer.status_code == 403
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GuiSession)) == 0
    form["csrf_token"] = "testtoken"
    assert client.post("/gui/einrichten", data=form).status_code == 303


@pytest.mark.parametrize(
    ("token", "headers"),
    [(None, {}), ("other", {}), *(("testtoken", h) for h in FOREIGN)],
)
def test_closing_the_owner_part_needs_its_csrf_token_and_this_site(
    client, engine, token, headers
):
    """#827 F10: a foreign page cannot end the owner's hour on the tablet."""
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    form = {} if token is None else {"csrf_token": token}
    answer = client.post("/gui/inhaber/schliessen", data=form, headers=headers)
    assert answer.status_code == 403
    assert client.get(DEVICES, headers=HTML).status_code == 200


# --- production: cookie flags, client address -----------------------------------


@pytest.fixture
def production(monkeypatch, engine):
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "agent_api_token", "a" * 32)
    monkeypatch.setattr(settings, "gui_proxy_secret", PROXY)
    client = _client(engine, base_url="https://testserver")
    client.headers["X-Maex-Proxy-Secret"] = PROXY
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_in_production_the_cookie_is_host_bound_and_secure(production, engine):
    _set_up_and_sign_in(production, engine)
    production.cookies.clear()
    production.cookies.set("csrf_token", "testtoken")
    answer = production.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": PASSWORD}
    )
    cookies = answer.headers.get_list("set-cookie")
    assert [c.split("=")[0] for c in cookies] == ["__Host-session", "__Host-device"]
    for cookie in cookies:
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
        assert "Domain" not in cookie
    assert production.get("/gui/", headers=HTML).status_code == 200


def test_in_production_failures_count_for_the_address_caddy_saw(production, engine):
    _set_up_and_sign_in(production, engine)
    # A stranger: no device cookie, so the failure counts per address.
    production.cookies.clear()
    production.cookies.set("csrf_token", "testtoken")
    production.post(
        "/gui/anmelden",
        data={"csrf_token": "testtoken", "passwort": "falsch-falsch"},
        headers={"X-Forwarded-For": "10.0.0.1, 203.0.113.9"},
    )
    with Session(engine) as session:
        assert session.scalars(select(GuiLoginFailure.address)).all() == ["203.0.113.9"]


def test_in_production_the_sign_in_still_needs_the_proxy(production):
    del production.headers["X-Maex-Proxy-Secret"]
    assert production.get("/gui/anmelden").status_code == 403
    assert production.get("/").status_code == 403


def test_the_check_leaves_no_transaction_open_for_the_event_stream(engine):
    """The stream holds its request for hours (router `events`): the session
    check must hand its pooled connection back before it."""
    from starlette.requests import Request

    from api.gui.session import require_gui_session

    with Session(engine) as session:
        tenant_id = stack_tenant_id(session)
        assert tenant_id is not None
        code, _ = issue_link(session, tenant_id, "operator:test", clock.utcnow())
        token = use_link(
            session, tenant_id, code, PASSWORD, PIN, "x", clock.utcnow()
        ).token
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/gui/events",
        "headers": [(b"cookie", f"maex_session={token}".encode())],
    }
    with Session(engine) as session:
        assert require_gui_session(Request(scope), session)
        assert not session.in_transaction()
        with pytest.raises(HTTPException):
            require_gui_session(Request({**scope, "headers": []}), session)
        assert not session.in_transaction()


def test_the_device_cookie_gets_a_known_browser_past_a_guessers_wait(client, engine):
    """Review of #585: someone else's wrong passwords keep the account waiting;
    the browser that signed in before still gets in with the password."""
    _set_up_and_sign_in(client, engine)
    device = client.cookies.get("maex_device")
    assert device
    stranger = _client(engine)
    for _ in range(5):
        stranger.post(
            "/gui/anmelden",
            data={"csrf_token": "testtoken", "passwort": "falsch-falsch"},
        )
    waits = stranger.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": PASSWORD}
    )
    assert waits.status_code == 401 and "Zu viele Versuche" in waits.text

    client.cookies.delete("maex_session")
    back = client.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": PASSWORD}
    )
    assert back.status_code == 303 and back.headers["location"] == "/gui/"


# --- the owner's part of the admin area (#570 part b, D34 points 3 and 6) -------

DEVICES = "/gui/geraete"


def _owner(client, pin=ADMIN_PIN, back=DEVICES):
    return client.post(
        "/gui/inhaber",
        data={"csrf_token": "testtoken", "pin": pin, "weiter": back},
    )


def _second_device(engine):
    other = _client(engine)
    answer = other.post(
        "/gui/anmelden", data={"csrf_token": "testtoken", "passwort": PASSWORD}
    )
    assert answer.status_code == 303
    return other


def _sessions(engine) -> list[GuiSession]:
    with Session(engine) as session:
        return list(session.scalars(select(GuiSession)))


def test_the_device_list_asks_the_admin_pin_first(client, engine):
    _set_up_and_sign_in(client, engine)
    page = client.get(DEVICES, headers=HTML)
    assert page.status_code == 303
    assert page.headers["location"] == "/gui/inhaber?weiter=/gui/geraete"
    form = client.get(page.headers["location"])
    assert form.status_code == 200 and 'name="weiter" value="/gui/geraete"' in form.text

    answer = _owner(client)
    assert answer.status_code == 303 and answer.headers["location"] == DEVICES
    listed = client.get(DEVICES, headers=HTML)
    assert listed.status_code == 200 and "Gerät abmelden" in listed.text
    # Open, the admin PIN page leads straight on.
    assert client.get("/gui/inhaber?weiter=/gui/geraete").headers["location"] == (
        DEVICES
    )


def test_a_write_of_the_owner_without_the_pin_goes_to_the_pin_page(client, engine):
    _set_up_and_sign_in(client, engine)
    session_id = _sessions(engine)[0].id
    answer = client.post(
        f"{DEVICES}/{session_id}/abmelden",
        headers={**HX, "HX-Current-URL": "http://testserver/gui/geraete"},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/geraete"
    assert len(_sessions(engine)) == 1


def test_a_wrong_admin_pin_says_so_and_the_team_pin_opens_nothing(client, engine):
    _set_up_and_sign_in(client, engine)
    for pin in ("0000", PIN):
        answer = _owner(client, pin=pin)
        assert answer.status_code == 401 and "stimmt nicht" in answer.text
    assert _owner(client, pin="1111").status_code == 401
    waits = _owner(client)
    assert waits.status_code == 401 and "Zu viele Versuche" in waits.text
    assert client.get(DEVICES, headers=HTML).status_code == 303


@pytest.mark.parametrize(
    "back",
    [
        "https://evil.example/",
        "//evil.example/x",
        "/elsewhere",
        "/gui/\\x",
        # The owner pages themselves lead nowhere useful (review of #587).
        "/gui/inhaber",
        "/gui/inhaber/schliessen",
    ],
)
def test_the_way_back_stays_on_the_view(client, engine, back):
    _set_up_and_sign_in(client, engine)
    answer = _owner(client, back=back)
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"


def test_the_admin_pin_form_needs_its_csrf_token_and_this_site(client, engine):
    _set_up_and_sign_in(client, engine)
    bare = client.post("/gui/inhaber", data={"pin": ADMIN_PIN, "weiter": DEVICES})
    assert bare.status_code == 403
    foreign = client.post(
        "/gui/inhaber",
        data={"csrf_token": "testtoken", "pin": ADMIN_PIN},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert client.get(DEVICES, headers=HTML).status_code == 303


def test_without_a_session_the_admin_pin_page_goes_to_the_sign_in(client):
    assert client.get("/gui/inhaber").headers["location"] == "/gui/anmelden"
    assert _owner(client).headers["location"] == "/gui/anmelden"


def test_the_owner_signs_out_another_device(client, engine):
    _set_up_and_sign_in(client, engine)
    other = _second_device(engine)
    assert _owner(client).status_code == 303
    own, theirs = sorted(_sessions(engine), key=lambda row: row.created_at)
    listed = client.get(DEVICES, headers=HTML).text
    assert "Dieses Gerät" in listed and f"/gui/geraete/{theirs.id}/abmelden" in listed

    answer = client.post(f"{DEVICES}/{theirs.id}/abmelden", headers=HX)
    assert answer.status_code == 200 and str(theirs.id) not in answer.text
    assert [row.id for row in _sessions(engine)] == [own.id]
    assert other.get("/gui/", headers=HTML).status_code == 303


def test_signing_out_this_device_goes_to_the_sign_in(client, engine):
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    own = _sessions(engine)[0]
    with Session(engine) as session:
        assert _signed_in(session, own.id)
    answer = client.post(f"{DEVICES}/{own.id}/abmelden", headers=HX)
    assert answer.headers["HX-Redirect"] == "/gui/anmelden"
    assert _sessions(engine) == []
    # Its event stream ends on the next tick (#597).
    with Session(engine) as session:
        assert not _signed_in(session, own.id)


def test_closing_the_owner_part_leaves_staff_rights(client, engine):
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    closed = client.post("/gui/inhaber/schliessen", data={"csrf_token": "testtoken"})
    assert closed.status_code == 303 and closed.headers["location"] == "/gui/"
    assert client.get(DEVICES, headers=HTML).status_code == 303
    assert client.get("/gui/", headers=HTML).status_code == 200


def _age_owner_clock(engine, minutes: int) -> None:
    with Session(engine) as session:
        session.execute(
            update(GuiSession).values(
                admin_active_at=clock.utcnow() - timedelta(minutes=minutes)
            )
        )
        session.commit()


def test_an_owner_page_keeps_owner_rights_and_staff_pages_do_not(client, engine):
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    # Staff pages and staff writes never move the owner's own clock (#600).
    _age_owner_clock(engine, 14)
    assert client.get("/gui/", headers=HTML).status_code == 200
    assert client.post("/gui/kopfzeile/ki-pausieren", headers=HX).status_code == 200
    idle_since = _sessions(engine)[0].admin_active_at
    assert idle_since is not None
    assert idle_since < clock.utcnow() - timedelta(minutes=13)
    # An owner page does: the 15 minutes start again.
    assert client.get(DEVICES, headers=HTML).status_code == 200
    active_at = _sessions(engine)[0].admin_active_at
    assert active_at is not None
    assert active_at > clock.utcnow() - timedelta(minutes=1)


def test_owner_rights_end_when_the_owner_is_idle_though_the_staff_write(client, engine):
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    _age_owner_clock(engine, 16)
    # The staff's write keeps the device unlocked, the owner rights are gone.
    assert client.post("/gui/kopfzeile/ki-pausieren", headers=HX).status_code == 200
    gone = client.get(DEVICES, headers=HTML)
    assert gone.status_code == 303
    assert gone.headers["location"] == "/gui/inhaber?weiter=/gui/geraete"


def test_the_admin_pin_unlocks_a_locked_device(client, engine):
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    assert client.get(DEVICES, headers=HTML).status_code == 303
    assert _owner(client).status_code == 303
    assert client.get(DEVICES, headers=HTML).status_code == 200
    assert client.post("/gui/kopfzeile/ki-pausieren", headers=HX).status_code == 200


def test_the_settings_sheet_names_the_device_list(client, engine):
    _set_up_and_sign_in(client, engine)
    assert 'hx-get="/gui/einstellungen"' in client.get("/gui/", headers=HTML).text
    sheet = client.get("/gui/einstellungen", headers=HX).text
    for page in (
        "/gui/zeiten",
        "/gui/lieferung",
        "/gui/menue",
        "/gui/daten",
        "/gui/geraete",
    ):
        assert f'href="{page}"' in sheet


# --- the owner's tab "Zeiten" (#571, plan number 7) -----------------------------

HOURS = "/gui/zeiten"
HOURS_WRITES = [
    (f"{HOURS}/woche/1", {"gesehen": "x", "pickup-von-0": "10:00"}),
    (f"{HOURS}/tag", {"datum": "2099-01-01", "art": "geschlossen"}),
]


def _hours_untouched(engine) -> bool:
    """The seed's 36 spans, no special day, no audit row of the tab."""
    with Session(engine) as session:
        spans = session.scalar(select(func.count()).select_from(OpeningHours))
        days = session.scalar(select(func.count()).select_from(SpecialDay))
        audits = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.in_(("opening_hours.changed", "special_day.set")))
        )
    return (spans, days, audits) == (36, 0, 0)


def test_the_weekly_hours_ask_the_admin_pin_first(client, engine):
    _set_up_and_sign_in(client, engine)
    page = client.get(HOURS, headers=HTML)
    assert page.status_code == 303
    assert page.headers["location"] == "/gui/inhaber?weiter=/gui/zeiten"
    # The parts of the page ask it too, and show nothing before.
    # After the PIN they lead back to the page they are a part of, never to
    # themselves: alone a part is no page (no stylesheet, no script).
    on_page = {**HX, "HX-Current-URL": "http://testserver/gui/zeiten"}
    for path in (f"{HOURS}/woche", f"{HOURS}/woche/1"):
        part = client.get(path, headers=on_page)
        assert part.status_code == 401 and "11:30" not in part.text
        assert part.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/zeiten"
        # Without the page's address the PIN page leads to the board.
        bare = client.get(path, headers=HX)
        assert bare.headers["HX-Redirect"] == "/gui/inhaber?weiter="
    # The team PIN opens nothing here.
    assert _owner(client, pin=PIN, back=HOURS).status_code == 401
    assert client.get(HOURS, headers=HTML).status_code == 303

    assert _owner(client, back=HOURS).headers["location"] == HOURS
    shown = client.get(HOURS, headers=HTML)
    assert shown.status_code == 200 and "Öffnungszeiten der Woche" in shown.text
    assert shown.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("path", "data"), HOURS_WRITES)
def test_a_write_to_the_hours_without_the_admin_pin_writes_nothing(
    client, engine, path, data
):
    _set_up_and_sign_in(client, engine)
    answer = client.post(
        path,
        data=data,
        headers={**HX, "HX-Current-URL": "http://testserver/gui/zeiten"},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/zeiten"
    assert _hours_untouched(engine)


@pytest.mark.parametrize(("path", "data"), HOURS_WRITES)
def test_a_write_to_the_hours_from_another_site_is_refused(client, engine, path, data):
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=HOURS).status_code == 303
    answer = client.post(
        path, data=data, headers={**HX, "Origin": "https://evil.example"}
    )
    assert answer.status_code == 403
    assert _hours_untouched(engine)


def test_the_hours_close_again_when_the_owner_is_idle(client, engine):
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=HOURS).status_code == 303
    assert client.get(HOURS, headers=HTML).status_code == 200
    _age_owner_clock(engine, 16)
    gone = client.post(HOURS_WRITES[1][0], data=HOURS_WRITES[1][1], headers=HX)
    assert gone.status_code == 401 and _hours_untouched(engine)


# --- the owner's tab "Lieferung" (#571, plan number 8) --------------------------

DELIVERY = "/gui/lieferung"
ZONE_FORM = {
    "name": "Zone 1",
    "plz": "76530",
    "pauschale": "2,50",
    "mindestwert": "15",
    "lieferzeit": "40",
    "aktiv": "1",
}


def _a_zone(engine) -> DeliveryZone:
    with Session(engine) as session:
        zone = DeliveryZone(
            tenant_id=session.scalars(select(Tenant.id)).one(),
            name="Zone 9",
            match_type="postal_code",
            postal_codes=["76599"],
            fee_cents=190,
            min_order_cents=1200,
            eta_minutes=35,
        )
        session.add(zone)
        session.commit()
        session.refresh(zone)
        session.expunge(zone)
        return zone


def _zones_untouched(engine) -> bool:
    """The one zone as `_a_zone` wrote it, no audit row of the tab."""
    with Session(engine) as session:
        zones = [
            (zone.name, zone.fee_cents, zone.active)
            for zone in session.scalars(select(DeliveryZone))
        ]
        audits = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity == "delivery_zones")
        )
    return zones == [("Zone 9", 190, True)] and audits == 0


def test_the_delivery_zones_ask_the_admin_pin_first(client, engine):
    zone = _a_zone(engine)
    _set_up_and_sign_in(client, engine)
    page = client.get(DELIVERY, headers=HTML)
    assert page.status_code == 303
    assert page.headers["location"] == "/gui/inhaber?weiter=/gui/lieferung"
    # The parts of the page ask it too, show nothing before and lead back to
    # the page after the PIN, never to themselves.
    on_page = {**HX, "HX-Current-URL": "http://testserver/gui/lieferung"}
    for path in (
        f"{DELIVERY}/zonen",
        f"{DELIVERY}/neu",
        f"{DELIVERY}/zone/{zone.id}",
    ):
        part = client.get(path, headers=on_page)
        assert part.status_code == 401, path
        assert "Zone 9" not in part.text and "76599" not in part.text
        assert part.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/lieferung"
    # The team PIN opens nothing here.
    assert _owner(client, pin=PIN, back=DELIVERY).status_code == 401
    assert client.get(DELIVERY, headers=HTML).status_code == 303

    assert _owner(client, back=DELIVERY).headers["location"] == DELIVERY
    shown = client.get(DELIVERY, headers=HTML)
    assert shown.status_code == 200 and "Zone 9" in shown.text
    assert shown.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("target", ["neu", "zone"])
def test_a_write_to_the_zones_without_the_admin_pin_writes_nothing(
    client, engine, target
):
    zone = _a_zone(engine)
    _set_up_and_sign_in(client, engine)
    path = f"{DELIVERY}/neu" if target == "neu" else f"{DELIVERY}/zone/{zone.id}"
    answer = client.post(
        path,
        data=ZONE_FORM,
        headers={**HX, "HX-Current-URL": "http://testserver/gui/lieferung"},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/lieferung"
    assert _zones_untouched(engine)


@pytest.mark.parametrize("target", ["neu", "zone"])
def test_a_write_to_the_zones_from_another_site_is_refused(client, engine, target):
    zone = _a_zone(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DELIVERY).status_code == 303
    path = f"{DELIVERY}/neu" if target == "neu" else f"{DELIVERY}/zone/{zone.id}"
    answer = client.post(
        path, data=ZONE_FORM, headers={**HX, "Origin": "https://evil.example"}
    )
    assert answer.status_code == 403
    assert _zones_untouched(engine)


def _nothing_of_the_zones_opens(client, engine, zone) -> None:
    """The page, each of its parts and both writes lead to the admin PIN, show
    nothing and write nothing."""
    on_page = {**HX, "HX-Current-URL": "http://testserver/gui/lieferung"}
    assert client.get(DELIVERY, headers=HTML).status_code == 303
    for path in (
        f"{DELIVERY}/zonen",
        f"{DELIVERY}/neu",
        f"{DELIVERY}/zone/{zone.id}",
    ):
        part = client.get(path, headers=on_page)
        assert part.status_code == 401, path
        assert "Zone 9" not in part.text and "76599" not in part.text
        assert part.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/lieferung"
    for path in (f"{DELIVERY}/neu", f"{DELIVERY}/zone/{zone.id}"):
        write = client.post(path, data=ZONE_FORM, headers=on_page)
        assert write.status_code == 401, path
        assert write.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/lieferung"
    assert _zones_untouched(engine)


def test_the_zones_close_again_when_the_owner_is_idle(client, engine):
    zone = _a_zone(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DELIVERY).status_code == 303
    assert client.get(DELIVERY, headers=HTML).status_code == 200
    _age_owner_clock(engine, 16)
    _nothing_of_the_zones_opens(client, engine, zone)


def test_a_locked_device_opens_nothing_of_the_zones(client, engine):
    """The team's idle lock ends the owner's rights on the device too."""
    zone = _a_zone(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DELIVERY).status_code == 303
    assert client.get(DELIVERY, headers=HTML).status_code == 200
    _lock(engine)
    _nothing_of_the_zones_opens(client, engine, zone)


# --- the owner's tab "Daten" (#571, plan number 10) ------------------------------

DATA = "/gui/daten"
DATA_PAGE = {**HX, "HX-Current-URL": "http://testserver/gui/daten"}
DATA_NOTE = "Frau Beispiel, Bestellung vom 12.10."
DATA_WRITES = [
    ("anfrage", {"gesehen": "", "art": "access", "weg": "mail", "notiz": "neu"}),
    ("anfrage/dsr_2026_001/frist", {}),
    ("anfrage/dsr_2026_001/antwort", {"ergebnis": "refused"}),
    ("anfrage/dsr_2026_001/pruefen", {"name": "Beispiel", "tag-1": "2026-10-12"}),
]


def _a_request(engine) -> DataSubjectRequest:
    with Session(engine) as session:
        tenant_id = session.scalars(select(Tenant.id)).one()
        row = log_request(
            session,
            tenant_id,
            kind="access",
            channel="mail",
            note=DATA_NOTE,
            received_at=clock.utcnow().replace(year=2026, month=11, day=2),
            zone="Europe/Berlin",
        )
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _requests_untouched(engine, before: DataSubjectRequest) -> bool:
    """The one request as `_a_request` wrote it, and its one audit row."""
    with Session(engine) as session:
        rows = [
            (row.reference, row.deadline, row.answered_at)
            for row in session.scalars(select(DataSubjectRequest))
        ]
        audits = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity == "data_subject_request")
        )
    return rows == [("dsr_2026_001", before.deadline, None)] and audits == 1


def _nothing_of_the_requests_opens(client, engine, before) -> None:
    """The page, each of its parts and every write lead to the admin PIN, show
    nothing and write nothing."""
    assert client.get(DATA, headers=HTML).status_code == 303
    for part in ("anfragen", "anfrage/dsr_2026_001"):
        answer = client.get(f"{DATA}/{part}", headers=DATA_PAGE)
        assert answer.status_code == 401, part
        assert "Beispiel" not in answer.text and "dsr_2026_001" not in answer.text
        assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/daten"
    for path, data in DATA_WRITES:
        write = client.post(f"{DATA}/{path}", data=data, headers=DATA_PAGE)
        assert write.status_code == 401, path
        assert write.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/daten"
        assert "Beispiel" not in write.text
    assert _requests_untouched(engine, before)


def test_the_requests_of_guests_ask_the_admin_pin_first(client, engine):
    before = _a_request(engine)
    _set_up_and_sign_in(client, engine)
    page = client.get(DATA, headers=HTML)
    assert page.status_code == 303
    assert page.headers["location"] == "/gui/inhaber?weiter=/gui/daten"
    _nothing_of_the_requests_opens(client, engine, before)
    # The team PIN opens nothing here.
    assert _owner(client, pin=PIN, back=DATA).status_code == 401
    assert client.get(DATA, headers=HTML).status_code == 303

    assert _owner(client, back=DATA).headers["location"] == DATA
    shown = client.get(DATA, headers=HTML)
    assert shown.status_code == 200 and "dsr_2026_001" in shown.text
    assert shown.headers["Cache-Control"] == "no-store"
    opened = client.get(f"{DATA}/anfrage/dsr_2026_001", headers=HX)
    assert opened.status_code == 200 and DATA_NOTE in opened.text
    assert opened.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("path", "data"), DATA_WRITES)
def test_a_write_to_the_requests_from_another_site_is_refused(
    client, engine, path, data
):
    before = _a_request(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DATA).status_code == 303
    answer = client.post(
        f"{DATA}/{path}", data=data, headers={**HX, "Origin": "https://evil.example"}
    )
    assert answer.status_code == 403
    assert _requests_untouched(engine, before)


def test_the_requests_close_again_when_the_owner_is_idle(client, engine):
    before = _a_request(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DATA).status_code == 303
    assert client.get(DATA, headers=HTML).status_code == 200
    _age_owner_clock(engine, 16)
    _nothing_of_the_requests_opens(client, engine, before)


def test_a_locked_device_opens_nothing_of_the_requests(client, engine):
    """The team's idle lock ends the owner's rights on the device too."""
    before = _a_request(engine)
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=DATA).status_code == 303
    assert client.get(DATA, headers=HTML).status_code == 200
    _lock(engine)
    _nothing_of_the_requests_opens(client, engine, before)


# --- one row of the call list in detail (#571, plan number 10) -------------------

CALL_DETAIL = "/gui/anrufe/anruf"


def _a_call(engine) -> str:
    with Session(engine) as session:
        call = Call(
            tenant_id=session.scalars(select(Tenant.id)).one(),
            external_session_id="s-detail",
            started_at=clock.utcnow() - timedelta(minutes=5),
            delete_after=clock.utcnow().date(),
            tool_calls=[{"name": "draft_order", "duration_ms": 80, "ok": True}],
        )
        session.add(call)
        session.commit()
        return str(call.id)


def _call_looks(engine) -> int | None:
    with Session(engine) as session:
        return session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "gui.call_read")
        )


def test_the_staff_open_a_calls_detail_with_the_restaurants_login(client, engine):
    call_id = _a_call(engine)
    _set_up_and_sign_in(client, engine)
    answer = client.post(CALL_DETAIL, data={"anruf": call_id}, headers=HX)
    assert answer.status_code == 200 and "1. draft_order" in answer.text
    assert answer.headers["Cache-Control"] == "no-store"
    assert _call_looks(engine) == 1


def test_a_calls_detail_from_another_site_or_a_locked_device_shows_nothing(
    client, engine
):
    call_id = _a_call(engine)
    _set_up_and_sign_in(client, engine)
    forged = client.post(
        CALL_DETAIL,
        data={"anruf": call_id},
        headers={**HX, "Origin": "https://evil.example"},
    )
    assert forged.status_code == 403 and "draft_order" not in forged.text
    _lock(engine)
    locked = client.post(CALL_DETAIL, data={"anruf": call_id}, headers=HX)
    assert locked.status_code == 401 and "draft_order" not in locked.text
    assert locked.headers["HX-Redirect"] == "/gui/entsperren"
    assert _call_looks(engine) == 0


def test_the_staff_pages_of_the_frame_need_no_admin_pin(client, engine):
    """The frame adds tabs, not a lock: "Anrufe", "Kennzahlen" and the
    read-only "Menü" stay with the restaurant's login (docs/06 section 4)."""
    _set_up_and_sign_in(client, engine)
    for path in ("/gui/anrufe", "/gui/kennzahlen", "/gui/menue"):
        page = client.get(path, headers=HTML)
        assert page.status_code == 200 and 'href="/gui/zeiten"' in page.text


def test_a_check_card_answer_without_the_admin_pin_goes_to_the_pin_page(
    client, engine, monkeypatch
):
    """Staff change the team number without a PIN; keeping it needs the owner."""
    monkeypatch.setattr(settings, "transfer_targets", "+497215551234")
    _set_up_and_sign_in(client, engine)
    changed = client.post(
        "/gui/teamnummer", data={"nummer": "+497215551234"}, headers=HX
    )
    assert changed.status_code == 200
    with Session(engine) as session:
        card_id, seen = session.execute(
            select(StaffChange.id, StaffChange.updated_at)
        ).one()
    shown = {"gesehen": seen.isoformat()}
    answer = client.post(
        f"/gui/pruefen/{card_id}/uebernehmen",
        data=shown,
        headers={**HX, "HX-Current-URL": "http://testserver/gui/"},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/"
    assert _owner(client, back="/gui/").headers["location"] == "/gui/"
    kept = client.post(f"/gui/pruefen/{card_id}/uebernehmen", data=shown, headers=HX)
    assert kept.status_code == 200


def test_a_locked_device_writing_behind_the_admin_pin_goes_to_the_admin_pin(
    client, engine
):
    """The owner reads the device list past the idle lock and taps "Gerät
    abmelden" (#597): the admin PIN page, which unlocks too, not the team PIN
    page, whose PIN would close owner rights and lose the click."""
    _set_up_and_sign_in(client, engine)
    assert _owner(client).status_code == 303
    _lock(engine)
    own = _sessions(engine)[0]
    answer = client.post(
        f"{DEVICES}/{own.id}/abmelden",
        headers={**HX, "HX-Current-URL": "http://testserver/gui/geraete"},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/geraete"
    # The same for a route that names the check only among its dependencies.
    card = client.post(
        f"/gui/pruefen/{own.id}/uebernehmen",
        headers={**HX, "HX-Current-URL": "http://testserver/gui/"},
    )
    assert card.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/"
    # A staff write on the locked device still asks the team PIN.
    staff = client.post("/gui/kopfzeile/ki-pausieren", headers=HX)
    assert staff.headers["HX-Redirect"] == "/gui/entsperren"


def test_the_right_admin_pin_on_a_device_signed_out_meanwhile_goes_to_the_sign_in(
    client, engine, monkeypatch
):
    _set_up_and_sign_in(client, engine)

    def signed_out(*_args):
        raise AdminRefused(ended=True)

    monkeypatch.setattr("api.gui.access.unlock_admin", signed_out)
    answer = _owner(client)
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/anmelden"


def test_a_device_in_use_gets_its_cookies_again(client, engine):
    """The browser drops a cookie 400 days after it was set (#598): use sets
    both again, at most once a minute."""
    _set_up_and_sign_in(client, engine)
    with Session(engine) as session:
        session.execute(
            update(GuiSession).values(
                last_seen_at=clock.utcnow() - timedelta(minutes=2)
            )
        )
        session.commit()
    page = client.get("/gui/", headers=HTML)
    cookies = " ".join(page.headers.get_list("set-cookie"))
    assert "maex_session=" in cookies and "maex_device=" in cookies
    assert "Max-Age=34560000" in cookies
    again = " ".join(client.get("/gui/", headers=HTML).headers.get_list("set-cookie"))
    assert "maex_session=" not in again and "maex_device=" not in again


@pytest.mark.parametrize(
    ("back", "lands"),
    [("/gui/raum_plan.v2", "/gui/raum_plan.v2"), ("/gui/../elsewhere", "/gui/")],
)
def test_the_way_back_takes_any_page_of_the_view(client, engine, back, lands):
    _set_up_and_sign_in(client, engine)
    assert _owner(client, back=back).headers["location"] == lands


def test_a_broken_current_url_still_goes_to_the_admin_pin(client, engine):
    _set_up_and_sign_in(client, engine)
    own = _sessions(engine)[0]
    answer = client.post(
        f"{DEVICES}/{own.id}/abmelden",
        headers={**HX, "HX-Current-URL": "http://["},
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter="


def test_without_a_team_pin_the_lock_page_says_so(client, engine):
    _set_up_and_sign_in(client, engine)
    with Session(engine) as session:
        session.execute(update(GuiAccount).values(team_pin=None))
        session.commit()
    _lock(engine)
    answer = client.post(
        "/gui/entsperren", data={"csrf_token": "testtoken", "pin": PIN}
    )
    assert answer.status_code == 409 and "Team-PIN" in answer.text


def test_csrf_matches_needs_both_ascii_and_equal():
    assert csrf_matches("abc", "abc")
    assert not csrf_matches("abc", "abd")
    assert not csrf_matches("", "")
    assert not csrf_matches(None, "abc") and not csrf_matches("abc", None)
    assert not csrf_matches("äbc", "äbc")  # compare_digest takes no non-ASCII str


# --- customer view hardening (#827 F2, F4, F6) ---------------------------------


def test_every_answer_of_the_view_but_its_files_says_no_store(client, engine):
    """#827 F2: the board's parts hold guests' names, numbers and addresses,
    and a refusal is no page to keep either (#821). One middleware sets it;
    the view's own files keep their `no-cache`."""
    refused = client.get("/gui/fragments/heute", headers=HX)
    assert refused.status_code == 401
    assert refused.headers["Cache-Control"] == "no-store"
    _set_up_and_sign_in(client, engine)
    for path in (
        "/gui/fragments/heute",
        "/gui/fragments/rueckrufe",
        "/gui/rueckrufe/verlauf",
        "/gui/fragments/bestellungen",
        "/gui/fragments/pruefen",
        "/gui/gibt-es-nicht",
    ):
        answer = client.get(path, headers=HX)
        assert answer.headers.get("Cache-Control") == "no-store", path
    foreign = client.post(
        "/gui/kopfzeile/ki-pausieren",
        headers={**HX, "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert foreign.headers["Cache-Control"] == "no-store"
    static = client.get("/gui/static/app.css")
    assert static.status_code == 200 and static.headers["Cache-Control"] == "no-cache"


def test_wrong_admin_pins_on_a_locked_device_count_on_its_five_tries(client, engine):
    """#827 F4: the admin PIN page unlocks a locked device too, so it takes no
    more tries than the lock page: three wrong team PINs and two wrong admin
    PINs sign the device out."""
    _set_up_and_sign_in(client, engine)
    _lock(engine)
    for _ in range(3):
        wrong = client.post(
            "/gui/entsperren", data={"csrf_token": "testtoken", "pin": "0000"}
        )
        assert wrong.status_code == 401
    assert _owner(client, pin="1111").status_code == 401
    ended = _owner(client, pin="2222")
    assert ended.status_code == 303 and ended.headers["location"] == "/gui/anmelden"
    assert _sessions(engine) == []


def test_wrong_admin_pins_on_a_device_in_use_never_sign_it_out(client, engine):
    """The decision of 09.11.2026 stays: on a device in use the admin PIN only
    brings waits."""
    _set_up_and_sign_in(client, engine)
    for pin in ("0000", "0001"):
        assert _owner(client, pin=pin).status_code == 401
    assert "Zu viele Versuche" in _owner(client, pin="0002").text
    assert len(_sessions(engine)) == 1


FLOOR_PLAN_AND_NOTICE = [
    ("GET", "/gui/raumplan"),
    ("GET", "/gui/raumplan/plan"),
    ("POST", "/gui/raumplan/bereiche"),
    ("PUT", f"/gui/raumplan/bereiche/{ANY_ID}"),
    ("GET", "/gui/vorlauf"),
    ("POST", "/gui/vorlauf"),
]


@pytest.mark.parametrize(("method", "path"), FLOOR_PLAN_AND_NOTICE)
def test_the_floor_plan_and_the_notice_ask_the_admin_pin(client, engine, method, path):
    """#827 F6 (Maxi, 2026-11-17): tables, seats and the notice per weekday
    turn callers away when wrong, so they are the owner's like the weekly
    hours; the standard wait and the price answer stay with the staff."""
    _set_up_and_sign_in(client, engine)
    answer = client.request(
        method, path, headers={**HX, "HX-Current-URL": "http://testserver/gui/"}
    )
    assert answer.status_code == 401
    assert answer.headers["HX-Redirect"] == "/gui/inhaber?weiter=/gui/"


def test_the_floor_plan_opens_after_the_admin_pin(client, engine):
    _set_up_and_sign_in(client, engine)
    page = client.get("/gui/raumplan", headers=HTML)
    assert page.status_code == 303
    assert page.headers["location"] == "/gui/inhaber?weiter=/gui/raumplan"
    assert _owner(client, back="/gui/raumplan").headers["location"] == "/gui/raumplan"
    assert client.get("/gui/raumplan", headers=HTML).status_code == 200
    assert client.get("/gui/raumplan/plan").json()["ok"] is True
    assert client.get("/gui/vorlauf", headers=HX).status_code == 200
