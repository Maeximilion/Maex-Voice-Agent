"""Whether a browser request comes from a page of the site it is sent to.

Shared by the operations view (`gui/session.py`) and the operator dashboard
(`dashboard/web.py`); it reads no settings.
"""

from urllib.parse import urlsplit

from fastapi import Request


def same_origin(request: Request) -> bool:
    """A change comes from a page of this site (docs/23 §6, T9).

    A browser names the page's origin on every cross-site write; any other
    host is refused. A client that sends neither header is no browser, so no
    cross-site request; the CSRF token still applies to it.

    "null" needs a second look: a browser sends it with a plain form post of
    this very site when the page's referrer policy is `no-referrer` (Fetch,
    "append a request `Origin` header"), which the proxy sets, and also from a
    sandboxed frame or after a cross-site redirect. `Sec-Fetch-Site` tells the
    two apart, and no page can set or change it: only `same-origin` passes. A
    browser too old to send that header is refused."""
    origin = request.headers.get("origin")
    site = request.headers.get("sec-fetch-site")
    if origin == "null":
        return site == "same-origin"
    if origin is not None:
        return urlsplit(origin).netloc == request.headers.get("host", "")
    return site in (None, "same-origin", "none")
