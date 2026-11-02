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
    Call,
    Customer,
    DataSubjectRequest,
    Order,
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
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert [(row[1], row[2], row[3]) for row in rows] == [
        ("calls", str(order.call_id), "cleared")
    ]
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
