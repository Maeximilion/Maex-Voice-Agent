"""HTTP side of the public site (#892, docs/06 §10, docs/13 §9).

    uvicorn api.site.app:app --port 8003

Four pages and nothing else: `/` the landing page, `/anfrage` the inquiry form,
`/impressum` and `/datenschutz`, still placeholders. No database, no cookie, no
session, no write: a visitor can read these pages and nothing more, and the
customer application and the operator dashboard have hosts of their own. The
CSP names this site only (no outside font, no inline script or style), so the
page's own files are all a browser may load.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.site import content, waves
from api.site.config import SiteSettings

HERE = Path(__file__).resolve().parent
# The compiled scripts, the stylesheet and the fonts sit beside those of the
# operations view, as the dashboard's stylesheet does.
STATIC = HERE.parent / "gui" / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
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
                "login_url": config.site_login_url,
                "contact_email": config.site_contact_email,
                "inquiry_action": config.site_inquiry_action,
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

    @app.get("/impressum", response_class=HTMLResponse)
    def legal_notice(request: Request) -> Response:
        return page(request, "legal.html", title="Impressum", kind="impressum")

    @app.get("/datenschutz", response_class=HTMLResponse)
    def privacy(request: Request) -> Response:
        return page(request, "legal.html", title="Datenschutz", kind="datenschutz")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers[name] = value
        return response

    return app


app = create_app()
