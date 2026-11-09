"""Create a new customer's stack on the server, and its tenant inside it (#573, D32).

    python3 scripts/new_stack.py customer.json --domain example.org [--root /srv/maex] [--dry-run]
    python3 scripts/new_stack.py --check-sites [--root /srv/maex]

Run on the host as the deploy user, from any checkout of this repository. For
the customer whose key the file names (format: deploy/customer.example.json):

1. `<root>/<key>`: a clone of this checkout, the stack's folder.
2. `<root>/<key>/.env`, mode 600: `.env.example` with a fresh value for every
   secret (`secrets.token_hex(32)`, never two alike), `ENV=prod`, the Compose
   project `maex-<key>` in the customer layout (no Caddy of its own, an `ops`
   network), the names `agent.`, `n8n.` and `push.` of the domain. An existing
   `.env` is kept as it is: a rerun never changes a token.
3. Database alone, the application's role, the migrations, then
   `python -m api.jobs.provision create` with the customer file. Its report gives
   KITCHEN_BRIDGE_TENANT_ID; TENANT_NAME and TEAM_PHONE come from the file.
4. The whole stack up.
5. `<root>/caddy/sites/<key>.caddy`, mode 640, rendered from deploy/Caddyfile for
   this stack's containers and secret; every site file is checked against every
   other (docs/23 §8) before the shared Caddy (deploy/caddy) joins the stack's
   `edge` network and reloads.

The setup link of the first login is printed once and stored nowhere. Values of
secrets are never printed, only the names of what was written. `--dry-run`
checks the file, the names and the other site files and writes nothing.
`--check-sites` runs the cross-site check alone.

Exit code: 0 done, 1 a step or the check failed, 2 wrong call or input.
"""

import argparse
import json
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# api/models/tenants.py CUSTOMER_KEY; the host needs no package of the API.
CUSTOMER_KEY = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# api/domain/customers/phone.py E164.
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
DOMAIN = re.compile(r"^(?=.{1,200}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")
COMPOSE_FILES = "docker-compose.yml:deploy/docker-compose.prod.yml:deploy/docker-compose.customer.yml"
CADDY_PROJECT = "maex-caddy"
# What deploy/Caddyfile names, and what a site of one stack becomes.
UPSTREAMS = {"api:8000", "n8n:5678", "push:80"}
FRESH_VARIABLES = (
    "POSTGRES_PASSWORD",
    "AGENT_API_TOKEN",
    "GUI_PROXY_SECRET",
    "KITCHEN_BRIDGE_TOKEN",
    "VOICE_MEDIA_TOKEN",
    "N8N_BASIC_AUTH_PASSWORD",
    "N8N_ENCRYPTION_KEY",
    "ACCESS_PASSWORD",
)


class Refused(Exception):
    """A failed step or check: the message names it, never a secret."""


class BadInput(Refused):
    """Wrong customer file or domain: exit code 2."""


def project(key: str) -> str:
    return f"maex-{key}"


# The push server's users (docker-compose.yml `push`): people sign in with the
# first three, n8n and the dispatcher with their tokens.
PUSH_USERS = ("team", "owner", "ops", "n8n", "monitor")
PUSH_SIGN_IN = ("team", "owner", "ops")
# Reads one password per line and prints its bcrypt hash, inside the stack's own
# pinned push image: no password on a command line, no hash library on the host.
HASH_SCRIPT = 'while read -r p; do printf "%s\\n%s\\n" "$p" "$p" | ntfy user hash; done'


def push_token() -> str:
    """An access token in ntfy's own form: tk_ and 29 lower-case letters or digits."""
    alphabet = string.ascii_lowercase + string.digits
    return "tk_" + "".join(secrets.choice(alphabet) for _ in range(29))


def push_env(hash_passwords) -> tuple[dict[str, str], dict[str, str]]:
    """The push server's users and tokens for .env, and the passwords of the
    users people sign in with, to print once (only their hashes are kept)."""
    passwords = {user: secrets.token_hex(16) for user in PUSH_USERS}
    hashes = hash_passwords(list(passwords.values()))
    if len(hashes) != len(PUSH_USERS) or not all(h.startswith("$2") for h in hashes):
        raise Refused("hashing the push server's passwords failed")
    tokens = {user: push_token() for user in ("n8n", "monitor")}
    values = {
        "NTFY_AUTH_USERS": ",".join(
            f"{user}:{hashed}:user"
            for user, hashed in zip(PUSH_USERS, hashes, strict=True)
        ),
        "NTFY_AUTH_TOKENS": (
            f"n8n:{tokens['n8n']}:n8n workflow,monitor:{tokens['monitor']}:monitor"
        ),
        "MONITOR_PUSH_TOKEN": tokens["monitor"],
    }
    return values, {user: passwords[user] for user in PUSH_SIGN_IN}


def fresh_env(spec: dict, domain: str) -> dict[str, str]:
    """Every value the new stack's .env sets, secrets freshly made."""
    made = {name: secrets.token_hex(32) for name in FRESH_VARIABLES}
    app_password = secrets.token_hex(32)
    key = spec["key"]
    values = {
        **made,
        "ENV": "prod",
        "COMPOSE_PROJECT_NAME": project(key),
        "COMPOSE_FILE": COMPOSE_FILES,
        "POSTGRES_USER": "maex",
        "POSTGRES_DB": "maex_agent",
        "DATABASE_URL": f"postgresql+psycopg://maex_app:{app_password}@db:5432/maex_agent",
        "OWNER_DATABASE_URL": (
            f"postgresql+psycopg://maex:{made['POSTGRES_PASSWORD']}@db:5432/maex_agent"
        ),
        "BACKUP_DOCKER_NETWORK": f"{project(key)}_backend",
        "AGENT_DOMAIN": f"agent.{domain}",
        "N8N_DOMAIN": f"n8n.{domain}",
        "PUSH_DOMAIN": f"push.{domain}",
        "NTFY_BASE_URL": f"https://push.{domain}",
        "TENANT_NAME": spec["name"],
        "TENANT_TIMEZONE": spec.get("timezone") or "Europe/Berlin",
    }
    if spec.get("team_phone"):
        values["TEAM_PHONE"] = spec["team_phone"]
    return values


def _quoted(value: str) -> str:
    # Compose's .env: double quotes keep spaces and `#`; `$` would interpolate.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def env_text(example: str, values: dict[str, str]) -> str:
    """The example with each value set: on its active line, else on its
    commented-out one, else appended. Comments stay for the operator."""
    lines = example.splitlines()
    for name, value in values.items():
        line = f"{name}={_quoted(value)}"
        for pattern in (rf"^{name}=", rf"^#\s*{name}="):
            hit = next((i for i, x in enumerate(lines) if re.match(pattern, x)), None)
            if hit is not None:
                lines[hit] = line
                break
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def read_env(path: Path) -> dict[str, str]:
    """Active lines of a .env: double quotes as this script writes them, single
    quotes as the operator writes a hash (.env.example), or bare."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if not match:
            continue
        name, raw = match.groups()
        if len(raw) > 1 and raw[0] == raw[-1] == '"':
            raw = re.sub(r"\\(.)", r"\1", raw[1:-1].replace("$$", "$"))
        elif len(raw) > 1 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1]
        values[name] = raw
    return values


def write_private(path: Path, text: str, mode: int) -> None:
    """Written under a temporary name with its mode from the first byte, then
    moved over: no moment with a readable or half-written file."""
    temporary = path.with_name(f".{path.name}.new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def site_text(caddyfile: str, key: str, env: dict[str, str]) -> str:
    """deploy/Caddyfile for one stack behind the shared Caddy: its names, its
    containers instead of the bare service names, its secret and editor access
    written in (the shared Caddy's environment serves every stack), its own
    snippet name (all site files share one Caddyfile)."""
    name = project(key)
    replacements = {
        "{$AGENT_DOMAIN:agent.example.com}": env["AGENT_DOMAIN"],
        "{$N8N_DOMAIN:n8n.example.com}": env["N8N_DOMAIN"],
        "{$PUSH_DOMAIN:push.example.com}": env["PUSH_DOMAIN"],
        "{env.GUI_PROXY_SECRET}": env["GUI_PROXY_SECRET"],
        # Both empty: no account, the editor answers 401 to everyone, as today.
        "{$N8N_EDITOR_AUTH_USER}": env.get("N8N_EDITOR_AUTH_USER", ""),
        "{$N8N_EDITOR_AUTH_HASH}": env.get("N8N_EDITOR_AUTH_HASH", ""),
        "(transport)": f"(transport-{key})",
        "import transport": f"import transport-{key}",
    }
    for upstream in UPSTREAMS:
        replacements[f"reverse_proxy {upstream}"] = f"reverse_proxy {name}-{upstream}"
    text = caddyfile
    for old, new in replacements.items():
        if old not in text:
            # A change of deploy/Caddyfile must reach this list, not slip past it.
            raise Refused(f"deploy/Caddyfile no longer contains {old}")
        text = text.replace(old, new)
    # Container names are the Compose default, <project>-<service>-1.
    return re.sub(rf"reverse_proxy {name}-(\w+):", rf"reverse_proxy {name}-\1-1:", text)


def _site_facts(text: str) -> tuple[set[str], set[str], set[str]]:
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    upstreams = set(re.findall(r"reverse_proxy\s+(\S+)", code))
    header = set(re.findall(r'X-Maex-Proxy-Secret\s+"([^"]*)"', code))
    names = set(re.findall(r"^(\S+)\s*\{\s*$", code, re.MULTILINE)) - set(
        re.findall(r"^(\(\S+\))", code, re.MULTILINE)
    )
    return upstreams, header, names


def check_sites(sites: dict[str, str]) -> list[str]:
    """Each site file reaches only its own stack's containers and carries a
    secret and host names no other file has (docs/23 §8). Key -> text."""
    problems = []
    seen_headers: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for key, text in sorted(sites.items()):
        upstreams, header, names = _site_facts(text)
        own = rf"^{re.escape(project(key))}-(api|n8n|push)-1:\d+$"
        problems += [
            f"{key}: proxies to {u}, not to its own stack"
            for u in sorted(upstreams)
            if not re.match(own, u)
        ]
        if len(upstreams) != len(UPSTREAMS):
            problems.append(f"{key}: {len(upstreams)} upstreams, expected 3")
        if len(header) != 1 or len(next(iter(header), "")) < 32:
            problems.append(f"{key}: no single proxy secret of 32 characters or more")
        for value in header:
            if value in seen_headers:
                problems.append(f"{key}: same proxy secret as {seen_headers[value]}")
            seen_headers[value] = key
        for name in names:
            if name in seen_names:
                problems.append(f"{key}: host {name} is also {seen_names[name]}'s")
            seen_names[name] = key
    return problems


def site_files(sites_dir: Path) -> dict[str, str]:
    if not sites_dir.is_dir():
        return {}
    return {path.stem: path.read_text() for path in sorted(sites_dir.glob("*.caddy"))}


def networks_file(keys: list[str]) -> str:
    """Compose file (JSON is YAML) that puts the shared Caddy on each stack's
    `edge` network, regenerated from the site files on every run."""
    nets = [f"{project(key)}_edge" for key in sorted(keys)]
    return json.dumps(
        {
            "services": {"caddy": {"networks": nets}},
            "networks": {net: {"external": True} for net in nets},
        },
        indent=2,
    )


def run(command: list[str], cwd: Path, **options) -> subprocess.CompletedProcess:
    print("+ " + " ".join(command), file=sys.stderr)
    # Docker's progress goes to stderr: stdout keeps the result and the link.
    options.setdefault("stdout", sys.stderr)
    done = subprocess.run(command, cwd=cwd, text=True, **options)
    if done.returncode != 0:
        raise Refused(f"failed with exit code {done.returncode}: {' '.join(command)}")
    return done


def read_customer(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BadInput(f"customer file {path}: {error}") from None
    key = spec.get("key") if isinstance(spec, dict) else None
    if not isinstance(key, str) or not CUSTOMER_KEY.fullmatch(key) or len(key) > 32:
        raise BadInput("customer file: key must look like `pilot` or `pizza-nord`")
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        # A line break would end the .env line and start another variable.
        raise BadInput("customer file: name is missing or has a control character")
    phone = spec.get("team_phone")
    if phone and not (isinstance(phone, str) and E164.fullmatch(phone)):
        # It becomes TEAM_PHONE as written, and provisioning compares the
        # normalized number with it exactly: only E.164 reads the same both ways.
        raise BadInput("customer file: team_phone must be E.164, like +4972215551234")
    return spec


def caddy(root: Path, *args: str) -> tuple[list[str], dict[str, str]]:
    """Command and environment for the shared Caddy's Compose project."""
    sites = root / "caddy" / "sites"
    return [
        "docker",
        "compose",
        "-p",
        CADDY_PROJECT,
        "-f",
        str(REPO_ROOT / "deploy" / "caddy" / "docker-compose.yml"),
        "-f",
        str(root / "caddy" / "networks.json"),
        *args,
    ], {**os.environ, "MAEX_SITES": str(sites), "MAEX_SITES_GID": str(os.getgid())}


def create(spec_path: Path, domain: str, root: Path, dry_run: bool) -> None:
    spec = read_customer(spec_path)
    if not DOMAIN.fullmatch(domain):
        raise BadInput(f"domain {domain!r}: lower-case host name like example.org")
    key = spec["key"]
    stack = root / key
    sites_dir = root / "caddy" / "sites"
    others = {k: t for k, t in site_files(sites_dir).items() if k != key}
    if problems := check_sites(others):
        raise Refused("site files already wrong: " + "; ".join(problems))
    for other, text in others.items():
        if f"agent.{domain}" in _site_facts(text)[2]:
            raise BadInput(f"agent.{domain} is already {other}'s site")
    if dry_run:
        print(f"would create stack {stack} (project {project(key)})")
        print(
            f"would write .env with fresh {', '.join(FRESH_VARIABLES)} and the database role"
        )
        print(f"would write {sites_dir / (key + '.caddy')} for agent.{domain}")
        return

    root.mkdir(parents=True, exist_ok=True)
    if not stack.exists():
        run(["git", "clone", "--quiet", str(REPO_ROOT), str(stack)], root)
    env_path = stack / ".env"
    if not env_path.exists():
        example = (stack / ".env.example").read_text(encoding="utf-8")
        write_private(env_path, env_text(example, fresh_env(spec, domain)), 0o600)
        print(f"wrote {env_path}: " + ", ".join(FRESH_VARIABLES), file=sys.stderr)
    env = read_env(env_path)
    if env.get("COMPOSE_PROJECT_NAME") != project(key):
        raise Refused(f"{env_path} belongs to another project, not {project(key)}")
    if env.get("AGENT_DOMAIN") != f"agent.{domain}":
        # The kept .env decides the names; a new domain is a change by hand.
        raise BadInput(
            f"{env_path} serves {env.get('AGENT_DOMAIN')}, not agent.{domain}"
        )
    sign_in: dict[str, str] = {}
    if not env.get("NTFY_AUTH_USERS"):

        def hash_passwords(passwords: list[str]) -> list[str]:
            hashed = run(
                [
                    *("docker", "compose", "run", "--rm", "-T", "--no-deps"),
                    *("--entrypoint", "sh", "push", "-c", HASH_SCRIPT),
                ],
                stack,
                input="\n".join(passwords) + "\n",
                stdout=subprocess.PIPE,
            ).stdout
            return [word for word in hashed.split() if word.startswith("$2")]

        push, sign_in = push_env(hash_passwords)
        current = env_path.read_text(encoding="utf-8")
        write_private(env_path, env_text(current, push), 0o600)
        print(f"wrote {env_path}: " + ", ".join(push), file=sys.stderr)
    # Checked before anything starts: a refused site leaves no stack behind.
    caddyfile = (stack / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    sites = {**others, key: site_text(caddyfile, key, env)}
    if problems := check_sites(sites):
        raise Refused("site file refused, nothing started: " + "; ".join(problems))

    run(["docker", "compose", "up", "-d", "--wait", "db"], stack)
    run(["bash", "scripts/db_role.sh"], stack)
    run(["bash", "scripts/migrate.sh"], stack)
    mount = f"{spec_path.resolve()}:/customer.json:ro"
    report = json.loads(
        run(
            [
                *("docker", "compose", "run", "--rm", "-T", "-v", mount, "api"),
                *("python", "-m", "api.jobs.provision", "create", "/customer.json"),
                *("--base-url", f"https://agent.{domain}"),
            ],
            stack,
            stdout=subprocess.PIPE,
        ).stdout
    )
    current = env_path.read_text(encoding="utf-8")
    tenant = {"KITCHEN_BRIDGE_TENANT_ID": report["tenant_id"]}
    write_private(env_path, env_text(current, tenant), 0o600)
    run(["docker", "compose", "up", "-d", "--build"], stack)

    sites_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    write_private(sites_dir / f"{key}.caddy", sites[key], 0o640)
    (root / "caddy" / "networks.json").write_text(networks_file(list(sites)))
    command, caddy_env = caddy(root, "up", "-d")
    run(command, root, env=caddy_env)
    command, caddy_env = caddy(root, "exec", "-T", "caddy")
    run(
        [*command, "caddy", "reload", "--config", "/etc/caddy/Caddyfile"],
        root,
        env=caddy_env,
    )

    print(f"Stack {project(key)} runs, tenant {report['tenant_id']}.")
    if report.get("setup_link"):
        print(f"First login, once: {report['setup_link']}")
    for user, password in sign_in.items():
        # Shown once like the setup link; .env keeps only the hash.
        print(f"Push server user {user}, password once: {password}")
    for gap in report.get("missing", []):
        print(f"still missing: {gap}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("customer", type=Path, nargs="?")
    parser.add_argument("--domain")
    parser.add_argument("--root", type=Path, default=Path("/srv/maex"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-sites", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_sites:
            problems = check_sites(site_files(args.root / "caddy" / "sites"))
            for problem in problems:
                print(problem)
            return 1 if problems else 0
        if not args.customer or not args.domain:
            parser.error("a customer file and --domain are needed")
        create(args.customer, args.domain, args.root, args.dry_run)
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2 if isinstance(refused, BadInput) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
