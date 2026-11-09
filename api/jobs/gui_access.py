"""The operator's side of the restaurant's login, until the dashboard has it (#570, #571).

Call, in the stack's api container:
    python -m api.jobs.gui_access link [--base-url https://agent.example.com]
    python -m api.jobs.gui_access devices
    python -m api.jobs.gui_access sign-out <device id or its first 8 characters>
    python -m api.jobs.gui_access sign-out --all

`link` makes the one-time setup link (24 hours, D34 point 5): the restaurant
opens it, sets its password and team PIN, and every other device is signed out.
It creates the restaurant's login the first time. The link is printed once and
stored only as a hash; a newer link replaces an older one. Without
`--base-url` it uses `https://$AGENT_DOMAIN`, or prints the path alone.
`sign-out` is the lost tablet (T8): one device, or all of them.

The stack's restaurant is its oldest tenant, as in the operations view (D32).
Exit code: 0 done, 1 nothing matched, 2 no tenant.
"""

import argparse
import os
import sys

from sqlalchemy.orm import Session

from api.core.time import to_local, utcnow
from api.db import SessionLocal
from api.domain.access.sessions import devices, end_sessions, stack_tenant_id
from api.domain.access.setup import issue_link
from api.models import Tenant

ACTOR = "operator:cli"
SETUP_PATH = "/gui/einrichten#"


def _link(session: Session, tenant: Tenant, args: argparse.Namespace) -> int:
    token, expires_at = issue_link(session, tenant.id, ACTOR, utcnow())
    base = args.base_url or (
        f"https://{os.environ['AGENT_DOMAIN']}"
        if os.environ.get("AGENT_DOMAIN")
        else ""
    )
    until = to_local(expires_at, tenant.timezone).strftime("%d.%m.%Y %H:%M")
    print(f"{base.rstrip('/')}{SETUP_PATH}{token}")
    print(f"Valid until {until}, once. Shown only now.", file=sys.stderr)
    return 0


def _devices(session: Session, tenant: Tenant, args: argparse.Namespace) -> int:
    for row in devices(session, tenant.id):
        seen = to_local(row.last_seen_at, tenant.timezone).strftime("%d.%m. %H:%M")
        print(f"{row.id}  last seen {seen}  {row.device or '-'}")
    return 0


def _sign_out(session: Session, tenant: Tenant, args: argparse.Namespace) -> int:
    if args.all:
        ended = end_sessions(session, tenant.id, ACTOR, "operator")
    else:
        matches = [
            row.id
            for row in devices(session, tenant.id)
            if str(row.id).startswith(args.device.lower())
        ]
        if len(matches) != 1 or len(args.device) < 8:
            print(
                f"{len(matches)} devices match {args.device!r}; give at least 8 characters of one id",
                file=sys.stderr,
            )
            return 1
        ended = end_sessions(session, tenant.id, ACTOR, "operator", matches[0])
    session.commit()
    print(f"Signed out: {ended}", file=sys.stderr)
    return 0 if ended else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    link = commands.add_parser("link", help="one-time setup link, 24 hours")
    link.add_argument("--base-url", default="")
    link.set_defaults(run=_link)
    commands.add_parser("devices", help="signed-in devices").set_defaults(run=_devices)
    sign_out = commands.add_parser("sign-out", help="end one device or all")
    target = sign_out.add_mutually_exclusive_group(required=True)
    target.add_argument("device", nargs="?")
    target.add_argument("--all", action="store_true")
    sign_out.set_defaults(run=_sign_out)
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        tenant_id = stack_tenant_id(session)
        tenant = session.get(Tenant, tenant_id) if tenant_id else None
        if tenant is None:
            print("No tenant in this database.", file=sys.stderr)
            return 2
        return args.run(session, tenant, args)
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
