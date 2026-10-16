"""Push service for team notifications (D13): closed by default, wired to the workflow.

The service is configured in three places that have to agree: the Compose file
(who may do what), the n8n export (where the workflow publishes) and the proxy
(how the team's devices get in). Static checks, no Docker needed; how the
running server answers was tried by hand, see `n8n/README.md`.
"""

import re
from urllib.parse import urlparse

import yaml

from api.config import Settings
from api.tests.test_commands import REPO_ROOT
from api.tests.test_n8n_workflow import _channel

COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
# Host ports for development; production never loads this file (docs/13 §3).
DEV = REPO_ROOT / "docker-compose.override.yml"

SERVICE = "push"
PUBLISHER = "n8n"
SUBSCRIBER = "team"
ACCESS_RE = re.compile(r'NTFY_AUTH_ACCESS="([^"]+)"')
TOPIC_RE = re.compile(r"topic:\s*'([^']+)'")
TOKEN_RE = re.compile(r"tk_[a-z0-9]{29}")
HASH_RE = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}")


def _service(path=COMPOSE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"][SERVICE]


def _environment(path=COMPOSE) -> dict[str, str | None]:
    """The environment list as a mapping; a key without a value maps to None."""
    env = {}
    for entry in _service(path)["environment"]:
        key, separator, value = entry.partition("=")
        env[key] = value if separator else None
    return env


def _access() -> set[tuple[str, str, str]]:
    """The access rules the start command sets: (user, topic, permission)."""
    (script,) = _service()["command"]
    (rules,) = ACCESS_RE.findall(script)
    return {tuple(rule.split(":")) for rule in rules.split(",")}


def _topic() -> str:
    (topic,) = TOPIC_RE.findall(_channel()["parameters"]["jsonBody"])
    return topic


def test_image_is_pinned_to_a_version() -> None:
    # A moving tag would change the server under the access rules unnoticed.
    assert re.search(r":v\d+\.\d+\.\d+$", _service()["image"])


def test_closed_unless_a_rule_opens_it() -> None:
    env = _environment()
    assert env["NTFY_AUTH_DEFAULT_ACCESS"] == "deny-all"
    assert "NTFY_ENABLE_SIGNUP" not in env
    # D13: the messages stay on our server. An upstream relay would tell a
    # third party about every message.
    assert not [key for key in env if "UPSTREAM" in key]


def test_users_and_tokens_come_from_the_environment() -> None:
    env = _environment()
    assert env["NTFY_AUTH_USERS"] is None
    assert env["NTFY_AUTH_TOKENS"] is None
    for path in (COMPOSE, PROD, CADDYFILE):
        text = path.read_text(encoding="utf-8")
        assert not TOKEN_RE.search(text), path.name
        assert not HASH_RE.search(text), path.name


def test_publisher_may_only_write_and_the_team_only_read() -> None:
    topic = _topic()
    assert _access() == {(PUBLISHER, topic, "wo"), (SUBSCRIBER, topic, "ro")}


def test_rules_are_only_set_when_users_exist() -> None:
    # The server refuses to start with rules for users it does not know: a
    # fresh checkout without credentials must come up closed, not crash.
    (script,) = _service()["command"]
    assert _service()["entrypoint"] == ["/bin/sh", "-c"]
    assert 'if [ -n "$$NTFY_AUTH_USERS" ]' in script
    assert "unset NTFY_AUTH_USERS NTFY_AUTH_TOKENS" in script
    assert "NTFY_AUTH_ACCESS" not in _environment()


def test_workflow_publishes_to_the_service_in_the_stack() -> None:
    channel = _channel()
    params = channel["parameters"]
    assert channel["type"] == "n8n-nodes-base.httpRequest"
    assert params["method"] == "POST"
    url = urlparse(params["url"])
    # Inside the Compose network, never through the public address.
    assert (url.scheme, url.hostname, url.port, url.path) == (
        "http",
        SERVICE,
        None,
        "/",
    )
    # The token lives in an n8n credential, not in the node.
    assert params["authentication"] == "genericCredentialType"
    assert params["genericAuthType"] == "httpHeaderAuth"
    assert "headerParameters" not in params
    body = params["jsonBody"]
    assert "title: $json.title" in body and "message: $json.text" in body
    assert "$json.priority === 'high' ? 4 : 3" in body


def test_channel_gives_up_before_the_dispatcher() -> None:
    # Otherwise the dispatcher times out first and sends the event again while
    # this run is still waiting for the push server.
    dispatcher_ms = Settings.model_fields["n8n_timeout_seconds"].default * 1000
    assert 0 < _channel()["parameters"]["options"]["timeout"] < dispatcher_ms


def test_host_port_is_loopback_only() -> None:
    # From outside the server is reached through the proxy, never directly.
    # The port lives in the dev file: in the base file it would also be published
    # on the production host (api/tests/test_compose_ports.py).
    assert "ports" not in _service() and "ports" not in _service(PROD)
    ports = _service(DEV)["ports"]
    assert ports and all(port.startswith("127.0.0.1:") for port in ports)


def test_proxy_routes_to_the_service_in_production() -> None:
    route = rf"^\s*reverse_proxy {SERVICE}:80\s*$"
    assert re.search(route, CADDYFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert _environment(PROD)["NTFY_BEHIND_PROXY"] == "true"
    prod = yaml.safe_load(PROD.read_text(encoding="utf-8"))["services"]
    assert SERVICE in prod["caddy"]["depends_on"]


def test_example_env_holds_no_credentials() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not TOKEN_RE.search(text)
    assert not HASH_RE.search(text)
    # Shown as comments only: a copied example starts the server closed.
    for key in ("NTFY_AUTH_USERS", "NTFY_AUTH_TOKENS"):
        lines = [line for line in text.splitlines() if key in line]
        assert lines and all(line.startswith("#") for line in lines), key
