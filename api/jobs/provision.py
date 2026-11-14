"""Create a new customer's tenant in its stack, and say what it still lacks (#573, D32).

Call, in the new stack's api container after its migrations:
    python -m api.jobs.provision create customer.json --dry-run
    python -m api.jobs.provision create customer.json [--base-url https://agent.example.com]
    python -m api.jobs.provision check

`create` reads the customer file (format: deploy/customer.example.json,
`api/schemas/provision.py`) and writes tenant, service config (call mode
`shadow`), weekly hours and seats, and the first setup link, in one
transaction. It prints one JSON object: what it wrote, the setup link (shown
only now, stored hashed) and what is still missing. A rerun with the same key
changes nothing; any other tenant in the database refuses (one customer per
stack). `--dry-run` checks and counts, writes nothing, issues no link.
`check` lists what the stack's tenant still lacks: menu, zones, print bridge,
login and the like.

Exit code: 0 done (create) or nothing missing (check), 1 file or stack
refused, or something missing, 2 file or tenant not found.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from api.core.errors import InvalidInput
from api.core.time import to_local, utcnow
from api.db import SessionLocal
from api.domain.access.sessions import stack_tenant_id
from api.domain.provision import create_tenant, missing
from api.jobs.gui_access import setup_url
from api.schemas.provision import CustomerSpec

ACTOR = "operator:cli"


def _create(args: argparse.Namespace) -> int:
    if not args.file.is_file():
        print(f"file not found: {args.file}", file=sys.stderr)
        return 2
    try:
        spec = CustomerSpec.model_validate_json(args.file.read_text(encoding="utf-8"))
    except ValidationError as refused:
        # Location and reason only: the values are the customer's.
        for error in refused.errors(include_input=False):
            where = ".".join(str(part) for part in error["loc"]) or "file"
            print(f"error: {where}: {error['msg']}", file=sys.stderr)
        return 1
    with SessionLocal() as session:
        try:
            done = create_tenant(session, spec, ACTOR, utcnow(), args.dry_run)
        except InvalidInput as refused:
            print(f"refused: {refused.message}", file=sys.stderr)
            return 1
        gaps = missing(session, done.tenant_id) if done.tenant_id else []
    report = {
        "customer_key": spec.key,
        "tenant_id": str(done.tenant_id) if done.tenant_id else None,
        "created": done.created,
        "dry_run": args.dry_run,
        "opening_hours": done.opening_hours,
        "seats": done.seats,
        "setup_link": setup_url(done.setup_token, args.base_url)
        if done.setup_token
        else None,
        "missing": gaps,
    }
    print(json.dumps(report, indent=2))
    if done.link_expires_at:
        until = to_local(done.link_expires_at, spec.timezone)
        print(
            f"Setup link valid until {until:%d.%m.%Y %H:%M}, once. Shown only now.",
            file=sys.stderr,
        )
    elif not done.created:
        print(f"{spec.key} exists, nothing changed.", file=sys.stderr)
    return 0


def _check(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        tenant_id = stack_tenant_id(session)
        if tenant_id is None:
            print("No tenant in this database.", file=sys.stderr)
            return 2
        gaps = missing(session, tenant_id)
    for gap in gaps:
        print(f"missing: {gap}")
    return 1 if gaps else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="the customer's tenant from its file")
    create.add_argument("file", type=Path)
    create.add_argument("--dry-run", action="store_true", help="check, write nothing")
    create.add_argument("--base-url", default="")
    create.set_defaults(run=_create)
    commands.add_parser("check", help="what the tenant still lacks").set_defaults(
        run=_check
    )
    args = parser.parse_args(argv)
    run: Callable[[argparse.Namespace], int] = args.run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
