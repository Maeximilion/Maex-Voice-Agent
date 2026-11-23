"""The static export of the public site (#892, scripts/site_export.py): the pages
and their files land in one folder a plain file host can serve, every page of the
preview says noindex, robots.txt allows crawling, every link reaches a file, and
the legal texts come from outside the repository or the export says loudly that
they are missing."""

import re
from pathlib import Path
from typing import Any

import pytest

from api.site import app as site_app
from api.site import content
from api.site.config import SiteSettings
from scripts import site_export

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deploy" / "site" / "legal.example"
FILES = (
    "index.html",
    "anfrage/index.html",
    "impressum/index.html",
    "datenschutz/index.html",
    "404.html",
    ".htaccess",
    "robots.txt",
    "static/site.css",
    "static/site.js",
    "static/anfrage.js",
    "static/site-logic.js",
)


@pytest.fixture(autouse=True)
def _no_outside_settings_and_no_demo(monkeypatch):
    for name in (
        "SITE_LOGIN_URL",
        "SITE_CONTACT_EMAIL",
        "SITE_INQUIRY_ACTION",
        "SITE_PREVIEW",
        "SITE_LEGAL_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(site_app, "TABLET_DEMO", Path("/nonexistent/tablet-demo"))
    # The landing page links the demo (content.DEMO_PATH); most tests export
    # without one. The tests of the demo switch it back on.
    monkeypatch.setattr(content, "DEMO_PATH", None)


def settings_of(**values: Any) -> SiteSettings:
    return SiteSettings(_env_file=None, **values)  # type: ignore[call-arg]


def legal_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "legal"
    folder.mkdir()
    for name in ("impressum.html", "datenschutz.html"):
        (folder / name).write_text((EXAMPLE / name).read_text(encoding="utf-8"))
    return folder


def pages(out: Path) -> list[Path]:
    return sorted(out.rglob("*.html"))


def test_the_export_writes_every_page_and_its_files(tmp_path):
    out = tmp_path / "site"

    missing = site_export.export(
        out, settings_of(site_legal_dir=str(legal_dir(tmp_path)))
    )

    assert missing == []
    for name in FILES:
        assert (out / name).is_file(), name
    for weight in (400, 500, 600, 700):
        assert (
            out / "static" / "fonts" / f"ibm-plex-sans-latin-{weight}-normal.woff2"
        ).is_file()
    assert not (out / "tablet-demo").exists()


def test_every_exported_page_of_the_preview_says_noindex_nofollow(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())

    assert len(pages(out)) == 5  # four pages and the 404 page
    for page in pages(out):
        html = page.read_text(encoding="utf-8")
        assert html.count('name="robots"') == 1, page
        assert '<meta name="robots" content="noindex, nofollow">' in html, page
        assert 'class="btn btn-off" aria-disabled="true"' in html, page
        assert ">Kunden-Login</a>" not in html, page
        assert "Content-Security-Policy" in html, page


def test_the_exported_robots_txt_allows_crawling_and_has_no_sitemap(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())

    text = (out / "robots.txt").read_text(encoding="utf-8")
    rules = [line for line in text.splitlines() if not line.startswith("#")]
    assert rules == ["User-agent: *", "Allow: /"]
    assert "Disallow" not in text and "Sitemap" not in text
    assert not list(out.rglob("sitemap*"))


def test_every_link_of_the_export_reaches_a_file_and_pages_get_clean_paths(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())

    assert site_export.broken_links(out) == []
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/anfrage/"' in landing
    assert 'href="/impressum/"' in landing
    assert 'href="/anfrage"' not in landing
    assert 'href="/#ablauf"' in landing
    # Every link names a path of the host, so the folder works at any host name.
    for page in pages(out):
        html = page.read_text(encoding="utf-8")
        assert not re.findall(r'(?:href|src)="(?!/|#|mailto:)', html), page


def test_a_broken_link_is_found(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())
    (out / "static" / "site.js").unlink()

    assert site_export.broken_links(out) == ["index.html: /static/site.js"]


def test_without_legal_texts_the_placeholders_are_written_and_the_export_says_so(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "site"
    monkeypatch.setattr(site_export, "SiteSettings", lambda: settings_of())

    assert site_export.main(["--out", str(out)]) == 0

    err = capsys.readouterr().err
    assert "WARNING: no impressum.html" in err
    assert "WARNING: no datenschutz.html" in err
    assert "NOT publishable" in err
    notice = (out / "impressum" / "index.html").read_text(encoding="utf-8")
    assert "nicht veröffentlichungsfähig" in notice


def test_with_legal_texts_the_pages_carry_them_and_no_warning(
    tmp_path, monkeypatch, capsys
):
    out = tmp_path / "site"
    folder = legal_dir(tmp_path)
    monkeypatch.setattr(
        site_export, "SiteSettings", lambda: settings_of(site_legal_dir=str(folder))
    )

    assert site_export.main(["--out", str(out)]) == 0

    assert "WARNING" not in capsys.readouterr().err
    for kind in ("impressum", "datenschutz"):
        html = (out / kind / "index.html").read_text(encoding="utf-8")
        assert "Erika Beispiel" in html
        assert "nicht veröffentlichungsfähig" not in html


def test_the_example_legal_texts_are_invented():
    for name in ("impressum.html", "datenschutz.html"):
        text = (EXAMPLE / name).read_text(encoding="utf-8")
        assert "erfunden" in text
        assert set(re.findall(r"@([\w.-]+)", text)) == {"example.com"}
        assert "<script" not in text and "style=" not in text


def test_a_legal_folder_inside_the_repository_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        site_export, "SiteSettings", lambda: settings_of(site_legal_dir=str(EXAMPLE))
    )

    assert site_export.main(["--out", str(tmp_path / "site")]) == 1
    assert "inside the repository" in capsys.readouterr().err
    assert not (tmp_path / "site").exists()


def test_a_folder_that_is_no_earlier_export_is_never_emptied(tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "keep.txt").write_text("mine")

    with pytest.raises(site_export.ExportError):
        site_export.export(out, settings_of())
    assert (out / "keep.txt").read_text() == "mine"


def test_an_earlier_export_is_replaced(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())
    (out / "stale.html").write_text("<a href='/gone'>old</a>")

    site_export.export(out, settings_of())

    assert not (out / "stale.html").exists()


def test_a_built_tablet_demo_joins_the_export_with_noindex(tmp_path, monkeypatch):
    demo = tmp_path / "tablet-demo"
    (demo / "kennzahlen").mkdir(parents=True)
    (demo / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body><a href="/tablet-demo/kennzahlen/">K</a>'
        '<link rel="stylesheet" href="/static/tablet-demo.css">'
        '<script src="/static/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (demo / "kennzahlen" / "index.html").write_text(
        "<html><head></head><body></body></html>", encoding="utf-8"
    )
    (demo / "captures.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(site_app, "TABLET_DEMO", demo)
    monkeypatch.setattr(content, "DEMO_PATH", "/tablet-demo")
    out = tmp_path / "site"

    site_export.export(out, settings_of())

    assert (out / "tablet-demo" / "captures.json").is_file()
    for page in ("tablet-demo/index.html", "tablet-demo/kennzahlen/index.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert html.count('<meta name="robots" content="noindex, nofollow">') == 1, page
    assert site_export.broken_links(out) == []
    # The landing page offers the demo, and the link reaches the demo's page.
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/tablet-demo"' in landing and 'id="demo"' in landing
    # The source folder of the demo is left as it was.
    assert "robots" not in (demo / "index.html").read_text(encoding="utf-8")
    # The demo's own files of /static/ come along, the board's script too.
    assert (out / "static" / "tablet-demo.css").is_file()
    assert (out / "static" / "app.js").is_file()


def test_the_demo_screenshot_of_the_landing_page_ships_with_the_export(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(content, "DEMO_PATH", "/tablet-demo")
    out = demo_page(tmp_path, monkeypatch, "")

    site_export.export(out, settings_of())

    landing = (out / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/demo-tablet.webp"' in landing
    assert (out / "static" / "demo-tablet.webp").is_file()
    assert site_export.broken_links(out) == []


def test_a_landing_page_that_offers_the_demo_needs_it_built(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "DEMO_PATH", "/tablet-demo")

    with pytest.raises(site_export.ExportError, match=r"index\.html: /tablet-demo"):
        site_export.export(tmp_path / "site", settings_of())


def demo_page(tmp_path, monkeypatch, body: str) -> Path:
    demo = tmp_path / "tablet-demo"
    demo.mkdir()
    (demo / "index.html").write_text(
        f"<html><head></head><body>{body}</body></html>", encoding="utf-8"
    )
    monkeypatch.setattr(site_app, "TABLET_DEMO", demo)
    return tmp_path / "site"


@pytest.mark.parametrize(
    "form",
    [
        '<form class="call-filter" method="get" action="/gui/anrufe"></form>',
        '<form method="get" action="/gui/menue" hx-get="/gui/menue"></form>',
        '<form method="post" action="/gui/inhaber/schliessen"></form>',
        '<form method="post" action="/gui/bestellungen/7/passt"></form>',
    ],
)
def test_a_form_the_demo_answers_is_no_broken_link(tmp_path, monkeypatch, form):
    out = demo_page(tmp_path, monkeypatch, form)

    site_export.export(out, settings_of())

    assert site_export.broken_links(out) == []


@pytest.mark.parametrize(
    ("body", "link"),
    [
        # A route the demo marks absent, or none at all, is a broken form.
        ('<form action="/gui/anmelden"></form>', "/gui/anmelden"),
        ('<form action="/gui/nirgends"></form>', "/gui/nirgends"),
        # Only a form action may name a route; a link or a source stays a file.
        ('<a href="/gui/anrufe">x</a>', "/gui/anrufe"),
        ('<img src="/gui/menue">', "/gui/menue"),
    ],
)
def test_the_demo_exception_is_only_for_form_actions_it_answers(
    tmp_path, monkeypatch, body, link
):
    out = demo_page(tmp_path, monkeypatch, body)

    with pytest.raises(site_export.ExportError, match="broken links"):
        site_export.export(out, settings_of())
    assert site_export.broken_links(out) == [f"tablet-demo/index.html: {link}"]


def test_a_form_action_outside_the_demo_is_still_checked(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())
    landing = out / "index.html"
    landing.write_text(
        landing.read_text(encoding="utf-8")
        + '<form action="/gui/inhaber/schliessen"></form>',
        encoding="utf-8",
    )

    assert site_export.broken_links(out) == ["index.html: /gui/inhaber/schliessen"]


def test_the_export_carries_only_the_static_files_its_pages_need(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())

    shipped = {
        p.relative_to(out / "static").as_posix()
        for p in (out / "static").rglob("*")
        if p.is_file()
    }
    fonts = {
        f"fonts/ibm-plex-sans-latin-{w}-normal.woff2" for w in (400, 500, 600, 700)
    }
    assert shipped == {
        "site.css",
        "site.js",
        "site-logic.js",
        "anfrage.js",
        "icon-192.png",
        "fonts/IBM-Plex-Sans-OFL.txt",
        *fonts,
    }
    # The tablet application's files stay out while no demo is built.
    for name in (
        "app.js",
        "app.css",
        "htmx.min.js",
        "passkey.js",
        "dev-console.js",
        "manifest.json",
    ):
        assert name not in shipped


def test_the_404_page_is_a_page_of_the_site(tmp_path):
    out = tmp_path / "site"
    site_export.export(out, settings_of())

    html = (out / "404.html").read_text(encoding="utf-8")
    assert "Diese Seite gibt es nicht." in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert 'href="/"' in html


@pytest.mark.parametrize("preview_on", [True, False])
def test_the_htaccess_sends_the_headers_and_names_the_404_page(tmp_path, preview_on):
    out = tmp_path / "site"
    site_export.export(out, settings_of(site_preview=preview_on))

    text = (out / ".htaccess").read_text(encoding="utf-8")
    assert "ErrorDocument 404 /404.html" in text
    assert f'Header always set Content-Security-Policy "{site_app.CSP}"' in text
    assert "frame-ancestors 'none'" in text and "unsafe-inline" not in text
    assert 'Header always set X-Content-Type-Options "nosniff"' in text
    assert 'Header always set Referrer-Policy "no-referrer"' in text
    assert ('X-Robots-Tag "noindex, nofollow"' in text) is preview_on
    # Directives a shared host may forbid in .htaccess (a 500 for every page).
    directives = [line.split()[0] for line in text.splitlines() if line.strip()]
    assert "Options" not in directives and "DirectoryIndex" not in directives
    assert "<IfModule mod_headers.c>" in text
    # www to the apex, 301, before every other rule, with no host name in it; the
    # host sets HSTS itself, so the file sends none.
    lines = text.splitlines()
    assert lines.index("  RewriteEngine On") < lines.index(
        "ErrorDocument 404 /404.html"
    )
    assert r"  RewriteCond %{HTTP_HOST} ^www\.(.+)$ [NC]" in lines
    assert "  RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]" in lines
    assert "Strict-Transport-Security" not in text


def test_the_zip_holds_the_folder_without_the_marker(tmp_path, monkeypatch):
    import zipfile

    out = tmp_path / "site"
    monkeypatch.setattr(site_export, "SiteSettings", lambda: settings_of())

    assert site_export.main(["--out", str(out), "--zip"]) == 0

    with zipfile.ZipFile(tmp_path / "site.zip") as packed:
        names = set(packed.namelist())
    assert {
        "index.html",
        "anfrage/index.html",
        ".htaccess",
        "404.html",
        "robots.txt",
    } <= names
    assert site_export.MARK not in names
    assert "static/site.css" in names
