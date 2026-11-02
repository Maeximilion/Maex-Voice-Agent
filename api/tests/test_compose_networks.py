"""Who stands on which Docker network (T-9.8, docs/13 §3).

Asterisk takes outside input, so of the containers it reaches `voice` alone, and
Caddy's published ports as the internet does. Caddy stands on the edge and never
sees the database. The database, the API and
the dispatcher sit on internal networks, without a way out of the server. These
promises live in the Compose files and nowhere in the code, and a service that
loses its `networks:` line lands on `default` without any error. So a service
on another network than the one named here fails, a new service too.

Nothing is started here: no docker CLI in the test container. The merge rule is
the one of test_deploy_exposure.py (a list is joined with the base's), compared
with `docker compose config` on the workbench when this was written. That the
networks hold on the wire, and that a cut one loses no call, is what the drills
"networks" and "cut off" show (`python3 -m scripts.voice_drill`).
"""

import pytest

from api.tests.test_compose_ports import (
    BASE,
    DEV,
    LINE,
    PROD,
    TUNNEL,
    _load,
    _parse,
    _services,
    _Tagged,
)
from api.tests.test_deploy_exposure import _merged

DRILL = LINE.parent / "docker-compose.drill.yml"

# Production: every service, and the one reason each network is there for it.
PRODUCTION = {
    "db": {"backend"},
    # At the database, and posts to n8n and the push server.
    "dispatcher": {"backend", "edge"},
    # Behind Caddy, and at the database.
    "api": {"backend", "edge"},
    # Behind Caddy. Neither needs the database, and n8n is third-party code.
    "n8n": {"edge"},
    "push": {"edge"},
    # Asterisk on one side, the database on the other, and its way out.
    "voice": {"backend", "telephony"},
    "asterisk": {"telephony"},
    "caddy": {"edge", "public"},
}
# What keeps a way out of the server: `public` for the certificates and the
# published ports, `telephony` for SIP, the model and the speech services.
INTERNAL = {"backend", "edge"}
WITH_A_WAY_OUT = {"public", "telephony"}
# Added by the production file, for Caddy, which the workbench does not run.
PRODUCTION_ONLY = {"public"}


def _networks_of(service: dict) -> set[str]:
    """The networks a service stands on. No key at all is `default`, where a
    forgotten service lands; `network_mode` is checked on its own."""
    networks = service.get("networks", ["default"])
    return set(networks)


def _production_networks() -> dict[str, set[str]]:
    names = _services(_load(BASE)) | _services(_load(PROD))
    return {name: _networks_of(_merged(name)) for name in names}


def _definitions(*documents: dict) -> dict[str, dict]:
    """The top-level networks after the files were merged, by name."""
    merged: dict[str, dict] = {}
    for document in documents:
        for name, definition in (document.get("networks") or {}).items():
            merged.setdefault(name, {}).update(definition or {})
    return merged


def _internal(definitions: dict[str, dict]) -> set[str]:
    return {name for name, value in definitions.items() if value.get("internal")}


def _wrong(actual: dict[str, set[str]], expected: dict[str, set[str]]) -> list[str]:
    return [
        f"{name}: on {sorted(actual.get(name, set()))}, "
        f"belongs on {sorted(expected.get(name, set()))}"
        for name in sorted(actual | expected)
        if actual.get(name) != expected.get(name)
    ]


def test_every_production_service_stands_where_it_belongs():
    wrong = _wrong(_production_networks(), PRODUCTION)
    assert not wrong, wrong


def test_the_workbench_has_the_same_services_on_the_same_networks():
    """Without what only the proxy needs, so a test call runs on the server's
    layout. Not its walls: a published dev port is open across the networks by
    address, even on loopback (measured 02.11.2026, docs/13 §3)."""
    base = {
        name: _networks_of(service) for name, service in _services(_load(BASE)).items()
    }
    expected = {
        name: networks - PRODUCTION_ONLY
        for name, networks in PRODUCTION.items()
        if name != "caddy"
    }
    wrong = _wrong(base, expected)
    assert not wrong, wrong


def test_asterisk_shares_a_network_with_voice_alone():
    networks = _production_networks()
    neighbours = {name for name, on in networks.items() if on & networks["asterisk"]}
    assert neighbours == {"asterisk", "voice"}


def test_only_our_own_code_shares_a_network_with_the_database():
    networks = _production_networks()
    neighbours = {name for name, on in networks.items() if on & networks["db"]}
    assert neighbours == {"db", "api", "dispatcher", "voice"}


def test_caddy_shares_no_network_with_the_database():
    networks = _production_networks()
    assert not networks["caddy"] & networks["db"]
    assert networks["caddy"] & networks["api"]


def test_what_a_service_calls_is_on_a_network_it_shares():
    """n8n posts to the push server, the dispatcher to n8n, the push server and
    `voice`, Caddy passes on to three, `voice` and Asterisk find each other. A
    wall in one of these places is an outage, not a hardening."""
    calls = {
        "api": {"db"},
        "dispatcher": {"db", "n8n", "push", "voice"},
        "n8n": {"push"},
        "voice": {"db"},
        "asterisk": {"voice"},
        "caddy": {"api", "n8n", "push"},
    }
    networks = _production_networks()
    cut = [
        f"{caller} -> {target}"
        for caller, targets in calls.items()
        for target in sorted(targets)
        if not networks[caller] & networks[target]
    ]
    assert not cut, cut


def test_production_makes_the_right_networks_internal():
    definitions = _definitions(_load(BASE), _load(PROD))
    assert set(definitions) == INTERNAL | WITH_A_WAY_OUT
    assert _internal(definitions) == INTERNAL


def test_who_has_a_way_out_in_production():
    """A container on any plain network leaves through it, so this list is the
    whole of what can reach the internet: `internal` on both others means
    nothing here without it."""
    leaving = {
        name for name, on in _production_networks().items() if on & WITH_A_WAY_OUT
    }
    assert leaving == {"asterisk", "caddy", "voice"}


def test_the_workbench_keeps_its_ports_and_a_model_on_the_host():
    """Docker publishes no port of a container that is only on an internal
    network, and such a container has no `host.docker.internal` (measured
    02.11.2026, Docker Desktop 29.9.2): `internal` in the base file would close
    the dev ports of docker-compose.override.yml without an error."""
    published = set(_services(_load(DEV)))
    assert published == {"db", "api", "n8n", "push"}
    assert not _internal(_definitions(_load(BASE)))


def test_no_production_service_shares_another_containers_network():
    """`network_mode` takes a service off the networks checked here. The tunnel
    of the test phase does so on purpose, in its own file."""
    for name in _production_networks():
        assert "network_mode" not in _merged(name), name


def test_the_drills_run_on_the_production_layout():
    """The drill file has its own services, so it has its own list: the outage
    map would otherwise be proven on walls production does not have."""
    drill = _load(DRILL)
    expected = {
        "db": PRODUCTION["db"],
        "voice": PRODUCTION["voice"],
        # The stand-in for the router stands outside, past Asterisk.
        "router": PRODUCTION["asterisk"],
        "setup": {"backend"},
    }
    services = _services(drill)
    wrong = _wrong({name: _networks_of(services[name]) for name in expected}, expected)
    assert not wrong, wrong
    # Taken from the base file by `extends`: nothing of its own here.
    assert "networks" not in services["asterisk"]
    assert services["asterisk"]["extends"]["service"] == "asterisk"
    assert _internal(_definitions(drill)) == {"backend"}
    assert set(_definitions(drill)) == {"backend", "telephony"}


@pytest.mark.parametrize("path", [LINE, TUNNEL])
def test_asterisk_in_the_tunnel_gives_up_its_network(path):
    """Docker refuses `network_mode` next to `networks`, and the tunnel files
    inherit the base file's list, so each has to take it away."""
    asterisk = _services(_load(path))["asterisk"]
    assert asterisk["network_mode"] == "service:wireguard"
    reset = asterisk["networks"]
    assert isinstance(reset, _Tagged) and reset.tag == "!reset"


def test_the_tunnel_stands_where_asterisk_finds_voice():
    """Asterisk shares the network of `wireguard` in the overlay of the product's
    stack: without `telephony` there, `voice` has no name for it and every call
    goes to the team."""
    assert _networks_of(_services(_load(TUNNEL))["wireguard"]) == PRODUCTION["asterisk"]


def test_a_service_without_a_networks_line_is_on_default():
    assert _networks_of({}) == {"default"}
    assert _networks_of({"networks": ["backend"]}) == {"backend"}


def test_the_comparison_names_a_service_on_the_wrong_network():
    expected = {"asterisk": {"telephony"}}
    assert not _wrong({"asterisk": {"telephony"}}, expected)
    assert _wrong({"asterisk": {"telephony", "backend"}}, expected)
    assert _wrong({"asterisk": {"default"}}, expected)
    # A new service nobody placed, and one that went away.
    assert _wrong({"asterisk": {"telephony"}, "new": {"backend"}}, expected)
    assert _wrong({}, expected)


def test_an_internal_flag_of_a_later_file_is_read():
    base = _parse("networks:\n  backend:\n")
    over = _parse("networks:\n  backend:\n    internal: true\n")
    assert _internal(_definitions(base)) == set()
    assert _internal(_definitions(base, over)) == {"backend"}
