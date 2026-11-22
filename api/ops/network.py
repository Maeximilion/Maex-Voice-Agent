"""The ops API answers only over the stack's `ops` network (#888 C4, docs/23 §8).

The api also stands on `backend` and `edge`, where the dispatcher, `voice`, n8n
and the push server reach it; without this `OPS_TOKEN` would be the only
barrier there. A connection's local address tells the network it came over:
Docker gives the container one address per network. `OPS_NETWORK_HOST` names
the alias the api has on `ops` alone (deploy/docker-compose.customer.yml), and
Docker's DNS answers that name on its own network only. Over any other network
`/v1/ops` answers as a path that does not exist. Empty, as on the workbench and
in the single-customer layout, which have no `ops` network: not checked.
"""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from api.config import settings
from api.core import envelope

PREFIX = "/v1/ops"


async def addresses(host: str) -> set[str]:
    """The addresses of `host`, empty when it does not resolve. Asked on every
    request, not kept: the api's address changes when it joins `ops` again."""
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return set()
    return {str(info[4][0]) for info in found}


async def ops_network_only(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    host = settings.ops_network_host
    if host and request.url.path.startswith(PREFIX):
        server = request.scope.get("server")
        if not server or server[0] not in await addresses(host):
            # The framework's own answer to an unknown path (api/main.py).
            return envelope.fail("not_found", "Not Found", status=404)
    return await call_next(request)
