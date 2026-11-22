"""HTTP side of the public site (#892, docs/06 §10, docs/13 §9).

    uvicorn api.site.app:app --port 8003

Four pages and nothing else: `/` the landing page, `/anfrage` the inquiry form,
`/impressum` and `/datenschutz` (from SITE_LEGAL_DIR, else placeholders), plus
`/robots.txt`. `scripts/site_export.py` writes the same answers as plain files
for a static host. No database, no cookie, no
session, no write: a visitor can read these pages and nothing more, and the
customer application and the operator dashboard have hosts of their own. The
CSP names this site only (no outside font, no inline script or style), so the
page's own files are all a browser may load.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.site import content, waves
from api.site.config import SiteSettings

HERE = Path(__file__).resolve().parent
# The compiled scripts, the stylesheet and the fonts sit beside those of the
# operations view, as the dashboard's stylesheet does.
STATIC = HERE.parent / "gui" / "static"
TABLET_DEMO = HERE / "tablet-demo"

CSP_META = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'"
)
# The header adds what a <meta> policy may not carry (a browser ignores
# frame-ancestors there and says so in the console). The pages carry CSP_META
# too, so the policy travels with them to a host that serves plain files.
CSP = f"{CSP_META}; frame-ancestors 'none'"
# Crawling stays allowed on purpose: a crawler that robots.txt shuts out never
# reads the page, so it never sees the noindex and may still list the address
# from links elsewhere. No sitemap.
ROBOTS = (
    "# Crawling is allowed on purpose: every page says itself whether it may be\n"
    "# indexed (meta robots), and a crawler that is shut out never reads that.\n"
    "User-agent: *\n"
    "Allow: /\n"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The browser asks again on every use, so a page of a newer build never runs
    # a script of an older one; the ETag keeps it to a 304.
    "Cache-Control": "no-cache",
}

templates = Jinja2Templates(directory=str(HERE / "templates"))


def create_app(settings: SiteSettings | None = None) -> FastAPI:
    config = settings or SiteSettings()
    app = FastAPI(
        title="Maex public site", docs_url=None, redoc_url=None, openapi_url=None
    )

    def page(request: Request, name: str, **extra: Any) -> Response:
        return templates.TemplateResponse(
            request,
            name,
            {
                "preview": config.site_preview,
                "csp": CSP_META,
                "login_url": config.site_login_url,
                "contact_email": config.site_contact_email,
                "inquiry_action": (
                    config.site_inquiry_action if config.inquiry_open else ""
                ),
                "demo_path": content.DEMO_PATH,
                "calls": content.CALLS,
                "kinds": content.KINDS,
                "waves": waves.paths(),
                **extra,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> Response:
        return page(request, "landing.html")

    @app.get("/anfrage", response_class=HTMLResponse)
    def inquiry(request: Request) -> Response:
        return page(request, "anfrage.html")

    def legal(request: Request, title: str, kind: str) -> Response:
        text = config.legal_text(kind)
        return page(request, "legal.html", title=title, kind=kind, legal_text=text)

    @app.get("/impressum", response_class=HTMLResponse)
    def legal_notice(request: Request) -> Response:
        return legal(request, "Impressum", "impressum")

    @app.get("/datenschutz", response_class=HTMLResponse)
    def privacy(request: Request) -> Response:
        return legal(request, "Datenschutz", "datenschutz")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return ROBOTS

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Response:
        # A page of the site, also what a static host shows (404.html).
        response = page(request, "notfound.html")
        response.status_code = 404
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    # The tablet demo, rendered by scripts/demo/tablet_demo_build.py into a
    # folder that is not committed. Not built when the site starts: the address
    # answers 404, and the landing page does not link it while
    # content.DEMO_PATH is None.
    if TABLET_DEMO.is_dir():
        app.mount(
            "/tablet-demo",
            StaticFiles(directory=str(TABLET_DEMO), html=True),
            name="tablet-demo",
        )

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers[name] = value
        if config.site_preview:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    return app


app = create_app()
