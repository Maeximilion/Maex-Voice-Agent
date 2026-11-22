"""The ops API answers only on the stack's `ops` network (#888 C4, docs/23 §8).

The api stands on `backend` and `edge` too, where the dispatcher, `voice`, n8n
and the push server reach it; there `/v1/ops` is a path that does not exist.
Which network a request came over is the local address of its connection
(`scope["server"]`), compared with the api's own address under the alias it has
on `ops` alone. That Docker resolves such an alias on its own network only, and
that the local address differs per network, was tried on the workbench's
Docker with two internal networks (22.11.2026). Here the TestClient's local
address is `testserver`.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app
from api.ops import network

OPS = "f" * 64


@pytest.fixture
def ops_on(monkeypatch):
    monkeypatch.setattr(settings, "ops_token", OPS)
    monkeypatch.setattr(settings, "ops_network_host", "ops-api")
    found: dict[str, set[str]] = {"ops-api": {"testserver"}}

    async def addresses(host: str) -> set[str]:
        return found.get(host, set())

    monkeypatch.setattr(network, "addresses", addresses)
    return found


def _status(client: TestClient):
    return client.get("/v1/ops/status", headers={"Authorization": "Bearer nope"})


def test_a_request_over_the_ops_network_reaches_the_token_check(ops_on):
    answer = _status(TestClient(app))
    assert answer.status_code == 401


def test_a_request_over_another_network_finds_no_such_path(ops_on):
    ops_on["ops-api"] = {"192.168.16.3"}
    client = TestClient(app)
    answer = _status(client)
    assert answer.status_code == 404
    # The same answer as a path that does not exist: nothing tells it is there.
    unknown = client.get("/v1/nothing-here")
    assert answer.json() == unknown.json()
    # A body the parser refuses is turned away before the token check too.
    broken = client.post(
        "/v1/ops/access/pin",
        content=b"{",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPS}"},
    )
    assert broken.status_code == 404


def test_a_name_that_does_not_resolve_closes_the_ops_api(ops_on):
    ops_on.clear()
    assert _status(TestClient(app)).status_code == 404


def test_other_paths_and_an_unset_host_are_not_touched(ops_on, monkeypatch):
    ops_on["ops-api"] = {"192.168.16.3"}
    assert TestClient(app).get("/health").status_code == 200
    # The workbench and the single-customer layout have no `ops` network.
    monkeypatch.setattr(settings, "ops_network_host", "")
    assert _status(TestClient(app)).status_code == 401


def test_addresses_of_a_name_that_does_not_resolve_are_empty():
    assert asyncio.run(network.addresses("no-such-host.invalid")) == set()
