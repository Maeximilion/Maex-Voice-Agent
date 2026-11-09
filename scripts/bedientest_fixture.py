"""Throwaway stack and invented data for the operating test (T-3.5).

    python3 -m scripts.bedientest_fixture up      [--bind ADDR] [--port N] [--dry-run]
    python3 -m scripts.bedientest_fixture reset   [--dry-run]     # fresh data, same stack
    python3 -m scripts.bedientest_fixture arrive a|e|callback
    python3 -m scripts.bedientest_fixture down    [--dry-run]

Section 0 of `docs/vorlagen/bedientest_protokoll.md` as a script. `up` starts a Compose
project of its own (`bedientest`: database and api only, so no dispatcher, no print
bridge, no n8n and `KITCHEN_BRIDGE_TENANT_ID` empty), migrates, and loads an invented
menu, the orders B, C, D, the red-card order, a complaint callback and a reservation.
`reset` drops that project's volume and does it again, which is the "rebuilt from
scratch" the protocol asks for before every tester. `arrive` creates the live arrivals
A, E and the callback while the tester works. Nothing here can reach the working stack:
the project name is fixed and `down -v` only ever names it.

This file drives Docker and needs only the standard library. The data is made by
`scripts/bedientest_data.py` inside the api container, against the database of that
stack. Invented data only. The dev `reset` keeps the images; `up` rebuilds the api image.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = "bedientest"
TENANT = "Bedientest"
REPO = Path(__file__).resolve().parent.parent
WORK_DIR = Path(tempfile.gettempdir()) / PROJECT
# Not 8000 and 5432: the working stack on the same PC keeps those.
DEFAULT_PORT = 18000
LOOPBACK = "127.0.0.1"

# --- on the host: the throwaway Compose project --------------------------------------


def compose(*args: str) -> list[str]:
    """`docker compose` on the throwaway project, the base file and the generated
    override only (never the repo's own override or `.env`)."""
    return [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "--project-directory",
        str(REPO),
        # Not the checkout's `.env`: the values come from STACK_ENV below.
        "--env-file",
        "/dev/null",
        "-f",
        str(REPO / "docker-compose.yml"),
        "-f",
        str(WORK_DIR / "override.yml"),
        *args,
    ]


def write_config(bind: str, port: int) -> None:
    WORK_DIR.mkdir(mode=0o700, exist_ok=True)
    (WORK_DIR / "override.yml").write_text(
        f'services:\n  api:\n    ports:\n      - "{bind}:{port}:8000"\n'
    )


# Handed to Compose through the process environment, never written to a file. Fixed and
# not secret: the database is not published, the stack holds invented data only, and
# `ENV=dev` takes a known token (api/config.py LOCAL_ENVS).
STACK_ENV = {
    "POSTGRES_USER": "maex",
    "POSTGRES_PASSWORD": "bedientest",
    "POSTGRES_DB": "maex_agent",
    "DATABASE_URL": "postgresql+psycopg://maex:bedientest@db:5432/maex_agent",
    "AGENT_API_TOKEN": "bedientest-token",
    "ENV": "dev",
    "TENANT_NAME": TENANT,
    "TENANT_TIMEZONE": "Europe/Berlin",
    "KITCHEN_BRIDGE_TENANT_ID": "",
}


def run(cmd: list[str], dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True, env={**docker_environment(), **STACK_ENV})


def docker_environment() -> dict[str, str]:
    """Only what the docker client needs. Anything else exported in the caller's shell
    (DATABASE_URL, N8N_WEBHOOK_URL, COMPOSE_FILE ...) would reach the stack, because
    Compose reads the shell before it reads a file, and could point it at the working
    stack."""
    keep = ("PATH", "HOME", "USER", "LANG", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")
    return {
        k: v
        for k, v in os.environ.items()
        if k in keep or k.startswith("DOCKER_") or k.lower().endswith("_proxy")
    }


def up(args: argparse.Namespace, build: bool) -> None:
    # `reset` keeps the bind and port of the last `up`.
    if not args.dry_run and (build or not (WORK_DIR / "override.yml").exists()):
        write_config(args.bind, args.port)
    run(compose("down", "-v", "--remove-orphans"), args.dry_run)
    run(
        compose("up", "-d", "--wait", *(["--build"] if build else []), "db", "api"),
        args.dry_run,
    )
    run(
        compose(
            "exec", "-T", "api", "alembic", "-c", "db/alembic.ini", "upgrade", "head"
        ),
        args.dry_run,
    )
    run(compose("exec", "-T", "api", *INNER, "fill"), args.dry_run)
    # The view needs the restaurant's login (#570), and the fresh volume has none.
    base = f"http://{args.bind}:{args.port}"
    run(compose("exec", "-T", "api", *ACCESS, "--base-url", base), args.dry_run)
    print(
        "Tablet: open the link above once, set any password, team PIN and admin PIN, "
        f"then the board is at {base}/gui/; stack `{PROJECT}`"
    )
    if args.bind != LOOPBACK:
        print(
            f"Note: bound to {args.bind}; the stack runs ENV=dev (no proxy secret) and is "
            "reachable from that network. Invented data only; run `down` when done."
        )


# The data layer needs the project's dependencies, so it runs in the api container.
INNER = ["python", "-m", "scripts.bedientest_data"]
ACCESS = ["python", "-m", "api.jobs.gui_access", "link"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["up", "reset", "down", "arrive"])
    parser.add_argument("what", nargs="?", choices=["a", "e", "callback"])
    parser.add_argument("--bind", default=LOOPBACK)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="needed for a --bind other than loopback (the tablet): ENV=dev has no proxy",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.bind != LOOPBACK and not args.allow_lan:
        parser.error(
            f"--bind {args.bind} would open the stack, which runs ENV=dev without the "
            "proxy check, to that network (AGENTS.md: dev ports are loopback only); "
            "add --allow-lan to do it on purpose"
        )

    if args.command == "arrive":
        if args.what is None:
            parser.error("arrive needs a, e or callback")
        run(compose("exec", "-T", "api", *INNER, "arrive", args.what), args.dry_run)
    elif args.command == "down":
        run(compose("down", "-v", "--remove-orphans"), args.dry_run)
    else:
        up(args, build=args.command == "up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
