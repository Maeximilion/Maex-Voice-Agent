"""Caddy and the push server run as a user of their own, not root (#888 S10).

Both images start as root and neither drops it. Each service now names a uid,
and a one-shot `<service>-chown` before it gives that uid the service's
volumes: a volume written by root on an existing host (Caddy's certificates,
the push server's cache and user database) would otherwise stop the service at
its next start, and a fresh push volume is created owned by root. The chown
runs on every `up` and is a no-op once the owner is right.

Measured with Docker 29.9.3 on 23.11.2026 (docs/13 §3): Caddy as uid 10002 with
`no-new-privileges`, every capability dropped but `NET_BIND_SERVICE`, binds 443
even with `net.ipv4.ip_unprivileged_port_start` raised to 1024; without the
capability it fails there with "permission denied". `chown -R` with only
`CHOWN` passes through a tree of 0700 directories and 0600 keys.

Nothing is started here; the files are read as test_compose_ports.py does.
"""

import pytest

from api.tests.test_compose_ports import BASE, REPO_ROOT, _load, _services
from api.tests.test_deploy_exposure import _merged

SHARED_CADDY = REPO_ROOT / "deploy" / "caddy" / "docker-compose.yml"
OPERATOR = REPO_ROOT / "deploy" / "docker-compose.operator.yml"

# A uid of each its own, apart from 10001 (api, dispatcher, voice, and the
# recordings folder): a broken proxy owns nothing the API's user owns.
CADDY_UID = "10002"
PUSH_UID = "10003"


def _production(name: str) -> dict:
    return _merged(name)


def _file(path):
    services = _services(_load(path))
    return lambda name: services.get(name) or {}


# (where, service, uid): every place Caddy or the push server is started.
PLACES = [
    ("production", _production, "caddy", CADDY_UID),
    ("production", _production, "push", PUSH_UID),
    ("workbench", _file(BASE), "push", PUSH_UID),
    ("shared caddy", _file(SHARED_CADDY), "caddy", CADDY_UID),
    ("operator", _file(OPERATOR), "push", PUSH_UID),
]
IDS = [f"{where}:{name}" for where, _, name, _ in PLACES]


def _named_volumes(service: dict) -> dict[str, str]:
    """Named volume -> path in the container; bind mounts start with . or $."""
    found = {}
    for entry in service.get("volumes", []):
        source, target = entry.split(":")[:2]
        if not source.startswith((".", "/", "$")):
            found[source] = target
    return found


@pytest.mark.parametrize(("where", "services", "name", "uid"), PLACES, ids=IDS)
def test_it_runs_as_its_own_user(where, services, name, uid):
    assert services(name).get("user") == f"{uid}:{uid}"


@pytest.mark.parametrize(("where", "services", "name", "uid"), PLACES, ids=IDS)
def test_its_volumes_go_to_that_user_before_it_starts(where, services, name, uid):
    service = services(name)
    chown = services(f"{name}-chown")
    volumes = _named_volumes(service)
    assert volumes, f"{where}: {name} has no volume"
    # The same volumes at the same places, and every one of them handed over.
    assert _named_volumes(chown) == volumes
    assert chown["entrypoint"] == ["chown", "-R", f"{uid}:{uid}", *volumes.values()]
    # The same image, so no other one is pulled for it.
    assert chown["image"] == service["image"]
    assert service["depends_on"][f"{name}-chown"] == {
        "condition": "service_completed_successfully"
    }


@pytest.mark.parametrize(("where", "services", "name", "uid"), PLACES, ids=IDS)
def test_the_chown_runs_once_with_one_capability(where, services, name, uid):
    chown = services(f"{name}-chown")
    assert chown["cap_drop"] == ["ALL"]
    assert chown["cap_add"] == ["CHOWN"]
    assert chown["security_opt"] == ["no-new-privileges:true"]
    assert chown["read_only"] is True
    assert chown["network_mode"] == "none"
    assert chown["restart"] == "no"
    assert "user" not in chown


@pytest.mark.parametrize(
    "services", [_production, _file(SHARED_CADDY)], ids=["production", "shared"]
)
def test_caddy_keeps_only_the_port_capability(services):
    caddy = services("caddy")
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert caddy["security_opt"] == ["no-new-privileges:true"]
