"""Production: what the server offers to the internet, and what a container may touch.

Three promises of docs/13 §3 live in two files and nowhere in the code, so only
a test notices when one is dropped:

- The proxy passes on what something outside the server needs (operations view,
  print bridge, health check) and nothing else. The tools under `/v1/tools` and
  the call endpoints are for the voice layer on the same server.
- The workflow editor has an access check in front. n8n 2 ignores the
  `N8N_BASIC_AUTH_*` variables, so its own first-visit setup is all there is.
- No container can change the checkout it runs from, and the API does not run
  as root. The host executes `scripts/backup.sh` from that checkout every night.

Nothing is started here: there is no docker CLI and no Caddy in the test
container. The Caddyfile is read as text and the Compose merge rule for
`volumes` (by container path, later file wins) is applied by hand. Both were
compared with a running Caddy and with `docker compose config` when the test
was written (12.10.2026).
"""

import re

from api.config import Settings
from api.tests.test_commands import REPO_ROOT
from api.tests.test_compose_ports import _load, _services

CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"
BASE = REPO_ROOT / "docker-compose.yml"
PROD = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
# Services that run this repository's code from a bind mount.
OWN_CODE = ("api", "dispatcher", "voice")
# Everything else under these prefixes is for inside the Docker network.
PUBLIC_PATHS = {"/gui", "/gui/*", "/v1/kitchen/*", "/health"}
ROOT_USERS = {"", "0", "root"}


def _sites(text: str) -> dict[str, str]:
    """Site address -> body, for every top-level block that is not a snippet."""
    sites: dict[str, str] = {}
    depth, address = 0, ""
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip() if depth == 0 else line.rstrip()
        if depth == 0:
            if stripped.endswith("{"):
                address, body, depth = stripped[:-1].strip(), [], 1
            continue
        depth += stripped.count("{") - stripped.count("}")
        if depth == 0:
            if not address.startswith("("):
                sites[address] = "\n".join(body)
        else:
            body.append(line)
    return sites


def _site(upstream: str) -> str:
    bodies = [
        body for body in _sites(CADDYFILE.read_text()).values() if upstream in body
    ]
    assert len(bodies) == 1, f"expected one site for {upstream}"
    return bodies[0]


def _without_comments(body: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def test_the_sites_are_the_three_the_stack_has():
    sites = _sites(CADDYFILE.read_text())
    assert len(sites) == 3
    # A placeholder with a default: the real names come from .env on the server.
    for address in sites:
        assert re.fullmatch(
            r"\{\$[A-Z0-9_]+_DOMAIN:[a-z0-9.]+\.example\.com\}", address
        )


def test_only_view_bridge_and_health_reach_the_api():
    body = _without_comments(_site("api:8000"))

    matcher = re.search(r"@public path (.+)", body)
    assert matcher and set(matcher.group(1).split()) == PUBLIC_PATHS
    # One way to the API, and it is the one behind the matcher.
    assert body.count("reverse_proxy") == 1
    proxied = re.search(r"handle @public \{(.*?)\n    \}", body, re.DOTALL)
    assert proxied and "reverse_proxy api:8000" in proxied.group(1)
    # What matches nothing is answered here, not passed on.
    assert re.search(r"handle \{\s+respond 404\s+\}", body)


def test_the_public_paths_leave_the_tools_inside():
    for path in PUBLIC_PATHS:
        assert not path.startswith(("/v1/tools", "/v1/calls", "/docs", "/openapi"))
        assert path != "/*" and path != "/v1/*"


def test_the_workflow_editor_has_its_own_access_in_front():
    body = _without_comments(_site("n8n:5678"))

    access = re.search(r"basic_auth \{\s+(\S+) (\S+)\s+\}", body)
    assert access, "the n8n site needs basic_auth for every path"
    user, password_hash = access.groups()
    # From the environment, never from the repository (CLAUDE.md §8), and not
    # the tablet's shared access: that one is known to the whole team.
    assert (user, password_hash) == (
        "{$N8N_EDITOR_AUTH_USER}",
        "{$N8N_EDITOR_AUTH_HASH}",
    )
    assert body.index("basic_auth") < body.index("reverse_proxy")


def test_n8n_is_not_told_to_use_a_login_it_no_longer_has():
    """The three variables read like protection and are ignored since n8n 1."""
    for path in (BASE, PROD):
        environment = _services(_load(path)).get("n8n", {}).get("environment") or {}
        assert not [key for key in environment if str(key).startswith("N8N_BASIC_AUTH")]


def _target(entry: object) -> str:
    return entry["target"] if isinstance(entry, dict) else str(entry).split(":")[1]


def _read_only(entry: object) -> bool:
    if isinstance(entry, dict):
        return bool(entry.get("read_only"))
    return str(entry).split(":")[2:] == ["ro"]


def _is_bind(entry: object) -> bool:
    if isinstance(entry, dict):
        return entry.get("type") == "bind"
    return str(entry).startswith((".", "/", "~"))


def _production_mounts(service: str) -> dict[str, object]:
    """Container path -> mount, after the production file was merged over the base."""
    mounts: dict[str, object] = {}
    for path in (BASE, PROD):
        for entry in _services(_load(path)).get(service, {}).get("volumes") or []:
            mounts[_target(entry)] = entry
    return mounts


def test_no_container_can_write_into_the_checkout_in_production():
    writable = {
        f"{service}: {target}"
        for service in _services(_load(BASE))
        for target, entry in _production_mounts(service).items()
        if _is_bind(entry) and not _read_only(entry)
    }
    assert writable == set(), (
        "writable in production; repeat the mount with :ro in "
        f"deploy/docker-compose.prod.yml: {sorted(writable)}"
    )


def test_the_base_file_keeps_the_source_writable_for_development():
    """The read-only mounts are a production decision; `make test` writes."""
    assert not _read_only(
        next(
            entry
            for entry in _services(_load(BASE))["api"]["volumes"]
            if _target(entry) == "/app/api"
        )
    )


def test_own_code_does_not_run_as_root_in_production():
    """The user of the production file, else the one of the base file."""
    base, production = _services(_load(BASE)), _services(_load(PROD))
    for service in OWN_CODE:
        user = str(
            production.get(service, {}).get("user")
            or base.get(service, {}).get("user", "")
        )
        assert user.split(":")[0] not in ROOT_USERS, f"{service} runs as root"


def test_own_code_knows_it_runs_in_production():
    """`.env` on the server may still say ENV=dev, the example does. A service
    without the override would start on the placeholder token: the guard in
    `api/config.py` asks ENV (Codex, PR #253)."""
    production = _services(_load(PROD))
    for service in OWN_CODE:
        environment = production.get(service, {}).get("environment") or {}
        assert environment.get("ENV") == "prod", f"{service} has no ENV: prod"


def test_mount_helpers_read_both_spellings():
    assert _target("./api:/app/api:ro") == "/app/api"
    assert _read_only("./api:/app/api:ro") and not _read_only("./api:/app/api")
    assert _is_bind("./api:/app/api") and not _is_bind("pgdata:/var/lib/postgresql")
    long_form = {"type": "bind", "source": ".", "target": "/x", "read_only": True}
    assert _target(long_form) == "/x" and _read_only(long_form) and _is_bind(long_form)


def _merged(service: str) -> dict:
    """One service after the production file was merged over the base: a list
    is joined with the base's, as Compose does for these keys, a mapping is
    merged, anything else replaced."""
    merged: dict = {}
    for path in (BASE, PROD):
        for key, value in (_services(_load(path)).get(service) or {}).items():
            if isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = merged[key] + [v for v in value if v not in merged[key]]
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def _handed(environment: dict) -> set[str]:
    """Names a service gets a value for: a key without one takes the line of
    .env, an empty string is a blank."""
    return {name for name, value in environment.items() if value != ""}


def _setting_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_own_code_gets_no_line_of_env_it_does_not_name():
    """`env_file: .env` handed api, dispatcher and voice every secret of the
    stack, the owner's, Caddy's, n8n's and the access password (#252 finding 7).
    Each names what it reads, and that is a setting of api/config.py."""
    for service in OWN_CODE:
        merged = _merged(service)
        assert "env_file" not in merged, f"{service} gets all of .env"
        stray = set(merged["environment"]) - _setting_names()
        assert stray == set(), f"{service} is handed what api/config.py never reads"


def test_every_setting_reaches_a_service():
    """A setting added to api/config.py but to none of the lists would keep its
    default in every container, whatever .env says."""
    handed = set().union(*(_handed(_merged(s)["environment"]) for s in OWN_CODE))
    assert _setting_names() - handed == set()


def test_each_secret_reaches_only_the_services_that_read_it():
    """The access password is Asterisk's, the proxy secret the API's and
    Caddy's, the webhook password the dispatcher's, the media token voice's
    (#252 findings 6 and 7). The agent token goes to all three: the start check
    of api/config.py asks it of every process outside dev and test."""
    readers = {
        "DATABASE_URL": set(OWN_CODE),
        "AGENT_API_TOKEN": set(OWN_CODE),
        "GUI_PROXY_SECRET": {"api"},
        "KITCHEN_BRIDGE_TOKEN": {"api"},
        "N8N_BASIC_AUTH_PASSWORD": {"dispatcher"},
        "VOICE_MEDIA_TOKEN": {"voice"},
        "LLM_API_KEY": {"voice"},
    }
    for secret, services in readers.items():
        holders = {
            service
            for service in OWN_CODE
            if secret in _handed(_merged(service)["environment"])
        }
        assert holders == services, f"{secret} in production: {sorted(holders)}"
    # In development the API's container runs the evals, which may need a key.
    assert "LLM_API_KEY" in _services(_load(BASE))["api"]["environment"]
    assert _services(_load(BASE))["asterisk"]["environment"]["ACCESS_PASSWORD"]
    assert "env_file" not in _services(_load(BASE))["asterisk"]
    assert _merged("caddy")["environment"]["GUI_PROXY_SECRET"] == (
        "${GUI_PROXY_SECRET:-}"
    )


def test_no_service_gains_rights_after_its_start():
    """#252 finding 8: no setuid binary or file capability raises a process,
    in development and in production."""
    for service in _services(_load(BASE)) | _services(_load(PROD)):
        for options in (
            _services(_load(BASE)).get(service, {}).get("security_opt"),
            _merged(service).get("security_opt"),
        ):
            if service == "caddy" and options is None:
                continue  # production only
            assert "no-new-privileges:true" in (options or []), service


# What a service may keep in production, and why, beside the comment in the
# Compose file: root in the image's entrypoint that hands files over and drops
# to its own user; Caddy binds 80 and 443.
HANDS_OVER = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]
KEPT_CAPABILITIES = {
    "db": HANDS_OVER,
    "asterisk": HANDS_OVER,
    "caddy": ["NET_BIND_SERVICE"],
}
# Writes into /var/lib, /var/spool and /var/log of its image.
WRITABLE_ROOT = {"asterisk"}


def test_production_keeps_no_capability_a_service_does_not_need():
    for service in _services(_load(BASE)) | _services(_load(PROD)):
        merged = _merged(service)
        assert merged.get("cap_drop") == ["ALL"], f"{service} keeps its capabilities"
        assert merged.get("cap_add", []) == KEPT_CAPABILITIES.get(service, []), service


def test_production_roots_are_read_only():
    for service in _services(_load(BASE)) | _services(_load(PROD)):
        read_only = bool(_merged(service).get("read_only"))
        assert read_only is (service not in WRITABLE_ROOT), service


def test_n8n_sends_no_usage_data_and_keeps_its_own_key():
    """#252 finding 9. The key is optional and comes from .env alone: a default
    here would differ from the one in an existing volume, and n8n refuses to
    start on a key that does not match it (checked with 2.43.3)."""
    environment = _merged("n8n")["environment"]
    assert environment["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert environment["N8N_ENCRYPTION_KEY"] is None
    example = (REPO_ROOT / ".env.example").read_text()
    assert "# N8N_ENCRYPTION_KEY=\n" in example
