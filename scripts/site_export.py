"""Write the public site as plain files for any static host (#892, docs/13 §9).

    python -m scripts.site_export [--out site-export] [--zip]

Asks the site app (`api/site/app.py`) for every page with the configuration of
this environment (`SITE_*`, `.env`) and writes the answers into one folder:
`index.html`, `anfrage/index.html`, `impressum/index.html`,
`datenschutz/index.html`, `404.html`, `robots.txt`, an Apache `.htaccess` with
the headers the app sends, the files of `/static/` the pages need, and the
tablet demo under `tablet-demo/` when `scripts/demo/tablet_demo_build.py` has
built it. `--zip` also writes `<folder>.zip` for the upload.
Links stay root-absolute (`/static/site.css`, `/anfrage/`), so the folder is
served from the root of a host name. Every link of the written pages is checked
against the folder; a broken one stops the export.

The legal notice and the privacy text come from `SITE_LEGAL_DIR`, a folder
outside the repository (`deploy/site/legal.example/` shows the form). Without
them the placeholders are written and the export says so loudly: such a folder
is a preview, not publishable.

Nothing is deployed or uploaded; the folder is the whole result.
"""

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from api.site import app as site_app
from api.site.config import SiteSettings
from scripts.demo.tablet_demo_routes import manifest, pattern

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site-export"
# Written into every export, so a later run only ever empties its own folder.
MARK = ".site-export"
PAGES = {
    "/": "index.html",
    "/anfrage": "anfrage/index.html",
    "/impressum": "impressum/index.html",
    "/datenschutz": "datenschutz/index.html",
}
LEGAL = ("impressum", "datenschutz")
# A link to a page of the site gets its trailing slash, the clean path a static
# host serves `<page>/index.html` at without a redirect.
PAGE_LINK = re.compile(r'((?:href|action)=")/(anfrage|impressum|datenschutz)(?=[?#"])')
LINK = re.compile(r'(?P<attr>href|src|action)="(?P<link>/[^"]*)"')
CSS_URL = re.compile(r"url\(\s*['\"]?(/[^)'\"]+)")
# A file of /static/ named by a page, a stylesheet, a script or a manifest, and
# the relative imports of a compiled script (`from "./site-logic.js"`).
STATIC_REF = re.compile(r"/static/([\w.-]+(?:/[\w.-]+)*)")
JS_IMPORT = re.compile(r"""(?:\bfrom|\bimport)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")
SCANNED = (".html", ".css", ".js", ".json")
# The fonts are under the SIL OFL, which travels with every copy of them.
FONT_LICENCE = "fonts/IBM-Plex-Sans-OFL.txt"
ROBOTS_META = re.compile(r'<meta\s+name="robots"', re.I)
NOINDEX = '<meta name="robots" content="noindex, nofollow">'


class ExportError(Exception):
    pass


def legal_missing(settings: SiteSettings) -> list[str]:
    """The legal pages that will be placeholders. Refuses a folder inside the
    repository: those texts hold private details and must never be committed."""
    if settings.site_legal_dir:
        folder = Path(settings.site_legal_dir).resolve()
        if folder == ROOT or ROOT in folder.parents:
            raise ExportError(
                f"SITE_LEGAL_DIR {folder} lies inside the repository; keep the "
                "legal texts outside it"
            )
    return [kind for kind in LEGAL if settings.legal_text(kind) is None]


def prepare(out: Path) -> None:
    """Empties `out` when it is an earlier export; refuses any other folder."""
    if out.exists():
        if any(out.iterdir()) and not (out / MARK).is_file():
            raise ExportError(f"{out} is not empty and no earlier export; not touched")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def target(out: Path, link: str) -> Path | None:
    """The file a root-absolute link reaches in the folder, None when none does."""
    # A `//host/...` link keeps a host name as its first part, so no file
    # matches it and it counts as broken: the site links nowhere outside.
    rel = link.split("#", 1)[0].split("?", 1)[0].removeprefix("/")
    if not rel or rel.endswith("/"):
        candidates = [out / rel / "index.html"]
    else:
        candidates = [out / rel, out / rel / "index.html"]
    return next((c for c in candidates if c.is_file()), None)


def answered_by_the_demo(link: str) -> bool:
    """Whether a form `action` inside the tablet demo is one no click can send to
    the host: a route of `tablet_demo_routes.json` that the demo does not mark
    absent. The page's own submit is answered in the browser (the call filter by
    `filters()` and the menu search by htmx, both in api/gui/ts/demo-backend.ts),
    or cannot happen: the forms of the PIN-locked pages have every control
    disabled (`lock()` in scripts/demo/tablet_demo_build.py, tested by
    `test_the_owners_pages_are_locked_and_say_so`)."""
    path = link.split("#", 1)[0].split("?", 1)[0]
    return any(
        route["answer"] != "absent" and pattern(route["path"]).fullmatch(path)
        for route in manifest()["routes"]
    )


def broken_links(out: Path) -> list[str]:
    """Every root-absolute link of a written page or stylesheet that reaches no
    file of the folder, as `<file>: <link>`. Only a form `action` inside
    `tablet-demo/` may name a route the demo answers instead of a file."""
    broken = []
    for file in sorted(out.rglob("*")):
        rel = file.relative_to(out)
        in_demo = rel.parts[0] == "tablet-demo"
        if file.suffix == ".html":
            text = file.read_text(encoding="utf-8")
            links = [
                m["link"]
                for m in LINK.finditer(text)
                if not (
                    in_demo
                    and m["attr"] == "action"
                    and answered_by_the_demo(m["link"])
                )
            ]
        elif file.suffix == ".css":
            links = CSS_URL.findall(file.read_text(encoding="utf-8"))
        else:
            continue
        for link in links:
            if target(out, link) is None:
                broken.append(f"{rel.as_posix()}: {link}")
    return broken


def copy_static(out: Path) -> None:
    """Copies the files of /static/ the written pages need, and what those files
    need in turn, nothing else: `api/gui/static/` also holds the tablet
    application's files, which only the tablet demo loads."""
    todo = [f for f in out.rglob("*") if f.suffix in (".html", ".json")]
    copied: set[str] = set()
    while todo:
        file = todo.pop()
        text = file.read_text(encoding="utf-8")
        names = STATIC_REF.findall(text)
        if file.suffix == ".js":
            here = file.parent.relative_to(out / "static")
            names += [(here / ref).as_posix() for ref in JS_IMPORT.findall(text)]
        if any(name.startswith("fonts/") for name in names):
            names.append(FONT_LICENCE)
        for name in names:
            name = Path(name).as_posix().removeprefix("./")
            source = site_app.STATIC / name
            if name in copied or ".." in name or not source.is_file():
                continue
            copied.add(name)
            target_file = out / "static" / name
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target_file)
            if target_file.suffix in SCANNED:
                todo.append(target_file)


def mark_noindex(folder: Path) -> None:
    """Gives every page of a copied folder the preview's robots tag where it has
    none, so no page of the export can be indexed."""
    for file in folder.rglob("*.html"):
        html = file.read_text(encoding="utf-8")
        if not ROBOTS_META.search(html):
            html = re.sub(
                r"<head\b[^>]*>", lambda m: f"{m.group(0)}\n{NOINDEX}", html, count=1
            )
            file.write_text(html, encoding="utf-8")


def htaccess(settings: SiteSettings) -> str:
    """What an Apache host (netcup webhosting, Plesk) needs beside the files:
    the headers the app would send, the `www` redirect and the 404 page. Only
    directives of the
    FileInfo class, which a shared host allows in .htaccess; `Options` and
    `DirectoryIndex` are left out because a host that forbids them answers 500.
    Apache's DirectorySlash already sends `/anfrage` to `/anfrage/`."""
    headers = [
        ("Content-Security-Policy", site_app.CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-cache"),
    ]
    if settings.site_preview:
        headers.append(("X-Robots-Tag", "noindex, nofollow"))
    lines = [
        "# Written by scripts/site_export.py (#892, docs/13 §9); do not edit here.",
        # www leads to the apex with 301: the panel's "preferred domain" is locked
        # on this host. No host name here, so any domain works; the query string
        # is kept by default. HSTS is the host's own setting, not sent here.
        "<IfModule mod_rewrite.c>",
        "  RewriteEngine On",
        r"  RewriteCond %{HTTP_HOST} ^www\.(.+)$ [NC]",
        "  RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]",
        "</IfModule>",
        "ErrorDocument 404 /404.html",
        "<IfModule mod_headers.c>",
        *(f'  Header always set {name} "{value}"' for name, value in headers),
        "</IfModule>",
    ]
    return "\n".join(lines) + "\n"


def export(out: Path, settings: SiteSettings) -> list[str]:
    """Writes the site into `out`; returns the legal pages left as placeholders."""
    missing = legal_missing(settings)
    prepare(out)
    client = TestClient(site_app.create_app(settings))
    for path, name in PAGES.items():
        response = client.get(path)
        if response.status_code != 200:
            raise ExportError(f"{path} answered {response.status_code}")
        file = out / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(PAGE_LINK.sub(r"\1/\2/", response.text), encoding="utf-8")
    (out / "robots.txt").write_text(client.get("/robots.txt").text, encoding="utf-8")
    missing_page = client.get("/404")
    if missing_page.status_code != 404:
        raise ExportError(f"/404 answered {missing_page.status_code}")
    (out / "404.html").write_text(
        PAGE_LINK.sub(r"\1/\2/", missing_page.text), encoding="utf-8"
    )
    (out / ".htaccess").write_text(htaccess(settings), encoding="utf-8")
    if site_app.TABLET_DEMO.is_dir():
        shutil.copytree(site_app.TABLET_DEMO, out / "tablet-demo")
        if settings.site_preview:
            mark_noindex(out / "tablet-demo")
    copy_static(out)
    (out / MARK).write_text("written by scripts/site_export.py\n", encoding="utf-8")
    broken = broken_links(out)
    if broken:
        raise ExportError("broken links:\n  " + "\n  ".join(broken))
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the public site as plain files."
    )
    parser.add_argument("--out", type=Path, default=OUT, help="the export folder")
    parser.add_argument(
        "--zip", action="store_true", help="also write <out>.zip beside the folder"
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    try:
        missing = export(out, SiteSettings())
    except ExportError as error:
        print(f"site export failed: {error}", file=sys.stderr)
        return 1
    if args.zip:
        # The marker stays local: the zip is what gets uploaded.
        archive = out.with_name(f"{out.name}.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as packed:
            for file in sorted(out.rglob("*")):
                if file.is_file() and file.name != MARK:
                    packed.write(file, file.relative_to(out).as_posix())
        print(f"zip written to {archive}")
    for kind in missing:
        print(
            f"WARNING: no {kind}.html in SITE_LEGAL_DIR: /{kind}/ is the "
            "placeholder. This export is NOT publishable.",
            file=sys.stderr,
        )
    print(f"site written to {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
