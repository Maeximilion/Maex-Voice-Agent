"""The job behind requests of data subjects (T-11.8): `python -m api.jobs.data_requests`.

Its stdout is what the operator keeps: the copy of an access request and the
lines of the erasure ledger. Everything else goes to stderr, and a ledger that
cannot be written stops the erasure.
"""

import csv
import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from api.domain.customers.requests import log_request
from api.jobs import data_requests
from api.jobs.data_requests import main
from api.models import (
    AuditLog,
    Call,
    Customer,
    DataSubjectRequest,
    Order,
    OrderItem,
    RecognitionObjection,
    Reservation,
)
from api.tests.test_domain_customers_requests import (
    OTHER_PHONE,
    PAST,
    PHONE,
    _call,
    _order,
    _reservation,
)
from api.tests.test_domain_draft_order import _tenant

TENANT = "Testbetrieb"
DETAILS = ["--name", "Müller", "--on", "2026-10-12", "--tenant-name", TENANT]
FUTURE = datetime.now(UTC) + timedelta(days=8)


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine, monkeypatch):
    monkeypatch.setattr(data_requests, "SessionLocal", sessionmaker(bind=engine))
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return _tenant(session, TENANT)


def _logged(session, tenant_id, kind="erasure", received_at=None) -> str:
    received_at = received_at or datetime.now(UTC)
    request = log_request(
        session,
        tenant_id,
        kind=kind,
        channel="mail",
        note="Frau Müller",
        received_at=received_at,
        zone="Europe/Berlin",
    )
    session.commit()
    return request.reference


def _row(session, reference) -> DataSubjectRequest:
    session.expire_all()
    return session.scalar(
        select(DataSubjectRequest).where(DataSubjectRequest.reference == reference)
    )


def test_log_prints_the_reference_and_the_deadline(session, tenant_id, capsys):
    args = ["log", "--kind", "access", "--channel", "phone", "--note", "Frau Müller"]
    assert main([*args, "--tenant-name", TENANT]) == 0
    printed = json.loads(capsys.readouterr().out)
    row = _row(session, printed["reference"])
    assert row.kind == "access"
    assert row.verification_note == "Frau Müller"
    assert printed["deadline"] == row.deadline.isoformat()
    assert "Müller" not in json.dumps(printed)


def test_log_moves_a_line_of_the_interim_file_with_its_dates(
    session, tenant_id, capsys
):
    args = [
        "log",
        "--kind",
        "erasure",
        "--channel",
        "mail",
        "--reference",
        "dsr_2026_004",
        "--received-on",
        "2026-10-30",
        "--tenant-name",
        TENANT,
    ]
    assert main(args) == 0
    row = _row(session, "dsr_2026_004")
    assert row.deadline == date(2026, 11, 30)
    assert row.verification_note is None
    answer = ["answer", "dsr_2026_004", "--outcome", "done"]
    assert main([*answer, "--answered-on", "2026-10-31", "--tenant-name", TENANT]) == 0
    assert _row(session, "dsr_2026_004").delete_after == date(2029, 12, 31)


def test_open_lists_requests_by_deadline(session, tenant_id, capsys):
    later = _logged(session, tenant_id)
    sooner = _logged(
        session, tenant_id, received_at=datetime.now(UTC) - timedelta(days=9)
    )
    assert main(["open", "--tenant-name", TENANT]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["reference"] for row in rows] == [sooner, later]


def test_extend_moves_the_deadline(session, tenant_id, capsys):
    reference = _logged(session, tenant_id, received_at=PAST)
    assert main(["extend", reference, "--tenant-name", TENANT]) == 0
    assert _row(session, reference).deadline == date(2027, 1, 12)


def test_find_prints_the_copy_and_changes_nothing(session, tenant_id, capsys):
    order = _order(session, tenant_id)
    reference = _logged(session, tenant_id, kind="access")
    assert main(["find", reference, *DETAILS]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["copy"]["orders"][0]["customer_name"] == "Müller"
    session.expire_all()
    assert session.get(Order, order.id).customer_name == "Müller"


def test_a_person_nobody_can_verify_ends_with_exit_1(session, tenant_id, capsys):
    _order(session, tenant_id, name="Schmidt")
    reference = _logged(session, tenant_id, kind="access")
    assert main(["find", reference, *DETAILS]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not verified" in captured.err


def test_two_people_of_one_day_need_the_number_they_called_from(
    session, tenant_id, capsys
):
    """Security audit 02.11.2026: never the union of two guests' numbers."""
    mine = _order(session, tenant_id)
    _order(session, tenant_id, phone=OTHER_PHONE)
    reference = _logged(session, tenant_id, kind="access")
    assert main(["find", reference, *DETAILS]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--phone" in captured.err
    assert main(["find", reference, *DETAILS, "--phone", "07221 555123"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in printed["copy"]["orders"]] == [str(mine.id)]


def test_a_number_alone_is_no_way_in(session, tenant_id, capsys):
    reference = _logged(session, tenant_id, kind="access")
    with pytest.raises(SystemExit):
        main(["find", reference, "--phone", PHONE, "--tenant-name", TENANT])


def test_erase_writes_the_ledger_to_stdout_and_commits(session, tenant_id, capsys):
    order = _order(session, tenant_id)
    call_id = order.call_id
    reference = _logged(session, tenant_id)
    assert main(["erase", reference, *DETAILS]) == 0
    captured = capsys.readouterr()
    rows = list(csv.reader(io.StringIO(captured.out)))
    assert {(row[1], row[2], row[3]) for row in rows} >= {
        ("orders", str(order.id), "cleared"),
        ("calls", str(call_id), "cleared"),
    }
    assert all(date.fromisoformat(row[0]) for row in rows)
    assert "Müller" not in captured.out
    assert PHONE not in captured.out
    report = json.loads(captured.err)
    assert report["ledger_lines"] == len(rows)
    assert report["reference"] == reference
    session.expire_all()
    assert session.get(Order, order.id).customer_name == ""


def test_a_ledger_that_cannot_be_written_stops_the_erasure(
    session, tenant_id, monkeypatch
):
    class FullDisk(io.StringIO):
        def write(self, text):
            raise OSError("no space left on device")

    order = _order(session, tenant_id)
    reference = _logged(session, tenant_id)
    monkeypatch.setattr("sys.stdout", FullDisk())
    with pytest.raises(OSError):
        main(["erase", reference, *DETAILS])
    session.expire_all()
    assert session.get(Order, order.id).customer_name == "Müller"


def test_a_ledger_that_cannot_be_flushed_stops_the_erasure(
    session, tenant_id, monkeypatch
):
    class BrokenPipe(io.StringIO):
        def flush(self):
            raise OSError("broken pipe")

    order = _order(session, tenant_id)
    reference = _logged(session, tenant_id)
    monkeypatch.setattr("sys.stdout", BrokenPipe())
    with pytest.raises(OSError):
        main(["erase", reference, *DETAILS])
    session.expire_all()
    assert session.get(Order, order.id).customer_name == "Müller"


def test_erase_with_a_claim_and_a_kept_row(session, tenant_id, capsys):
    _order(session, tenant_id)
    call = _call(session, tenant_id)
    table = _reservation(session, tenant_id, reserved_for=FUTURE)
    reference = _logged(session, tenant_id)
    claim = ["--claim", str(call.id)]
    assert main(["erase", reference, *DETAILS, *claim]) == 1
    assert str(table.id) in capsys.readouterr().err
    assert main(["erase", reference, *DETAILS, *claim, "--keep", str(table.id)]) == 0
    session.expire_all()
    assert session.get(Call, call.id).caller_id is None
    assert session.get(Reservation, table.id).guest_name == "Müller"


def test_object_records_the_number_and_prints_the_ledger(session, tenant_id, capsys):
    customer = Customer(tenant_id=tenant_id, phone=PHONE, name="Müller")
    session.add(customer)
    order = _order(session, tenant_id)
    reference = _logged(session, tenant_id, kind="objection")
    assert main(["object", reference, *DETAILS]) == 0
    out = capsys.readouterr().out
    rows = list(csv.reader(io.StringIO(out)))
    # The rows that hold the number, so that a replay finds it again (#1085).
    assert [(row[1], row[2], row[3]) for row in rows] == [
        ("customers", str(customer.id), "objected"),
        ("orders", str(order.id), "objected"),
        ("calls", str(order.call_id), "cleared"),
    ]
    assert PHONE not in out
    objections = session.scalars(select(RecognitionObjection.phone)).all()
    assert objections == [PHONE]


def test_an_objection_on_two_numbers_needs_every_number_named(
    session, tenant_id, capsys
):
    """Re-audit 02.11.2026, evening: day A on the guest's number and day B on
    a namesake's must not write an objection for the namesake's number."""
    _order(session, tenant_id)
    _order(session, tenant_id, phone=OTHER_PHONE, ready_at=PAST + timedelta(days=1))
    reference = _logged(session, tenant_id, kind="objection", received_at=PAST)
    two_days = [*DETAILS, "--on", "2026-10-13"]
    assert main(["object", reference, *two_days]) == 1
    assert "--phone" in capsys.readouterr().err
    assert session.scalars(select(RecognitionObjection.phone)).all() == []
    assert main(["object", reference, *two_days, "--phone", PHONE]) == 0
    session.expire_all()
    assert session.scalars(select(RecognitionObjection.phone)).all() == [PHONE]


def test_an_unknown_tenant_ends_with_exit_2(session, tenant_id, capsys):
    assert main(["open", "--tenant-name", "Niemand"]) == 2
    assert "tenant not found" in capsys.readouterr().err


def test_an_unknown_reference_ends_with_exit_1(session, tenant_id, capsys):
    assert (
        main(["answer", "dsr_2026_999", "--outcome", "done", "--tenant-name", TENANT])
        == 1
    )
    assert "dsr_2026_999" in capsys.readouterr().err


DUMPED = "20261101T020000Z"


def _replay(monkeypatch, *lines: str, since: str = DUMPED) -> int:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("".join(f"{line}\n" for line in lines))
    )
    return main(["replay", "--since", since, "--tenant-name", TENANT])


def test_replay_erases_again_what_a_restore_brought_back(
    session, tenant_id, monkeypatch, capsys
):
    """U7 (#1085): a restore brings back what the ledger says was erased."""
    customer = Customer(tenant_id=tenant_id, phone=PHONE, name="Müller")
    session.add(customer)
    session.flush()
    order = _order(session, tenant_id, customer=customer, item_note="ohne Zwiebeln")
    table = _reservation(session, tenant_id)
    session.commit()
    item = session.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
    ids = (table.id, item.id, order.id, order.call_id, customer.id)
    table_id, item_id, order_id, call_id, customer_id = ids
    day = "2026-11-02"
    ledger = [
        f"{day},reservations,{table_id},deleted",
        f"{day},order_items,{item_id},cleared",
        f"{day},orders,{order_id},cleared",
        f"{day},calls,{call_id},cleared",
        f"{day},customers,{customer_id},deleted",
    ]

    assert _replay(monkeypatch, *ledger) == 0
    session.expire_all()
    stripped = session.get(Order, order_id)
    assert (stripped.customer_name, stripped.phone, stripped.customer_id) == (
        "",
        "",
        None,
    )
    assert session.get(OrderItem, item_id).note is None
    assert session.get(Reservation, table_id) is None
    assert session.get(Call, call_id).caller_id is None
    assert session.get(Customer, customer_id) is None
    report = json.loads(capsys.readouterr().err)
    assert report["ledger_lines"] == 5
    replayed = session.scalar(
        select(AuditLog).where(AuditLog.action == "data_subject.ledger_replayed")
    )
    assert replayed.payload["since"] == "2026-11-01"
    assert "Müller" not in json.dumps(replayed.payload)

    # Idempotent: a second run over the same ledger changes nothing and succeeds.
    assert _replay(monkeypatch, *ledger) == 0
    session.expire_all()
    assert session.get(Order, order_id).customer_name == ""


def test_replay_records_an_objection_again_from_the_row_that_holds_the_number(
    session, tenant_id, monkeypatch
):
    order = _order(session, tenant_id)
    stripped = _order(session, tenant_id, phone="")
    session.commit()
    lines = [
        f"2026-11-02,orders,{order.id},objected",
        f"2026-11-02,orders,{stripped.id},objected",
        f"2026-11-02,calls,{order.call_id},cleared",
    ]
    assert _replay(monkeypatch, *lines) == 0
    assert _replay(monkeypatch, *lines) == 0
    session.expire_all()
    assert session.scalars(select(RecognitionObjection.phone)).all() == [PHONE]
    assert session.get(Call, order.call_id).caller_id is None


def test_replay_leaves_lines_older_than_the_dump_alone(session, tenant_id, monkeypatch):
    """Older lines are in the dump already; an objection withdrawn since must
    not come back."""
    order = _order(session, tenant_id)
    session.commit()
    assert _replay(monkeypatch, f"2026-10-31,orders,{order.id},objected") == 0
    assert session.scalars(select(RecognitionObjection.phone)).all() == []
    # The day of the dump itself is replayed: the ledger names days, not times.
    assert _replay(monkeypatch, f"2026-11-01,orders,{order.id},objected") == 0
    assert session.scalars(select(RecognitionObjection.phone)).all() == [PHONE]


@pytest.mark.parametrize(
    "bad",
    [
        "2026-11-02,orders,{id},forgotten",
        "2026-11-02,menu_items,{id},deleted",
        "2026-11-02,orders,not-an-id,cleared",
        "yesterday,orders,{id},cleared",
        "2026-11-02,orders,{id}",
    ],
)
def test_replay_refuses_a_ledger_it_cannot_read_and_changes_nothing(
    session, tenant_id, monkeypatch, capsys, bad
):
    order = _order(session, tenant_id)
    session.commit()
    good = f"2026-11-02,orders,{order.id},cleared"
    assert _replay(monkeypatch, good, "", bad.format(id=order.id)) == 1
    assert "line 3" in capsys.readouterr().err
    session.expire_all()
    assert session.get(Order, order.id).customer_name == "Müller"


def test_replay_wants_the_time_of_the_dump(session, tenant_id, monkeypatch):
    with pytest.raises(SystemExit):
        main(["replay", "--tenant-name", TENANT])
