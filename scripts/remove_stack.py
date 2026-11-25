"""Remove a leaving customer's stack from the server, after its export (#576, D32).

    python3 scripts/remove_stack.py <key> --export [--root /srv/maex]
    python3 scripts/remove_stack.py <key> [--root /srv/maex] [--dry-run]
    python3 scripts/remove_stack.py <key> --confirm <key> [--root /srv/maex]

Run on the host as the deploy user, from any checkout of this repository: the
counterpart of scripts/new_stack.py. A customer is a whole stack (D32), so its
data goes with the stack's database, volumes, folder, site file and the backups
in that folder; nothing is deleted row by row.

1. `--export`: in the stack, `python -m api.jobs.offboarding export` writes menu
   (docs/14 import format), orders and reservations into
   `<root>/offboarding/<key>/`, mode 700, outside the stack. It is handed to the
   customer and then deleted by hand (docs/13 §8).
2. Without `--confirm` (the default, `--dry-run` says it out loud): checks the
   export and lists exactly what would go, the containers, volumes, networks and
   images of the Compose project `maex-<key>` as Docker names them, the stack
   folder, the site file and the number of local backups. Changes nothing.
3. `--confirm <key>`, the key typed a second time: refused without a whole
   export (its manifest names this key, every file has its checksum, the
   database held no other tenant). Then the stack stops, the export is written
   once more while its database volume is still there (the same files unless
   something changed since; nothing can now), the site file leaves the shared
   Caddy, which reloads (put back if it refuses, and nothing of the stack is
   removed), the operator dashboard leaves the stack's `ops` network and its
   registry, `docker compose down --volumes` with every profile, leftovers by
   the project's label, then the folder, its `.env` last.
4. One JSON line in `<root>/offboarding/removed.jsonl` (mode 640), outside the
   removed stack: key, time, the export's time, checksum and row counts, what
   was removed, and what is left to do by hand. No name, number or address.

A rerun after a failed step takes up what is left; one after a complete
removal finds nothing and writes no second line. Backups outside the stack
folder (a BACKUP_DIR elsewhere) are refused, since their file names do not say
which stack they belong to; off-site copies are not touched and expire with
their rolling 30 days (Maxi's decision 9 of 15.11.2026, docs/13 §8).

Exit code: 0 done or nothing left, 1 a step or check failed, 2 wrong call.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # Run as `python3 scripts/remove_stack.py`: the shared parts of new_stack.py.
    sys.path.insert(0, str(REPO_ROOT))

from scripts import new_stack  # noqa: E402
from scripts.new_stack import (  # noqa: E402
    CUSTOMER_KEY,
    OPERATOR_PROJECT,
    RESERVED_KEYS,
    BadInput,
    Refused,
    caddy,
    operator_networks_file,
    project,
    read_env,
    site_files,
    write_private,
)

LEDGER = "removed.jsonl"
MANIFEST = "manifest.json"
# api.jobs.offboarding FILES (its imports need the stack's image, not this
# host): a manifest that lists fewer is no whole export.
EXPORT_FILES = frozenset(
    {
        "menu_items.csv",
        "item_options.csv",
        "item_allergens.csv",
        "item_aliases.csv",
        "orders.csv",
        "order_items.csv",
        "reservations.csv",
    }
)
# The volume that holds the stack's database (docker-compose.yml `pgdata`).
DATABASE_VOLUME = "pgdata"
BY_HAND = (
    "hand the export to the customer, then delete <root>/offboarding/<key>",
    "off-site backups of the stack expire with their rolling 30 days "
    "(decision 9 of 15.11.2026, docs/13 §8): tell the customer that date",
    "the stack's .env copy in the password manager",
    "the customer's DNS names, phone number and SIP trunk",
)


def run(
    command: list[str], cwd: Path, **options: Any
) -> subprocess.CompletedProcess[str]:
    """Every subprocess of this script goes through here; tests replace it."""
    try:
        return new_stack.run(command, cwd, **options)
    except OSError as error:
        # No docker on PATH, say, before anything was changed.
        raise Refused(f"cannot run {command[0]}: {error.strerror}") from None


def compose_env() -> dict[str, str]:
    """The caller's environment without COMPOSE_*: Compose lets an exported
    COMPOSE_PROJECT_NAME or COMPOSE_FILE win over the stack's .env."""
    return {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}


def compose(key: str, *args: str) -> list[str]:
    """Compose in the stack's folder with its project named on the command
    line, and every profile, so `voice` and `asterisk` go too."""
    return ["docker", "compose", "-p", project(key), "--profile", "*", *args]


def check_key(key: str) -> None:
    if not CUSTOMER_KEY.fullmatch(key) or len(key) > 32:
        raise BadInput("key must look like `pilot` or `pizza-nord`")
    if key in RESERVED_KEYS:
        raise BadInput(f"key {key!r} is reserved, not a customer's")


def export_folder(root: Path, key: str) -> Path:
    return root / "offboarding" / key


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_export(folder: Path, key: str) -> dict[str, Any]:
    """The whole export of this key, or Refused: the manifest is written last,
    and every file must still have the checksum it lists."""
    path = folder / MANIFEST
    if not path.is_file():
        raise Refused(
            f"no export in {folder}: run with --export first, nothing is removed"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise Refused(f"{path} is not JSON, nothing is removed") from None
    if not isinstance(manifest, dict) or manifest.get("customer_key") != key:
        raise Refused(f"{path} is not the export of {key!r}, nothing is removed")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != EXPORT_FILES:
        raise Refused(f"{path} does not list the whole export, nothing is removed")
    for name, entry in sorted(files.items()):
        file = folder / name
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not file.is_file() or _digest(file) != digest:
            raise Refused(f"{file} is missing or changed since the export")
    if manifest.get("other_tenants") != 0:
        # The database holds another tenant: removing the stack would take its
        # rows too. Not a case of D32; a per-tenant deletion is not built.
        raise Refused(
            f"the database of {project(key)} held {manifest.get('other_tenants')} "
            "other tenant(s) at the export; the whole stack is not removed"
        )
    return manifest


def _docker_ids(kind: str, key: str) -> list[str]:
    """Names of what Docker has under the project's label: read only."""
    label = f"label=com.docker.compose.project={project(key)}"
    command = {
        "container": [
            "docker",
            "ps",
            "-a",
            "--filter",
            label,
            "--format",
            "{{.Names}}",
        ],
        "volume": [
            "docker",
            "volume",
            "ls",
            "--filter",
            label,
            "--format",
            "{{.Name}}",
        ],
        "network": [
            "docker",
            "network",
            "ls",
            "--filter",
            label,
            "--format",
            "{{.Name}}",
        ],
        "image": ["docker", "image", "ls", "--filter", label, "--format", "{{.ID}}"],
    }[kind]
    out = run(command, Path.cwd(), stdout=subprocess.PIPE).stdout or ""
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


@dataclass
class Survey:
    """What is left of the stack, as found now."""

    key: str
    stack: Path
    stack_exists: bool
    site: Path
    site_exists: bool
    backups: Path | None
    local_backups: int
    operator: bool = False
    docker: dict[str, list[str]] = field(default_factory=dict)

    @property
    def in_docker(self) -> bool:
        # An image another project shares may stay; it is no part left over.
        return any(self.docker.get(k) for k in ("container", "volume", "network"))

    @property
    def anything(self) -> bool:
        return self.stack_exists or self.site_exists or self.in_docker

    @property
    def database_left(self) -> bool:
        """The stack's database volume is still there, so an export can read it."""
        volume = f"{project(self.key)}_{DATABASE_VOLUME}"
        return volume in self.docker.get("volume", [])

    def lines(self) -> list[str]:
        lines: list[str] = []
        for kind in ("container", "volume", "network", "image"):
            for name in self.docker.get(kind, []):
                lines.append(f"{kind} {name}")
        if self.site_exists:
            lines.append(f"site file {self.site}, then the shared Caddy reloads")
        if self.operator:
            lines.append(
                f"the operator dashboard leaves {project(self.key)}_ops "
                "and its registry"
            )
        if self.stack_exists:
            lines.append(f"folder {self.stack} (code, .env and its tokens)")
        if self.local_backups:
            lines.append(f"{self.local_backups} backup(s) in {self.backups}")
        return lines


def _backups(stack: Path, env: dict[str, str]) -> tuple[Path, int]:
    """The stack's backup folder, which must lie inside the stack folder: the
    file names (maex_maex_agent_<time>.dump.gpg) are the same for every stack."""
    folder = Path(env.get("BACKUP_DIR") or "backups")
    if not folder.is_absolute():
        folder = stack / folder
    resolved, inside = folder.resolve(), stack.resolve()
    if resolved != inside and inside not in resolved.parents:
        raise Refused(
            f"BACKUP_DIR {folder} lies outside {stack}: its dumps cannot be told "
            "apart from another stack's. Move them into the stack folder or delete "
            "them by hand, then set BACKUP_DIR back and run again"
        )
    count = len(list(resolved.glob("maex_*.dump*"))) if resolved.is_dir() else 0
    return resolved, count


def survey(root: Path, key: str) -> Survey:
    stack = root / key
    if stack.is_symlink():
        raise Refused(f"{stack} is a link; remove what it points to by hand")
    site = root / "caddy" / "sites" / f"{key}.caddy"
    backups, local = None, 0
    if stack.is_dir() and (stack / ".env").is_file():
        env = read_env(stack / ".env")
        if env.get("COMPOSE_PROJECT_NAME") != project(key):
            raise Refused(
                f"{stack}/.env belongs to another project, not {project(key)}"
            )
        backups, local = _backups(stack, env)
    elif stack.exists():
        raise Refused(f"{stack} has no .env: not a stack of new_stack.py, not touched")
    found = Survey(key, stack, stack.is_dir(), site, site.is_file(), backups, local)
    found.operator = (root / "ops" / ".env").is_file()
    found.docker = {
        kind: _docker_ids(kind, key)
        for kind in ("container", "volume", "network", "image")
    }
    return found


def export_command(folder: Path, key: str) -> list[str]:
    """The job in the stack's api image, as the deploy user, the folder mounted."""
    return [
        *("docker", "compose", "-p", project(key), "run", "--rm", "-T"),
        *("--user", f"{os.getuid()}:{os.getgid()}"),
        *("-v", f"{folder.resolve()}:/export", "api"),
        *("python", "-m", "api.jobs.offboarding", "export", "/export", "--key", key),
    ]


def write_export(root: Path, key: str) -> dict[str, Any]:
    stack = root / key
    if not (stack / ".env").is_file():
        raise Refused(f"{stack} is not a stack (no .env); nothing to export from")
    if read_env(stack / ".env").get("COMPOSE_PROJECT_NAME") != project(key):
        raise Refused(f"{stack}/.env belongs to another project, not {project(key)}")
    folder = export_folder(root, key)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    run(export_command(folder, key), stack, env=compose_env())
    return check_export(folder, key)


def take_off(root: Path, key: str) -> None:
    """The site file out and the shared Caddy reloaded without the stack's
    network; a refusal puts the file back as it was (new_stack.load_site).
    Both networks files come from the files on disk as new_stack writes them,
    so the shared Caddy keeps the operator stack's `edge`."""
    path = root / "caddy" / "sites" / f"{key}.caddy"
    before = path.read_text()
    path.unlink()
    new_stack._write_networks(root)
    config = ("--config", "/etc/caddy/Caddyfile")
    try:
        command, caddy_env = caddy(root, "up", "-d")
        run(command, root, env=caddy_env)
        command, caddy_env = caddy(root, "exec", "-T", "caddy")
        run([*command, "caddy", "validate", *config], root, env=caddy_env)
        run([*command, "caddy", "reload", *config], root, env=caddy_env)
    except Refused:
        write_private(path, before, 0o640)
        new_stack._write_networks(root)
        raise Refused(
            f"the shared Caddy refused the change; {path.name} is back and nothing "
            "of the stack was removed, but it is stopped: `docker compose up -d` in "
            "its folder if it should serve again, or fix the shared Caddy and rerun"
        ) from None


def leave_operator(root: Path, key: str) -> None:
    """The counterpart of new_stack.join_operator: the dashboard is recreated
    without the stack's `ops` network (Docker removes no network a container
    still stands on), and the customer leaves its registry. A rerun changes
    only what is left."""
    ops = root / "ops"
    sites = list(site_files(root / "caddy" / "sites"))
    (ops / "networks.json").write_text(operator_networks_file(sites))
    env = compose_env()
    dashboard = ["docker", "compose", "-p", OPERATOR_PROJECT]
    run([*dashboard, "up", "-d", "--wait", "dashboard"], ops, env=env)
    cli = [*dashboard, "exec", "-T", "dashboard", "python", "-m", "api.dashboard.cli"]
    listed = run([*cli, "customers"], ops, env=env, stdout=subprocess.PIPE).stdout
    if any(line.split(":", 1)[0] == key for line in (listed or "").splitlines()):
        run([*cli, "customer-remove", key], ops, env=env)


def remove_folder(stack: Path, root: Path) -> None:
    """Everything but `.env` first: a step that fails halfway (a file a
    container wrote as root) leaves the `.env` that tells a rerun whose
    folder this is."""
    rest = sorted(str(p) for p in stack.iterdir() if p.name != ".env")
    if rest:
        run(["rm", "-rf", "--one-file-system", "--", *rest], root)
    run(["rm", "-rf", "--one-file-system", "--", str(stack)], root)


def _record(root: Path, entry: dict[str, Any]) -> Path:
    """Appended, never rewritten: the proof of every removal on this host."""
    folder = root / "offboarding"
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = folder / LEDGER
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return path


def remove(root: Path, key: str, now: datetime) -> dict[str, Any] | None:
    """Remove what is left of the stack. Returns the record, or None when
    nothing was left. Raises Refused before anything goes when a check fails."""
    found = survey(root, key)
    if not found.anything:
        return None
    folder = export_folder(root, key)
    check_export(folder, key)
    removed = found.lines()
    env = compose_env()
    if found.stack_exists and found.database_left:
        run(compose(key, "stop"), found.stack, env=env)
        # Nothing writes now: the last export holds the stack's last state.
        # Without the database volume (a rerun after `down`) the first run's
        # export is the last state; a run here would start an empty database.
        run(export_command(folder, key), found.stack, env=env)
    manifest = check_export(folder, key)
    if found.site_exists:
        take_off(root, key)
    if found.operator:
        leave_operator(root, key)
    if found.stack_exists and found.in_docker:
        down = ["down", "--volumes", "--remove-orphans", "--rmi", "local"]
        run(compose(key, *down), found.stack, env=env)
    # What `down` left, or what a run before left without its folder.
    if left := _docker_ids("container", key):
        run(["docker", "rm", "-f", *left], root)
    if left := _docker_ids("volume", key):
        run(["docker", "volume", "rm", *left], root)
    if left := _docker_ids("network", key):
        run(["docker", "network", "rm", *left], root)
    if found.stack_exists:
        remove_folder(found.stack, root)
    after = survey(root, key)
    after.operator = False  # done above; no part of the stack
    still = [
        line
        for line in after.lines()
        if not line.startswith("image ")  # an image another project shares stays
    ]
    if still:
        raise Refused("still there after the removal: " + "; ".join(still))
    entry = {
        "customer_key": key,
        "project": project(key),
        "removed_at": now.isoformat(timespec="seconds"),
        "export": {
            "exported_at": manifest.get("exported_at"),
            "manifest_sha256": _digest(folder / MANIFEST),
            "rows": {n: e.get("rows") for n, e in sorted(manifest["files"].items())},
        },
        "removed": removed,
        "by_hand": [line.replace("<key>", key) for line in BY_HAND],
    }
    _record(root, entry)
    return entry


def _dry_run(root: Path, key: str) -> int:
    found = survey(root, key)
    if not found.anything:
        print(f"nothing left of {project(key)} under {root}")
        return 0
    folder = export_folder(root, key)
    whole = True
    try:
        manifest = check_export(folder, key)
    except Refused as refused:
        print(f"would refuse: {refused}")
        whole = False
    else:
        rows = ", ".join(
            f"{n} {e['rows']}" for n, e in sorted(manifest["files"].items())
        )
        print(f"export {folder} of {manifest.get('exported_at')}: {rows}")
    again = ", write the export once more" if found.database_left else ""
    print(f"would stop {project(key)}{again}, then remove:")
    for line in found.lines():
        print(f"  {line}")
    print(f"would append a line to {root / 'offboarding' / LEDGER}")
    print(f"to remove: --confirm {key}")
    return 0 if whole else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("key")
    parser.add_argument("--root", type=Path, default=Path("/srv/maex"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--export", action="store_true", help="write the export")
    action.add_argument(
        "--dry-run", action="store_true", help="the default: list, change nothing"
    )
    action.add_argument("--confirm", metavar="KEY", help="the key again: remove")
    args = parser.parse_args(argv)
    try:
        check_key(args.key)
        if args.confirm is not None and args.confirm != args.key:
            raise BadInput(f"--confirm {args.confirm!r} is not {args.key!r}")
        if args.export:
            manifest = write_export(args.root, args.key)
            print(f"export of {args.key} in {export_folder(args.root, args.key)}:")
            for name, entry in sorted(manifest["files"].items()):
                print(f"  {name}: {entry['rows']} rows")
            return 0
        if args.confirm is None:
            return _dry_run(args.root, args.key)
        args.root.mkdir(parents=True, exist_ok=True)
        # The lock of new_stack.py: both change the site files.
        with open(args.root / ".new_stack.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entry = remove(args.root, args.key, datetime.now(UTC))
        if entry is None:
            print(f"nothing left of {project(args.key)}; no line written")
            return 0
        print(f"{project(args.key)} removed:")
        for line in entry["removed"]:
            print(f"  {line}")
        print("left to do by hand:")
        for line in entry["by_hand"]:
            print(f"  {line}")
    except Refused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2 if isinstance(refused, BadInput) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
