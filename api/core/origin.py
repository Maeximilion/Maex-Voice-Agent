"""Whether a browser request comes from a page of the site it is sent to.

Shared by the operations view (`gui/session.py`) and the operator dashboard
(`dashboard/web.py`); it reads no settings.
"""

from urllib.parse import urlsplit

from fastapi import Request


def same_origin(request: Request) -> bool:
    """A change comes from a page of this site (docs/23 §6, T9).

    A browser names the page's origin on every cross-site write; "null" and any
    other host are refused. A client that sends neither header is no browser,
    so no cross-site request; the CSRF token still applies to it."""
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin).netloc == request.headers.get("host", "")
    return request.headers.get("sec-fetch-site") in (None, "same-origin", "none")
