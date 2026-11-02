"""Requests of data subjects on the server (T-11.8, docs/09 §2).

Call, from the stack's folder:
    python -m api.jobs.data_requests log --kind erasure --channel mail --note "..."
    python -m api.jobs.data_requests open
    python -m api.jobs.data_requests find dsr_2026_001 --name "..." --on 2026-10-12
    python -m api.jobs.data_requests erase dsr_2026_001 --name "..." --on 2026-10-12
    python -m api.jobs.data_requests object dsr_2026_001 --name "..." --on 2026-10-12
    python -m api.jobs.data_requests answer dsr_2026_001 --outcome done
    python -m api.jobs.data_requests extend dsr_2026_001

The verified person comes from `--name` with `--on` (repeatable) or with
`--street`, `--house-number` and `--postal-code`, never from a number.

stdout is what the operator keeps: the copy of `find` and the lines of the
erasure ledger of `erase` and `object` (`date,table,row_id,action`), to be
appended to the ledger file on the host; the container's root is read-only and
the ledger stays outside the database and its backups (U7). The ledger is
written and flushed before the commit: a line too many is erased again by a
replay, a line missing would bring a row back after a restore. Everything else
goes to stderr.

Exit code: 0 done, 1 refused (nothing changed), 2 tenant not found.
"""

import argparse
import csv
import json
import sys
import uuid
from collections.abc import Callable
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import AppError
from api.core.time import to_local, tz, utcnow
from api.db import SessionLocal
from api.domain.customers.requests import (
    Details,
    LedgerEntry,
    Person,
    access_copy,
    answer_request,
    erase,
    extend_deadline,
    get_request,
    log_request,
    open_requests,
    record_objection,
    verify,
)
from api.models import Tenant
from api.models.customers import REQUEST_CHANNELS, REQUEST_KINDS, REQUEST_OUTCOMES


def _local_midnight(day: date, zone: str) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz(zone))


def _person(session: Session, tenant: Tenant, args: argparse.Namespace) -> Person:
    details = Details(
        name=args.name,
        days=tuple(args.on),
        street=args.street,
        house_number=args.house_number,
        postal_code=args.postal_code,
    )
    return verify(
        session, tenant.id, details, tenant.timezone, utcnow(), claimed=args.claim
    )


def _write_ledger(ledger: list[LedgerEntry], tenant: Tenant) -> None:
    day = to_local(utcnow(), tenant.timezone).date().isoformat()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerows((day, *entry) for entry in ledger)
    sys.stdout.flush()


def _log(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    received_at = utcnow()
    if args.received_on:
        received_at = _local_midnight(args.received_on, tenant.timezone)
    request = log_request(
        session,
        tenant.id,
        kind=args.kind,
        channel=args.channel,
        note=args.note,
        received_at=received_at,
        zone=tenant.timezone,
        reference=args.reference,
    )
    session.commit()
    print(
        json.dumps(
            {"reference": request.reference, "deadline": request.deadline.isoformat()}
        )
    )


def _open(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    rows = [
        {
            "reference": request.reference,
            "kind": request.kind,
            "deadline": request.deadline.isoformat(),
        }
        for request in open_requests(session, tenant.id)
    ]
    print(json.dumps(rows, indent=2))


def _extend(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    request = get_request(session, tenant.id, args.reference)
    extend_deadline(session, request, tenant.timezone)
    session.commit()
    print(
        json.dumps({"reference": request.reference, "deadline": str(request.deadline)})
    )


def _answer(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    answered_at = utcnow()
    if args.answered_on:
        answered_at = _local_midnight(args.answered_on, tenant.timezone)
    request = get_request(session, tenant.id, args.reference)
    answer_request(session, request, args.outcome, answered_at, tenant.timezone)
    session.commit()


def _find(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    get_request(session, tenant.id, args.reference)
    copy = access_copy(session, tenant.id, _person(session, tenant, args))
    print(json.dumps(copy, default=str, ensure_ascii=False, indent=2))


def _erase(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    request = get_request(session, tenant.id, args.reference)
    ledger = erase(session, request, _person(session, tenant, args), keep=args.keep)
    _finish(session, tenant, args.reference, ledger)


def _object(session: Session, tenant: Tenant, args: argparse.Namespace) -> None:
    request = get_request(session, tenant.id, args.reference)
    ledger = record_objection(session, request, _person(session, tenant, args))
    _finish(session, tenant, args.reference, ledger)


def _finish(
    session: Session, tenant: Tenant, reference: str, ledger: list[LedgerEntry]
) -> None:
    _write_ledger(ledger, tenant)
    session.commit()
    report = {"reference": reference, "ledger_lines": len(ledger)}
    print(json.dumps(report), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    tenant = argparse.ArgumentParser(add_help=False)
    tenant.add_argument("--tenant-name", default=settings.tenant_name)
    reference = argparse.ArgumentParser(add_help=False)
    reference.add_argument("reference")
    person = argparse.ArgumentParser(add_help=False)
    person.add_argument("--name", required=True)
    person.add_argument("--on", action="append", default=[], type=date.fromisoformat)
    person.add_argument("--street")
    person.add_argument("--house-number")
    person.add_argument("--postal-code")
    person.add_argument("--claim", action="append", default=[], type=uuid.UUID)

    commands = parser.add_subparsers(dest="command", required=True)
    log = commands.add_parser("log", parents=[tenant])
    log.add_argument("--kind", required=True, choices=REQUEST_KINDS)
    log.add_argument("--channel", required=True, choices=REQUEST_CHANNELS)
    log.add_argument("--note")
    log.add_argument("--reference")
    log.add_argument("--received-on", type=date.fromisoformat)
    log.set_defaults(run=_log)
    commands.add_parser("open", parents=[tenant]).set_defaults(run=_open)
    commands.add_parser("extend", parents=[tenant, reference]).set_defaults(run=_extend)
    answer = commands.add_parser("answer", parents=[tenant, reference])
    answer.add_argument("--outcome", required=True, choices=REQUEST_OUTCOMES)
    answer.add_argument("--answered-on", type=date.fromisoformat)
    answer.set_defaults(run=_answer)
    commands.add_parser("find", parents=[tenant, reference, person]).set_defaults(
        run=_find
    )
    eraser = commands.add_parser("erase", parents=[tenant, reference, person])
    eraser.add_argument("--keep", action="append", default=[], type=uuid.UUID)
    eraser.set_defaults(run=_erase)
    commands.add_parser("object", parents=[tenant, reference, person]).set_defaults(
        run=_object
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run: Callable[[Session, Tenant, argparse.Namespace], None] = args.run
    with SessionLocal() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.name == args.tenant_name))
        if tenant is None:
            print(f"tenant not found: {args.tenant_name}", file=sys.stderr)
            return 2
        try:
            run(session, tenant, args)
        except AppError as exc:
            print(exc.message, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
