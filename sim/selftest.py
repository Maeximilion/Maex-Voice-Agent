"""Self-test of a stack (#574): "connected", or the first check that failed.

    python -m sim.selftest [--health-url URL] [--card-seconds 60]
                           [--print-slip [--slip-wait 30]]

Run inside the stack's api container (`docker compose exec api python -m
sim.selftest`), after `api.jobs.provision create` and the menu import. One
line per check, in this order, and the run stops at the first that fails:

1. health: `/health` of the running API answers `ok`.
2. database: reachable, and at the head migration of this checkout.
3. configuration: the stack's tenant, its `service_config` and opening hours.
4. menu: active dishes, with how many have no allergen information (allowed:
   the agent then says it has none, docs/05).
5. reservation: a simulated call through the agent with the scripted stand-in
   model books a table: draft, the yes, `confirm`, then the tablet's day list
   and its event stream see it. It is a test call (`calls.is_test`): no outbox
   event, nobody is called or notified, no figure counts it. A table on today's
   business day shows on the tablet as a card marked TEST for `--card-seconds`
   (0: not at all); then the self-test deletes the call and its rows
   (`selftest_call.py`). A failed call is rolled back and keeps no row.
6. print: only with `--print-slip`, one slip marked SELBSTTEST goes to the
   print bridge, and the test waits for its answer. A slip on paper in the
   kitchen is an effect outside the server, so it is off by default.

A module of `sim/`, not of `jobs/`: it drives a call through the agent like the
text phone, and `jobs/` sits below `agent/` (docs/11 §1).

Exit code: 0 connected, 2 wrong call, else the failed check: 3 health,
4 database, 5 configuration, 6 menu, 7 reservation, 8 print.
"""

import argparse
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import distinct, exists, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.core.time import to_local, utcnow
from api.db import engine as stack_engine
from api.domain.access.sessions import stack_tenant_id
from api.domain.ordering.selftest_slip import (
    queue_selftest_slip,
    remove_selftest_slip,
    selftest_slip_status,
)
from api.domain.provision import missing
from api.models import ItemAllergen, MenuItem, OpeningHours, ServiceConfig, Tenant
from sim.selftest_call import SHOW_SECONDS, CallFailed, reservation_call

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"
HEALTH_URL = "http://127.0.0.1:8000/health"
HEALTH_TIMEOUT_SECONDS = 5.0
SLIP_WAIT_SECONDS = 30.0
POLL_SECONDS = 1.0
CODES = {
    "health": 3,
    "database": 4,
    "configuration": 5,
    "menu": 6,
    "reservation": 7,
    "print": 8,
}


class Failed(Exception):
    """A check failed; the message is its line."""


def check_health(client: httpx.Client, url: str) -> str:
    try:
        answer = client.get(url)
    except httpx.HTTPError as error:
        raise Failed(f"{url} does not answer ({type(error).__name__})") from None
    body = answer.json() if answer.status_code == 200 else {}
    if body.get("status") != "ok":
        raise Failed(f"{url} answered {answer.status_code}, not ok")
    return f"{url} ok, env {body.get('env')}"


def check_database(engine: Engine, ini: Path = ALEMBIC_INI) -> str:
    if not ini.is_file():
        raise Failed(f"{ini} not found: the checkout's migrations are missing")
    heads = set(ScriptDirectory.from_config(Config(str(ini))).get_heads())
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
    except SQLAlchemyError as error:
        raise Failed(f"not reachable ({type(error).__name__})") from None
    if not current:
        raise Failed("not migrated (bash scripts/migrate.sh)")
    if current != heads:
        raise Failed(
            f"at {', '.join(sorted(current))}, the code wants "
            f"{', '.join(sorted(heads))} (bash scripts/migrate.sh)"
        )
    return f"reachable, at head {', '.join(sorted(heads))}"


def check_configuration(session: Session) -> tuple[uuid.UUID, str, list[str]]:
    tenant_id = stack_tenant_id(session)
    if tenant_id is None:
        raise Failed("no tenant (python -m api.jobs.provision create)")
    tenant = session.get(Tenant, tenant_id)
    config = session.get(ServiceConfig, tenant_id)
    if tenant is None or config is None:
        raise Failed("no service_config (python -m api.jobs.provision create)")
    services = session.scalars(
        select(OpeningHours.service)
        .where(OpeningHours.tenant_id == tenant_id)
        .distinct()
        .order_by(OpeningHours.service)
    ).all()
    if not services:
        raise Failed("no opening hours")
    line = (
        f"{tenant.customer_key or tenant.name}, call mode {config.call_mode}, "
        f"opening hours for {', '.join(services)}"
    )
    notes = [gap for gap in missing(session, tenant_id) if not gap.startswith("menu")]
    return tenant_id, line, notes


def check_menu(session: Session, tenant_id: uuid.UUID) -> str:
    active = (MenuItem.tenant_id == tenant_id, MenuItem.active)
    dishes, categories = session.execute(
        select(func.count(MenuItem.id), func.count(distinct(MenuItem.category))).where(
            *active
        )
    ).one()
    if not dishes:
        raise Failed("no active dish (python -m scripts.import_menu)")
    unknown = session.scalar(
        select(func.count(MenuItem.id)).where(
            *active, ~exists().where(ItemAllergen.menu_item_id == MenuItem.id)
        )
    )
    return (
        f"{dishes} active dishes in {categories} categories, {unknown} without "
        "allergen information (allowed: the agent says it has none)"
    )


def check_reservation(
    engine: Engine,
    tenant_id: uuid.UUID,
    now: datetime,
    *,
    show: float = SHOW_SECONDS,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    def showing(seconds: float) -> None:
        out(f"      the tablet shows the TEST card for {seconds:.0f} s")
        sleep(seconds)

    try:
        report = reservation_call(engine, tenant_id, now, show=show, sleep=showing)
    except CallFailed as failed:
        raise Failed(str(failed)) from None
    with Session(engine) as session:
        zone = session.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id))
    when = to_local(report.reserved_for, zone or "Europe/Berlin")
    if not report.shown_today:
        card = "not today, so not on the tablet's list now"
    elif show > 0:
        card = f"on the tablet as TEST for {show:.0f} s"
    else:
        card = "not shown (--card-seconds 0)"
    return (
        f"simulated test call booked a table for {when:%d.%m.%Y %H:%M} "
        f"({' '.join(report.tools)}); draft, confirm, tablet list and event "
        f"stream saw it, no outbox event; card {card}; removed, no row kept, "
        "nobody notified"
    )


def check_print(
    engine: Engine,
    tenant_id: uuid.UUID,
    wait: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    with Session(engine) as session:
        event_id = queue_selftest_slip(session, tenant_id, utcnow())
        waited = 0.0
        status = selftest_slip_status(session, event_id)
        while status == "pending" and waited < wait:
            sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            session.expire_all()
            status = selftest_slip_status(session, event_id)
        removed = remove_selftest_slip(session, event_id)
    if status == "sent":
        return f"the print bridge printed the test slip within {waited:.0f} s"
    kept = "" if removed else "; the bridge holds it, it may still print"
    raise Failed(f"no printed slip after {wait:.0f} s (bridge off or printer){kept}")


def run(
    engine: Engine,
    client: httpx.Client,
    *,
    health_url: str = HEALTH_URL,
    print_slip: bool = False,
    slip_wait: float = SLIP_WAIT_SECONDS,
    card_seconds: float = SHOW_SECONDS,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """All checks in order; the exit code of the first failed one, 0 for none."""

    def step(check: str, do: Callable[[], str]) -> bool:
        try:
            ok, line = True, do()
        except Failed as failed:
            ok, line = False, str(failed)
        except SQLAlchemyError as error:
            # The type only: the statement's text can carry a guest's data.
            ok, line = False, f"database error ({type(error).__name__})"
        except Exception as error:  # noqa: BLE001 - a line, never a traceback
            ok, line = False, f"{type(error).__name__}: {error}"
        out(f"{'ok  ' if ok else 'FAIL'}  {check}: {line}")
        return ok

    found: dict[str, uuid.UUID] = {}
    notes: list[str] = []

    def configuration() -> str:
        with Session(engine) as session:
            found["tenant"], line, gaps = check_configuration(session)
        notes.extend(gaps)
        return line

    def menu() -> str:
        with Session(engine) as session:
            return check_menu(session, found["tenant"])

    steps: list[tuple[str, Callable[[], str]]] = [
        ("health", lambda: check_health(client, health_url)),
        ("database", lambda: check_database(engine)),
        ("configuration", configuration),
        ("menu", menu),
        (
            "reservation",
            lambda: check_reservation(
                engine,
                found["tenant"],
                now or utcnow(),
                show=card_seconds,
                out=out,
                sleep=sleep,
            ),
        ),
    ]
    if print_slip:
        steps.append(
            ("print", lambda: check_print(engine, found["tenant"], slip_wait, sleep))
        )
    for check, do in steps:
        if not step(check, do):
            out(f"not connected: {check}")
            return CODES[check]
        for note in notes:
            out(f"note  still missing: {note}")
        notes.clear()
    if not print_slip:
        out("skip  print: not asked (--print-slip sends one test slip to the kitchen)")
    out("connected")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--health-url", default=HEALTH_URL)
    parser.add_argument(
        "--print-slip", action="store_true", help="one test slip on the kitchen printer"
    )
    parser.add_argument("--slip-wait", type=float, default=SLIP_WAIT_SECONDS)
    parser.add_argument(
        "--card-seconds",
        type=float,
        default=SHOW_SECONDS,
        help="how long the tablet shows the TEST card (0: not at all)",
    )
    args = parser.parse_args(argv)

    with httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS) as client:
        return run(
            stack_engine,
            client,
            health_url=args.health_url,
            print_slip=args.print_slip,
            slip_wait=args.slip_wait,
            card_seconds=args.card_seconds,
        )


if __name__ == "__main__":
    sys.exit(main())
