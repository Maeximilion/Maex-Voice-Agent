"""The self-test of a stack (#574, sim/selftest.py): connected on a provisioned
stack with a menu, the first failing check named otherwise, and the simulated
reservation leaves no row behind and reaches nobody."""

from datetime import UTC, datetime

import httpx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.menu.importer import apply, parse
from api.domain.ordering.handover import ack_ticket, claim_tickets
from api.domain.ordering.selftest_slip import queue_selftest_slip, remove_selftest_slip
from api.domain.provision import create_tenant
from api.events.dispatcher import dispatch_once
from api.models import (
    AuditLog,
    Call,
    Customer,
    ItemAllergen,
    MenuItem,
    OutboxEvent,
    Reservation,
    ServiceConfig,
    Tenant,
)
from api.tests.conftest import scratch_db_at
from api.tests.test_domain_menu_search import KARTE
from api.tests.test_domain_provision import TEAM, _spec
from sim import selftest, selftest_call
from sim.selftest import ALEMBIC_INI, check_database, run

# Monday morning: the provisioned seats are Tuesday to Sunday, 17:00 to 22:00.
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
TABLES = (Call, Reservation, OutboxEvent, AuditLog, Customer)


def healthy() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"status": "ok", "env": "test"})
        )
    )


@pytest.fixture
def engine(migrated_db_url, monkeypatch):
    monkeypatch.setattr(settings, "team_phone", TEAM)
    monkeypatch.setattr(settings, "transfer_targets", "")
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        create_tenant(session, _spec(), "operator:test", NOW, False)
    yield engine
    engine.dispose()


@pytest.fixture
def stack(engine):
    """A provisioned stack with its menu imported."""
    with Session(engine) as session:
        tenant_id = session.scalars(select(Tenant.id)).one()
        plan = parse(KARTE)
        assert plan.ok, plan.errors
        apply(session, tenant_id, plan, now=NOW)
    return engine


def counts(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            t.__tablename__: session.scalar(select(func.count()).select_from(t)) or 0
            for t in TABLES
        }


def selftest_run(engine, **options) -> tuple[int, list[str]]:
    lines: list[str] = []
    options.setdefault("now", NOW)
    with options.pop("client", None) or healthy() as client:
        code = run(engine, client, out=lines.append, **options)
    return code, lines


def test_a_provisioned_stack_with_a_menu_is_connected_and_keeps_no_row(stack):
    before = counts(stack)

    code, lines = selftest_run(stack)

    assert code == 0, lines
    assert lines[-1] == "connected"
    checks = [line.split(":")[0] for line in lines if line.startswith("ok")]
    assert checks == [
        f"ok    {name}"
        for name in ("health", "database", "configuration", "menu", "reservation")
    ]
    reservation = next(line for line in lines if "reservation:" in line)
    assert "check_slot create_reservation confirm" in reservation
    assert "10.11.2026 17:" in reservation  # Tuesday evening, Berlin time
    assert any(line.startswith("skip  print") for line in lines)
    assert counts(stack) == before


def test_the_menu_line_counts_dishes_without_allergen_information(stack):
    with Session(stack) as session:
        first = session.scalars(select(MenuItem.id).where(MenuItem.active)).first()
        for code in ("A", "C"):
            session.add(
                ItemAllergen(
                    menu_item_id=first,
                    allergen_code=code,
                    confirmed_by="owner",
                    confirmed_at=NOW,
                )
            )
        session.commit()
        dishes = (
            session.scalar(
                select(func.count()).select_from(MenuItem).where(MenuItem.active)
            )
            or 0
        )
        with_info = (
            session.scalar(
                select(func.count(func.distinct(ItemAllergen.menu_item_id)))
                .join(MenuItem, MenuItem.id == ItemAllergen.menu_item_id)
                .where(MenuItem.active)
            )
            or 0
        )
    assert with_info == 1

    _code, lines = selftest_run(stack)

    menu = next(line for line in lines if " menu:" in line)
    assert f"{dishes} active dishes" in menu
    assert f"{dishes - with_info} without allergen information (allowed" in menu


def test_a_stack_without_a_menu_names_the_menu_and_runs_no_call(engine):
    before = counts(engine)

    code, lines = selftest_run(engine)

    assert code == selftest.CODES["menu"] == 6
    assert lines[-1] == "not connected: menu"
    assert "FAIL  menu: no active dish (python -m scripts.import_menu)" in lines
    assert not any("reservation" in line for line in lines)
    assert counts(engine) == before


def test_a_database_one_migration_behind_is_named():
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    (head,) = script.get_heads()
    behind = script.get_revision(head).down_revision
    assert isinstance(behind, str)
    with scratch_db_at(behind) as url:
        engine = create_engine(url)
        try:
            code, lines = selftest_run(engine)
            with pytest.raises(selftest.Failed, match=f"at {behind}, the code wants"):
                check_database(engine)
        finally:
            engine.dispose()

    assert code == selftest.CODES["database"]
    assert lines[-1] == "not connected: database"


def test_an_empty_database_is_not_migrated(scratch_db_url):
    engine = create_engine(scratch_db_url)
    try:
        with pytest.raises(selftest.Failed, match="not migrated"):
            check_database(engine)
    finally:
        engine.dispose()


def test_a_database_that_does_not_answer_is_named():
    engine = create_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    with pytest.raises(selftest.Failed, match="not reachable"):
        check_database(engine)


def test_an_api_that_does_not_answer_stops_at_health(stack):
    down = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )

    code, lines = selftest_run(stack, client=down)

    assert code == selftest.CODES["health"]
    assert lines == [
        f"FAIL  health: {selftest.HEALTH_URL} answered 503, not ok",
        "not connected: health",
    ]


def test_the_health_check_reads_the_real_route(stack):
    from api.main import app

    code, lines = selftest_run(
        stack, client=TestClient(app), health_url="http://testserver/health"
    )

    assert code == 0, lines
    assert lines[0] == "ok    health: http://testserver/health ok, env test"


def test_reservations_switched_off_fail_the_call_with_the_reason(stack):
    with Session(stack) as session:
        session.execute(update(ServiceConfig).values(reservations_enabled=False))
        session.commit()

    code, lines = selftest_run(stack)

    assert code == selftest.CODES["reservation"]
    assert "FAIL  reservation: reservations are switched off on the tablet" in lines


def test_the_dispatcher_sees_nothing_while_the_call_holds_its_event(stack, monkeypatch):
    """The team's outbox event exists inside the test's transaction and nowhere
    else: a dispatcher pass at that moment sends nothing to n8n."""
    sent: list[OutboxEvent] = []
    checked = selftest_call._check_booking

    def send(event: OutboxEvent) -> None:
        sent.append(event)

    def with_a_dispatcher_pass(session, *args):
        report = checked(session, *args)
        with Session(stack) as other:
            dispatch_once(other, send, limit=10)
        assert session.scalar(
            select(func.count()).where(
                OutboxEvent.payload["call_id"].astext == str(report.call_id)
            )
        )
        return report

    monkeypatch.setattr(selftest_call, "_check_booking", with_a_dispatcher_pass)

    code, lines = selftest_run(stack)

    assert code == 0, lines
    assert sent == []


def test_a_call_that_fails_after_the_booking_leaves_no_row(stack, monkeypatch):
    before = counts(stack)

    def broken(*_args):
        raise selftest_call.CallFailed("checked and found wrong")

    monkeypatch.setattr(selftest_call, "_check_booking", broken)

    code, lines = selftest_run(stack)

    assert code == selftest.CODES["reservation"]
    assert "FAIL  reservation: checked and found wrong" in lines
    assert counts(stack) == before


def test_the_print_slip_goes_through_the_bridge_and_its_row_goes(stack):
    with Session(stack) as session:
        tenant_id = session.scalars(select(Tenant.id)).one()
    printed: list[dict] = []

    def bridge_polls(_seconds: float) -> None:
        with Session(stack) as session:
            for ticket in claim_tickets(session, tenant_id, datetime.now(UTC)):
                printed.append(ticket["ticket"])
                ack_ticket(session, tenant_id, ticket["id"], True, datetime.now(UTC))

    before = counts(stack)

    code, lines = selftest_run(stack, print_slip=True, sleep=bridge_polls)

    assert code == 0, lines
    assert "ok    print: the print bridge printed the test slip within 1 s" in lines
    assert [slip["customer_name"] for slip in printed] == ["SELBSTTEST"]
    assert "phone" not in printed[0]
    assert counts(stack) == before


def test_a_slip_no_bridge_fetches_is_removed_again(stack):
    before = counts(stack)

    code, lines = selftest_run(
        stack, print_slip=True, slip_wait=3, sleep=lambda _s: None
    )

    assert code == selftest.CODES["print"]
    assert lines[-2] == (
        "FAIL  print: no printed slip after 3 s (bridge off or printer)"
    )
    assert counts(stack) == before


def test_a_slip_the_bridge_holds_is_kept_and_another_event_is_never_removed(
    stack,
):
    with Session(stack) as session:
        tenant_id = session.scalars(select(Tenant.id)).one()
        slip = queue_selftest_slip(session, tenant_id, datetime.now(UTC))
        assert claim_tickets(session, tenant_id, datetime.now(UTC))
        assert remove_selftest_slip(session, slip) is False
        ack_ticket(session, tenant_id, slip, False, datetime.now(UTC), "paper out")
        assert remove_selftest_slip(session, slip) is True
        assert session.get(OutboxEvent, slip) is None

        other = OutboxEvent(
            tenant_id=tenant_id, event_type="daily.report", payload={"day": "x"}
        )
        session.add(other)
        session.commit()
        with pytest.raises(ValueError, match="no test slip"):
            remove_selftest_slip(session, other.id)
        assert session.get(OutboxEvent, other.id) is not None


def test_an_unexpected_error_in_a_check_is_one_line_and_its_exit_code(
    stack, monkeypatch
):
    def broken(*_args):
        raise RuntimeError("model gone")

    monkeypatch.setattr(selftest, "reservation_call", broken)

    code, lines = selftest_run(stack)

    assert code == selftest.CODES["reservation"]
    assert lines[-2:] == [
        "FAIL  reservation: RuntimeError: model gone",
        "not connected: reservation",
    ]
