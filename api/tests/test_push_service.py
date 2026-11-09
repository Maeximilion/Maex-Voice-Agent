"""Push service for team notifications (D13): closed by default, wired to the workflow.

The service is configured in three places that have to agree: the Compose file
(who may do what), the n8n export (where the workflow publishes) and the proxy
(how the team's devices get in). Static checks, no Docker needed; how the
running server answers was tried by hand, see `n8n/README.md`.
"""

import re
import subprocess
from urllib.parse import urlparse

import pytest

from api.config import Settings
from api.events import types
from api.jobs import monitor
from api.tests.test_commands import REPO_ROOT
from api.tests.test_compose_ports import _load
from api.tests.test_n8n_workflow import _branches, _channels, _topics

COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
# Host ports for development; production never loads this file (docs/13 §3).
DEV = REPO_ROOT / "docker-compose.override.yml"

SERVICE = "push"
PUBLISHER = "n8n"
SUBSCRIBER = "team"
OWNER = "owner"
TOKEN_RE = re.compile(r"tk_[a-z0-9]{29}")
HASH_RE = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}")


def _service(path=COMPOSE) -> dict:
    # Compose's loader: the production file resets keys with `!reset`, which
    # `yaml.safe_load` rejects.
    return _load(path)["services"][SERVICE]


def _environment(path=COMPOSE) -> dict[str, str | None]:
    """The environment list as a mapping; a key without a value maps to None."""
    env = {}
    for entry in _service(path)["environment"]:
        key, separator, value = entry.partition("=")
        env[key] = value if separator else None
    return env


def _access(users: str) -> set[tuple[str, ...]]:
    """The access rules the start command sets for this NTFY_AUTH_USERS:
    (user, topic, permission). Runs the command itself, with `ntfy serve`
    replaced by printing the rules."""
    (script,) = _service()["command"]
    script = script.replace("$$", "$").replace(
        "exec ntfy serve", 'printf %s "$NTFY_AUTH_ACCESS"'
    )
    rules = subprocess.run(
        ["sh", "-c", script],
        env={"NTFY_AUTH_USERS": users},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {tuple(rule.split(":")) for rule in rules.split(",") if rule}


def _published() -> set[str]:
    """Every topic the workflow publishes to."""
    return set().union(*(_topics(event_type) for event_type in _branches()))


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


TEAM_USERS = "team:<hash>:user,n8n:<hash>:user"


def test_publisher_may_only_write_and_the_team_only_read() -> None:
    assert _topics(types.CALLBACK_CREATED) == {SUBSCRIBER}
    assert _access(TEAM_USERS) == {
        (PUBLISHER, SUBSCRIBER, "wo"),
        (SUBSCRIBER, SUBSCRIBER, "ro"),
        (PUBLISHER, OWNER, "wo"),
        (PUBLISHER, monitor.TOPIC, "wo"),
    }
    # n8n may write to exactly the topics the workflow publishes to.
    assert {t for user, t, _ in _access(TEAM_USERS) if user == PUBLISHER} == (
        _published()
    )


def test_the_owner_reads_only_the_owner_topic_once_that_user_exists() -> None:
    """Changes made without the owner's PIN and of the call mode (#588, T12)."""
    team = _access(TEAM_USERS)
    assert _access(f"{TEAM_USERS},owner:<hash>:user") == team | {(OWNER, OWNER, "ro")}
    # A user whose name only ends in "owner" is not the owner.
    assert _access(f"{TEAM_USERS},xowner:<hash>:user") == team


def test_the_operator_topic_opens_only_with_both_of_its_users() -> None:
    """The server refuses rules for a user it does not know (T-8.3)."""
    team = _access(TEAM_USERS)
    ops = {("monitor", monitor.TOPIC, "wo"), ("ops", monitor.TOPIC, "ro")}
    assert _access(f"{TEAM_USERS},ops:<hash>:user,monitor:<hash>:user") == team | ops
    assert _access(f"{TEAM_USERS},monitor:<hash>:user") == team
    assert _access(f"{TEAM_USERS},ops:<hash>:user") == team
    # A user whose name only ends in "monitor" is not the dispatcher.
    assert _access(f"{TEAM_USERS},ops:<hash>:user,xmonitor:<hash>:user") == team
    assert _access("") == set()


def test_the_alarms_go_to_the_service_in_the_stack() -> None:
    url = urlparse(Settings.model_fields["monitor_push_url"].default)
    assert (url.scheme, url.hostname, url.port, url.path) == (
        "http",
        SERVICE,
        None,
        "/",
    )
    # The team never reads the operator's topic; the workflow's `ops` is the
    # one the operator subscribes to for the alarms.
    assert _topics(types.CALLBACK_CREATED) == {SUBSCRIBER} != {monitor.TOPIC}
    assert monitor.TOPIC in _topics(types.CALL_MODE_CHANGED)


def test_rules_are_only_set_when_users_exist() -> None:
    # The server refuses to start with rules for users it does not know: a
    # fresh checkout without credentials must come up closed, not crash.
    (script,) = _service()["command"]
    assert _service()["entrypoint"] == ["/bin/sh", "-c"]
    assert 'if [ -n "$$NTFY_AUTH_USERS" ]' in script
    assert "unset NTFY_AUTH_USERS NTFY_AUTH_TOKENS" in script
    assert "NTFY_AUTH_ACCESS" not in _environment()


@pytest.mark.parametrize("channel", _channels(), ids=lambda c: c["name"])
def test_workflow_publishes_to_the_service_in_the_stack(channel) -> None:
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
    for channel in _channels():
        assert 0 < channel["parameters"]["options"]["timeout"] < dispatcher_ms


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
    prod = _load(PROD)["services"]
    assert SERVICE in prod["caddy"]["depends_on"]


def test_example_env_holds_no_credentials() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not TOKEN_RE.search(text)
    assert not HASH_RE.search(text)
    # Shown as comments only: a copied example starts the server closed.
    for key in ("NTFY_AUTH_USERS", "NTFY_AUTH_TOKENS"):
        lines = [line for line in text.splitlines() if key in line]
        assert lines and all(line.startswith("#") for line in lines), key
