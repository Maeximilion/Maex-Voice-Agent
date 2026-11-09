"""The restaurant's login on the operations view over HTTP (#570, D34, docs/23 §6).

These tests switch off the signed-in device every other GUI test acts as
(conftest `_gui_signed_in`), so they meet the view as a browser does."""

import re
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core import time as clock
from api.db import get_db
from api.domain.access.sessions import IDLE, stack_tenant_id
from api.domain.access.setup import issue_link, use_link
from api.gui.router import router as gui_router
from api.main import app
from api.models import GuiLoginFailure, GuiSession
from scripts.seed import seed

pytestmark = pytest.mark.gui_login

PASSWORD = "Kuechentablet-2026"
PIN = "4711"
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
        },
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/gui/"


def _routes():
    for route in gui_router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", ANY_ID, route.path)
        for method in route.methods:
            yield method, path


# --- without a session ---------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), sorted(set(_routes())))
def test_every_route_of_the_view_refuses_without_a_session(client, method, path):
    answer = client.request(method, path, headers=HX)
    assert answer.status_code == 401, path
    assert answer.headers["HX-Redirect"] == "/gui/anmelden"


def test_a_page_load_goes_to_the_sign_in(client):
    for path in ("/gui", "/gui/", "/gui/kennzahlen", "/gui/raumplan"):
        answer = client.get(path, headers=HTML)
        assert answer.status_code == 303, path
        assert answer.headers["location"] == "/gui/anmelden"


def test_the_event_stream_refuses_without_a_session(client):
    assert client.get("/gui/events").status_code == 401


def test_the_sign_in_pages_and_static_files_need_no_session(client):
    for path in ("/gui/anmelden", "/gui/einrichten", "/gui/static/app.css"):
        assert client.get(path, headers=HTML).status_code == 200, path


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


def test_the_check_leaves_no_transaction_open_for_the_event_stream(engine):
    """The stream holds its request for hours (router `events`): the session
    check must hand its pooled connection back before it."""
    from starlette.requests import Request

    from api.gui.session import require_gui_session

    with Session(engine) as session:
        code, _ = issue_link(
            session, stack_tenant_id(session), "operator:test", clock.utcnow()
        )
        token = use_link(
            session, stack_tenant_id(session), code, PASSWORD, PIN, "x", clock.utcnow()
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
