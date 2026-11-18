"""The token check `main._TOKEN_CHECKS` runs for a body FastAPI cannot parse
must be the one the prefix's routes carry as a dependency (#853): two lists to
keep in step, and a drift would send a stranger's `{` to `invalid_input`
instead of 401, or the wrong token to the right route.
"""

import pytest

from api import main
from api.tests import test_ops_status as base

http = base.http
session = base.session
tenant = base.tenant


def _flat(routes) -> list:
    """FastAPI 0.16x keeps an included router as a lazy node whose
    `effective_candidates()` are the routes with the router's own dependencies
    and prefix applied; older versions flatten them into `app.routes`."""
    flat = []
    for route in routes:
        if hasattr(route, "effective_candidates"):
            flat += _flat(route.effective_candidates())
        else:
            flat.append(route)
    return flat


def _routes_under(prefix: str) -> list:
    return [
        route
        for route in _flat(main.app.routes)
        if getattr(route, "dependant", None) and route.path.startswith(prefix)
    ]


@pytest.mark.parametrize(("prefix", "check"), main._TOKEN_CHECKS)
def test_every_route_of_a_prefix_carries_the_check_the_prefix_names(prefix, check):
    routes = _routes_under(prefix)
    assert routes, f"no route under {prefix}"
    for route in routes:
        carried = {dep.call for dep in route.dependant.dependencies}
        assert check in carried, f"{route.path} does not carry {check.__name__}"


@pytest.mark.parametrize(
    "path", ["/v1/kitchen/claim", "/v1/kitchen/ack", "/v1/calls/start"]
)
@pytest.mark.parametrize("token", [None, "Bearer " + "e" * 64])
def test_a_body_that_cannot_be_read_is_refused_by_the_token_first(http, path, token):
    """`/v1/calls/` exists in dev and test only (`add_local_routes`)."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    answer = http.post(path, content=b"{", headers=headers)
    assert answer.status_code == 401
    assert answer.json()["error"]["message"] == "unauthorized"
