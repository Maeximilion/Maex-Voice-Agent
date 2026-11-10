"""Operator accounts on the operator host (docs/23 §5): the first account, and
the break glass when every factor is lost.

    python -m api.dashboard.cli add <name>
    python -m api.dashboard.cli reset <name>

Both ask the password twice, never as an argument (it would stay in the shell
history and the process list), and print the TOTP link for the authenticator
app and ten recovery codes, once. `reset` also ends every session and forgets
every device of the account. SSH to the host is root anyway, so this needs no
second factor of its own.
"""

import argparse
import sys
from getpass import getpass

from api.dashboard import totp
from api.dashboard.access import create_account, reset_account
from api.dashboard.db import SessionLocal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.dashboard.cli")
    parser.add_argument("command", choices=["add", "reset"])
    parser.add_argument("name")
    args = parser.parse_args(argv)
    password = getpass("New password: ")
    if password != getpass("Once more: "):
        print("The two entries differ.", file=sys.stderr)
        return 1
    action = create_account if args.command == "add" else reset_account
    with SessionLocal() as db:
        try:
            factors = action(db, args.name, password)
        except (ValueError, LookupError) as problem:
            print(problem, file=sys.stderr)
            return 1
    name = args.name.strip().lower()
    # The one place the secret is shown: inside the link (its `secret=` value is
    # what an app without link import takes typed in).
    print("Add this link to the authenticator app, or type in its secret= value:")
    print(f"  {totp.uri(factors.totp_secret, name)}")
    print("Recovery codes, each works once. Keep them offline:")
    for code in factors.recovery_codes:
        print(f"  {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
