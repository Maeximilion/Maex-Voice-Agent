"""The public site (#892): serving, the unset-configuration cases, the files it
loads and the tokens it shares with the operations view."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.site import app as site_app
from api.site import content, waves
from api.site.config import SiteSettings

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "api" / "gui" / "static"
PAGES = ("/", "/anfrage", "/impressum", "/datenschutz")


@pytest.fixture(autouse=True)
def _no_site_settings_from_outside(monkeypatch):
    """A developer's environment or .env must not change what the tests expect."""
    for name in ("SITE_LOGIN_URL", "SITE_CONTACT_EMAIL", "SITE_INQUIRY_ACTION"):
        monkeypatch.delenv(name, raising=False)


def settings_of(**values: str) -> SiteSettings:
    # `_env_file=None` is pydantic-settings' own argument; its signature is not typed.
    return SiteSettings(_env_file=None, **values)  # type: ignore[call-arg]


def client(**values: str) -> TestClient:
    return TestClient(site_app.create_app(settings_of(**values)))


def text_of(client_: TestClient, path: str) -> str:
    response = client_.get(path)
    assert response.status_code == 200
    return response.text


@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_served_with_the_site_only_policy(path):
    response = client().get(path)

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"
    assert 'lang="de"' in response.text


def test_the_health_check_answers_and_nothing_of_the_applications_is_there():
    c = client()

    assert c.get("/health").json() == {"status": "ok"}
    for path in ("/gui/", "/gui/anmelden", "/v1/tools/ping", "/tablet-demo", "/docs"):
        assert c.get(path).status_code == 404, path


@pytest.mark.parametrize("path", PAGES)
def test_a_page_loads_nothing_from_an_outside_host_and_has_no_inline_code(path):
    html = text_of(client(), path)

    assert not re.findall(r"""(?:src|href|action)=["']https?://""", html)
    assert not re.findall(r"<script(?![^>]*\bsrc=)", html)
    assert not re.findall(r"""\sstyle=["']""", html)
    assert "<style" not in html
    assert not re.findall(r"\son[a-z]+=", html)


def test_the_stylesheet_names_no_outside_host_and_the_fonts_are_served():
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    c = client()

    assert "@import" not in css
    assert not re.findall(r"url\(\s*[\"']?(?:https?:)?//", css)
    assert "fonts.googleapis" not in css
    for weight in (400, 500, 600, 700):
        name = f"/static/fonts/ibm-plex-sans-latin-{weight}-normal.woff2"
        response = c.get(name)
        assert response.status_code == 200
        assert response.content[:4] == b"wOF2"
        assert name in css
    assert (STATIC / "fonts" / "IBM-Plex-Sans-OFL.txt").is_file()


def test_the_landing_page_has_what_the_design_asks_for():
    html = text_of(client(), "/")

    for words in (
        "Aktuell in der Testphase",
        "Es klingelt.",
        "nimmt",
        "So funktioniert's",
        "Anfrage",
        "Kunden-Login",
        "Reservierung",
        "Abholung",
        "Lieferung",
        "Der Bon kommt aus dem Drucker",
        "Ihre Daten entscheiden",
        "Nichts ohne ein klares Ja",
        "Im Zweifel Ihr Team",
        "Zur Anfrage",
        "Impressum",
        "Datenschutz",
    ):
        assert words in html, words
    # Three sound waves, the three calls with their sentences, four steps.
    assert html.count('class="wave ') == 3
    assert html.count("data-call=") == 3
    assert html.count('class="step rv"') == 4
    # The first call stands complete without any script.
    assert "Eingetragen. Vielen Dank und bis morgen." in html


def test_the_copy_names_no_price_and_no_figure_of_performance():
    html = text_of(client(), "/")
    copy = re.sub(r"<[^>]+>", " ", html)

    assert "€" not in copy and "Euro" not in copy
    assert not re.findall(r"\d+\s*(?:%|Prozent)", copy)
    for call in content.CALLS:
        for sentence in call.says:
            assert "€" not in sentence and "Euro" not in sentence
    assert "Beispielgespräch" in copy


def test_with_nothing_configured_nothing_is_offered_as_working():
    c = client()
    landing = text_of(c, "/")
    form = text_of(c, "/anfrage")

    # No sign-in target: a label that says so, no link.
    assert 'class="btn btn-off" aria-disabled="true"' in landing
    assert "noch nicht freigeschaltet" in landing
    assert "login" not in re.findall(r'href="([^"]*)"', landing)
    # No contact address: no mail link anywhere.
    assert "mailto:" not in landing and "mailto:" not in form
    # No inquiry route: the form is shown, disabled and without an action.
    assert "<fieldset disabled>" in form
    assert "Das Formular ist noch nicht freigeschaltet" in form
    tag = re.search(r"<form[^>]*>", form)
    assert tag and "action=" not in tag.group(0)
    # No demo yet: no hero action, no section, no link to the demo.
    assert "Jetzt ausprobieren" not in landing
    assert "tablet-demo" not in landing
    assert 'id="demo"' not in landing


def test_configured_values_turn_their_parts_on():
    c = client(
        site_login_url="https://login.example.com/gui/anmelden",
        site_contact_email="kontakt@example.com",
        site_inquiry_action="/anfrage/senden",
    )
    landing = text_of(c, "/")
    form = text_of(c, "/anfrage")

    assert 'href="https://login.example.com/gui/anmelden">Kunden-Login' in landing
    assert "btn-off" not in landing
    assert 'href="mailto:kontakt@example.com"' in landing
    assert 'href="mailto:kontakt@example.com"' in form
    assert "<fieldset disabled>" not in form
    assert 'method="post" action="/anfrage/senden"' in form
    assert "noch nicht freigeschaltet" not in form


def test_a_local_path_is_a_valid_sign_in_target():
    c = client(site_login_url="/gui/anmelden")

    assert 'href="/gui/anmelden">Kunden-Login' in text_of(c, "/")


@pytest.mark.parametrize(
    "setting",
    [
        {"site_login_url": "javascript:alert(1)"},
        {"site_login_url": "//evil.example.com/login"},
        {"site_login_url": "http://login.example.com/", "env": "production"},
        {"site_login_url": "https://user:pw@login.example.com/"},
        {"site_login_url": "login.example.com"},
        {"site_contact_email": "kontakt"},
        {"site_contact_email": "a@b.example.com, c@d.example.com"},
        {"site_contact_email": '"x"@example.com'},
        {"site_inquiry_action": "https://evil.example.com/take"},
        {"site_inquiry_action": "//evil.example.com/take"},
        {"site_inquiry_action": "senden"},
    ],
)
def test_a_value_that_is_not_what_it_says_stops_the_start(setting):
    with pytest.raises(ValidationError):
        settings_of(**setting)


def test_a_value_is_never_shown_unescaped(monkeypatch):
    # The settings are checked, but the template escapes anyway.
    c = client(site_login_url="https://login.example.com/?a=1&b=<2>")

    html = text_of(c, "/")

    assert "&b=<2>" not in html
    assert "&amp;b=&lt;2&gt;" in html


def test_once_the_demo_exists_the_hero_offers_it(monkeypatch):
    monkeypatch.setattr(content, "DEMO_PATH", "/tablet-demo")
    html = text_of(client(), "/")

    assert "Jetzt ausprobieren" in html
    assert 'href="/tablet-demo"' in html
    assert 'id="demo"' in html


@pytest.mark.parametrize("path", ["/impressum", "/datenschutz"])
def test_the_legal_pages_are_placeholders_marked_as_not_publishable(path):
    html = text_of(client(), path)

    assert "nicht veröffentlichungsfähig" in html
    assert '<meta name="robots" content="noindex">' in html
    # No invented company details: no street, no register number, no tax id.
    assert not re.findall(r"\b\d{5}\b|HRB|DE\d{9}|GmbH|UG\b", html)


def test_the_form_has_visible_labels_and_an_error_place_for_every_field():
    html = text_of(client(), "/anfrage")

    for field in ("kind", "business", "name", "mail", "phone", "text"):
        assert f'data-error="{field}"' in html
        assert f'name="{field}"' in html
    labels = re.findall(r'<label for="(f-[a-z]+)"', html)
    assert len(labels) == 6
    for target in labels:
        assert f'id="{target}"' in html
    assert "<novalidate" not in html and " novalidate" in html
    for kind in content.KINDS:
        assert f"<option>{kind}</option>" in html


def test_the_kinds_of_business_are_the_same_in_the_page_and_in_the_script():
    logic = (ROOT / "api" / "gui" / "ts" / "site-logic.ts").read_text(encoding="utf-8")
    listed_match = re.search(r"export const KINDS = \[(.*?)\];", logic, re.S)
    assert listed_match
    listed = listed_match.group(1)

    assert re.findall(r'"([^"]+)"', listed) == list(content.KINDS)


def test_the_waves_are_two_seamless_periods_of_path_data():
    for path in waves.paths().values():
        assert path.startswith(f"M0 {waves.MID} Q")
        assert path.endswith(f"{waves.WIDTH} {waves.MID}")
        # Every half wave starts and ends on the middle line.
        ends = re.findall(r"Q[\d.]+ \d+ (\d+) (\d+)", path)
        assert all(y == str(waves.MID) for _, y in ends)
        assert int(ends[-1][0]) == waves.WIDTH


def _tokens(css: str) -> dict[str, str]:
    root_match = re.search(r":root\{(.*?)\}", css, re.S)
    assert root_match
    root = root_match.group(1)
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", root))


def test_the_tokens_it_shares_with_the_operations_view_are_equal():
    board = _tokens((STATIC / "app.css").read_text(encoding="utf-8"))
    site = _tokens((STATIC / "site.css").read_text(encoding="utf-8"))
    shared = set(board) & set(site)

    assert {
        "bg",
        "surface",
        "accent",
        "text",
        "muted",
        "line",
        "danger",
        "warn-bg",
    } <= shared
    for name in shared:
        assert site[name].strip() == board[name].strip(), name


def test_every_animation_has_a_still_state_for_reduced_motion():
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion:reduce)") :]

    assert "animation:none!important" in block
    assert "transition:none!important" in block
    assert "scroll-behavior:auto" in block
    # Scroll reveals exist only where the browser has a scroll timeline.
    assert "@supports (animation-timeline:view())" in css
    for selector in (".rv{", ".track-fill{"):
        assert css.index(selector, css.index("@supports (animation-timeline")) > 0
    # Nothing animates a filter, and nothing follows the pointer.
    assert "filter:" not in css and "backdrop-filter" not in css
    assert "pointermove" not in (STATIC / "site.js").read_text(encoding="utf-8")
    assert "mousemove" not in (STATIC / "site.js").read_text(encoding="utf-8")


def test_the_scripts_are_compiled_and_named_by_the_pages():
    assert 'src="/static/site.js"' in text_of(client(), "/")
    assert 'src="/static/anfrage.js"' in text_of(client(), "/anfrage")
    for name in ("site.js", "site-logic.js", "anfrage.js"):
        assert (STATIC / name).is_file()
        assert (ROOT / "api" / "gui" / "ts" / name.replace(".js", ".ts")).is_file()


def test_the_logic_of_the_call_card_and_the_form_holds_in_node():
    node = shutil.which("node")
    if not node:
        # The api image carries no node; CI's runner does, and must run this.
        if os.environ.get("CI"):
            pytest.fail("the site logic check needs node")
        pytest.skip("the site logic check needs node")
    out = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("site_logic.mjs")),
            str(STATIC / "site-logic.js"),
        ],
        capture_output=True,
        text=True,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
