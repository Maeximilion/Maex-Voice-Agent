"""The operator's commands on the operator host (docs/23 §5 and §8): accounts,
the break glass when every factor is lost, and the customers the dashboard
looks after.

    python -m api.dashboard.cli add <name>
    python -m api.dashboard.cli reset <name>
    python -m api.dashboard.cli customer-add <key> http://<api container>:8000
    python -m api.dashboard.cli customer-remove <key>
    python -m api.dashboard.cli customers

`add` and `reset` ask the password twice, never as an argument (it would stay
in the shell history and the process list), and print the TOTP link for the
authenticator app and ten recovery codes, once. `reset` also ends every session,
removes every passkey and forgets every device of the account. SSH to the host is root anyway, so
this needs no second factor of its own.

`customer-add` takes the stack's `OPS_TOKEN` the same way: asked for, or read
from standard input when it is piped in, never as an argument. Run again for a
customer it replaces the address and the token (after a rotation). The token is
never printed; `customers` lists keys and addresses only.
"""

import argparse
import sys
from getpass import getpass

from api.dashboard import totp
from api.dashboard.access import audit, create_account, reset_account
from api.dashboard.customers import (
    BadCustomer,
    add_customer,
    list_customers,
    remove_customer,
)
from api.dashboard.db import SessionLocal

ACTOR = "cli"


def _account(command: str, name: str) -> int:
    password = getpass("New password: ")
    if password != getpass("Once more: "):
        print("The two entries differ.", file=sys.stderr)
        return 1
    action = create_account if command == "add" else reset_account
    with SessionLocal() as db:
        try:
            factors = action(db, name, password)
        except (ValueError, LookupError) as problem:
            print(problem, file=sys.stderr)
            return 1
    name = name.strip().lower()
    # The one place the secret is shown: inside the link (its `secret=` value is
    # what an app without link import takes typed in).
    print("Add this link to the authenticator app, or type in its secret= value:")
    print(f"  {totp.uri(factors.totp_secret, name)}")
    print("Recovery codes, each works once. Keep them offline:")
    for code in factors.recovery_codes:
        print(f"  {code}")
    return 0


def _customer_add(key: str, url: str) -> int:
    token = (
        getpass("OPS_TOKEN of the stack: ")
        if sys.stdin.isatty()
        else sys.stdin.readline()
    )
    with SessionLocal() as db:
        try:
            row = add_customer(db, key, url, token)
        except BadCustomer as problem:
            print(problem, file=sys.stderr)
            return 1
        audit(db, ACTOR, "customer.added", {"api_url": row.api_url}, target=row.key)
        db.commit()
    print(f"{row.key}: {row.api_url}")
    return 0


def _customer_remove(key: str) -> int:
    with SessionLocal() as db:
        if not remove_customer(db, key):
            print(f"no customer {key}", file=sys.stderr)
            return 1
        audit(db, ACTOR, "customer.removed", {}, target=key.strip())
        db.commit()
    return 0


def _customers() -> int:
    with SessionLocal() as db:
        for row in list_customers(db):
            print(f"{row.key}: {row.api_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.dashboard.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "reset"):
        commands.add_parser(name).add_argument("name")
    adding = commands.add_parser("customer-add")
    adding.add_argument("key")
    adding.add_argument("url")
    commands.add_parser("customer-remove").add_argument("key")
    commands.add_parser("customers")
    args = parser.parse_args(argv)
    if args.command in ("add", "reset"):
        return _account(args.command, args.name)
    if args.command == "customer-add":
        return _customer_add(args.key, args.url)
    if args.command == "customer-remove":
        return _customer_remove(args.key)
    return _customers()


if __name__ == "__main__":
    raise SystemExit(main())
