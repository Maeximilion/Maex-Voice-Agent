"""Production Compose: caddy is the only service that publishes a host port.

docs/13 §3 promises "Internet 443 only" and a Postgres that is reachable only
inside the Docker network. Docker publishes ports past ufw, so a `ports:` entry
on the server is open whatever the host firewall says - the Compose files are
the only place where this promise is kept or broken.

The trap this guards against: Compose merges `ports` lists across files. An
override with `ports: []` adds nothing and removes nothing, the port of the base
file stays published. That is how db, api and n8n ended up on 0.0.0.0 in the
rendered production stack while the override claimed the opposite.

No docker CLI in the test container, so nothing is rendered here. The test
applies the merge rule for `ports` itself to the two files the production
command names, in that order: a plain list is appended to what earlier files
set, `!reset` drops it, `!override` replaces it. Everything the rule cannot see
(`include`, `extends`, a tag on a whole service) fails instead of passing
unchecked. `docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml
config` on a machine with Docker is the reference; the merge here was compared
against it when the test was written.
"""

from pathlib import Path

import yaml

from api.tests.test_commands import REPO_ROOT

BASE = REPO_ROOT / "docker-compose.yml"
PROD = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
# Loaded by a bare `docker compose up` only, never by the production command.
DEV = REPO_ROOT / "docker-compose.override.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "13_DEPLOYMENT.md"
PRODUCTION_FILES = "-f docker-compose.yml -f deploy/docker-compose.prod.yml"
# The reverse proxy. Every other service is reached through it or not at all.
PUBLISHERS = {"caddy"}
# HTTPS, and HTTP for the certificate challenge and the redirect. Nothing else,
# not even on the proxy: its admin API on 2019 would be just as open.
PROXY_PORTS = {80, 443}
LOOPBACK = {"127.0.0.1", "::1"}


class _Tagged:
    """A value behind `!reset` or `!override`, which `yaml.safe_load` rejects."""

    def __init__(self, tag: str, value: object) -> None:
        self.tag = tag
        self.value = value


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that keeps the two Compose merge tags instead of failing on them."""


def _construct_tagged(loader: yaml.SafeLoader, node: yaml.Node) -> _Tagged:
    if isinstance(node, yaml.SequenceNode):
        value: object = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return _Tagged(node.tag, value)


for _tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_tag, _construct_tagged)


def _parse(text: str) -> dict:
    return yaml.load(text, Loader=_ComposeLoader) or {}


def _load(path: Path) -> dict:
    return _parse(path.read_text(encoding="utf-8"))


def _services(document: dict) -> dict:
    services = document.get("services") or {}
    return services if isinstance(services, dict) else {}


def _merged_ports(documents: list[dict]) -> dict[str, list]:
    """The `ports` of every service after Compose merged the files in order."""
    ports: dict[str, list] = {}
    for document in documents:
        for name, service in _services(document).items():
            current = ports.setdefault(name, [])
            if not isinstance(service, dict) or "ports" not in service:
                continue
            value = service["ports"]
            if isinstance(value, _Tagged):
                # `!reset` drops the key whatever follows it, `!override` keeps
                # only what this file says.
                ports[name] = [] if value.tag == "!reset" else list(value.value or [])
            else:
                ports[name] = current + list(value or [])
    return ports


def _unverifiable(documents: list[dict]) -> list[str]:
    """Constructs that bring in ports from somewhere this test does not read."""
    found = []
    for document in documents:
        if document.get("include"):
            found.append("include: pulls in further files")
        if isinstance(document.get("services"), _Tagged):
            found.append("services: carries a merge tag")
        for name, service in sorted(_services(document).items()):
            if isinstance(service, _Tagged):
                found.append(f"{name}: carries a merge tag on the whole service")
            elif isinstance(service, dict) and service.get("extends"):
                found.append(f"{name}: extends another service")
    return found


def _published_outside_proxy(documents: list[dict]) -> dict[str, list]:
    return {
        name: ports
        for name, ports in sorted(_merged_ports(documents).items())
        if ports and name not in PUBLISHERS
    }


def _on_host_network(documents: list[dict]) -> list[str]:
    """Services on the host network: every port they listen on is a host port."""
    return sorted(
        {
            name
            for document in documents
            for name, service in _services(document).items()
            if isinstance(service, dict) and service.get("network_mode") == "host"
        }
    )


def _host_ip(entry: object) -> str | None:
    """The address a port entry binds to, or None when it binds to all of them."""
    if isinstance(entry, dict):  # long syntax
        return entry.get("host_ip")
    parts = str(entry).rsplit(":", 2)  # [address, published, target]
    return parts[0].strip("[]") if len(parts) == 3 else None


def _published(entry: object) -> int | None:
    """The host port of a port entry, or None when Docker picks one at random."""
    if isinstance(entry, dict):  # long syntax
        published = entry.get("published")
    else:
        parts = str(entry).split("/")[0].rsplit(":", 2)  # [address,] published, target
        published = parts[-2] if len(parts) > 1 else None
    return int(published) if str(published or "").isdigit() else None


def _production() -> list[dict]:
    return [_load(BASE), _load(PROD)]


def test_production_publishes_ports_only_on_the_proxy():
    documents = _production()
    assert not _unverifiable(documents), _unverifiable(documents)
    offenders = _published_outside_proxy(documents)
    assert not offenders, (
        f"published on the production host besides {sorted(PUBLISHERS)}: {offenders}. "
        "A dev port belongs in docker-compose.override.yml, which production never "
        "loads; `ports: []` in deploy/docker-compose.prod.yml does not remove it."
    )


def test_the_proxy_is_what_production_publishes():
    """The check above would also pass on files that publish nothing at all."""
    ports = _merged_ports(_production())
    assert {"db", "api", "n8n"} <= set(ports), sorted(ports)
    published = {
        name: [_published(entry) for entry in ports[name]] for name in PUBLISHERS
    }
    assert 443 in published["caddy"], (
        "caddy does not publish 443, the stack is unreachable"
    )
    for name, host_ports in published.items():
        # A range or a random port is not in the set either and fails here.
        assert set(host_ports) <= PROXY_PORTS, f"{name} publishes {host_ports}"


def test_no_production_service_sits_on_the_host_network():
    assert not _on_host_network(_production())


def test_documented_production_command_names_the_files_checked_here():
    """A third -f file in the command would be merged by Compose and unseen here."""
    for path in (DEPLOYMENT_DOC, PROD):
        commands = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "docker compose" in line and "docker-compose.prod.yml" in line
        ]
        assert commands, f"{path.name} names no production command"
        for command in commands:
            assert PRODUCTION_FILES in command, command
            assert command.count(" -f ") == 2, command


def test_dev_ports_bind_to_loopback_and_the_dev_file_carries_nothing_else():
    """`ports` only: mounts added here would bypass test_compose_mounts.py."""
    document = _load(DEV)
    # `include`, `volumes`, `secrets` at the top would be just as unseen there.
    assert set(document) == {"services"}, sorted(document)
    services = _services(document)
    assert services, "docker-compose.override.yml defines no service"
    for name, service in sorted(services.items()):
        assert set(service) == {"ports"}, f"{name}: {sorted(service)}"
        assert service["ports"], f"{name}: empty ports list"
        for entry in service["ports"]:
            assert _host_ip(entry) in LOOPBACK, (
                f"{name}: {entry} is open to the network"
            )


BASE_WITH_PORT = 'services:\n  db:\n    ports:\n      - "5432:5432"\n'


def _over_base(override: str) -> dict[str, list]:
    return _published_outside_proxy([_parse(BASE_WITH_PORT), _parse(override)])


def test_an_empty_list_in_the_override_does_not_unpublish():
    """The original defect, kept as a case: `ports: []` leaves the base port open."""
    assert _over_base("services:\n  db:\n    ports: []\n") == {"db": ["5432:5432"]}


def test_reset_tag_unpublishes():
    assert _over_base("services:\n  db:\n    ports: !reset []\n") == {}


def test_override_tag_replaces_the_base_list():
    override = 'services:\n  db:\n    ports: !override\n      - "127.0.0.1:1:1"\n'
    assert _over_base(override) == {"db": ["127.0.0.1:1:1"]}


def test_an_override_that_adds_a_port_is_caught():
    override = 'services:\n  db:\n    ports:\n      - "127.0.0.1:1:1"\n'
    assert _over_base(override) == {"db": ["5432:5432", "127.0.0.1:1:1"]}


def test_constructs_the_merge_rule_cannot_see_fail():
    assert _unverifiable([_parse("include:\n  - other.yml\nservices: {}\n")])
    assert _unverifiable([_parse("services:\n  db:\n    extends:\n      service: x\n")])
    assert _unverifiable([_parse("services:\n  db: !reset null\n")])
    assert not _unverifiable([_parse(BASE_WITH_PORT)])


def test_host_port_of_a_port_entry():
    assert _published("443:443") == 443
    assert _published("127.0.0.1:8000:8000/tcp") == 8000
    assert _published({"target": 80, "published": "80"}) == 80
    assert _published("8000") is None
    assert _published("8000-8010:8000-8010") is None
    assert _published("${PORT}:80") is None


def test_host_ip_of_a_port_entry():
    assert _host_ip("127.0.0.1:5432:5432") == "127.0.0.1"
    assert _host_ip("[::1]:8000:8000/tcp") == "::1"
    assert (
        _host_ip({"target": 80, "published": "80", "host_ip": "127.0.0.1"})
        == "127.0.0.1"
    )
    assert _host_ip("5432:5432") is None
    assert _host_ip(8000) is None
    assert _host_ip({"target": 80, "published": "80"}) is None
