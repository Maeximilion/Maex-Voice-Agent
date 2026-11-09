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
import secrets
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
        "--env-file",
        str(WORK_DIR / "env"),
        "-f",
        str(REPO / "docker-compose.yml"),
        "-f",
        str(WORK_DIR / "override.yml"),
        *args,
    ]


def write_config(bind: str, port: int) -> None:
    WORK_DIR.mkdir(mode=0o700, exist_ok=True)
    password = secrets.token_hex(12)
    env = {
        "POSTGRES_USER": "maex",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "maex_agent",
        "DATABASE_URL": f"postgresql+psycopg://maex:{password}@db:5432/maex_agent",
        "AGENT_API_TOKEN": secrets.token_hex(24),
        "ENV": "dev",
        "TENANT_NAME": TENANT,
        "TENANT_TIMEZONE": "Europe/Berlin",
        "KITCHEN_BRIDGE_TENANT_ID": "",
    }
    path = WORK_DIR / "env"
    path.touch(mode=0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    (WORK_DIR / "override.yml").write_text(
        f'services:\n  api:\n    ports:\n      - "{bind}:{port}:8000"\n'
    )


def run(cmd: list[str], dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True)


def up(args: argparse.Namespace, build: bool) -> None:
    if not args.dry_run:
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
    print(f"Tablet: http://{args.bind}:{args.port}/ (reload it); stack `{PROJECT}`")


# The data layer needs the project's dependencies, so it runs in the api container.
INNER = ["python", "-m", "scripts.bedientest_data"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["up", "reset", "down", "arrive"])
    parser.add_argument("what", nargs="?", choices=["a", "e", "callback"])
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

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
