"""domain/callbacks/transfer.team_missed: nobody answered the team leg (T-10.8).

The dialplan reports a call whose leg at TEAM_PHONE nobody took; the card is
the team's only notice of it then (docs/20 §5). It is found by the id the line
gave the call, after our leg and its log are closed.
"""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from api.domain.callbacks import create_callback, team_missed
from api.domain.calls import end_call
from api.models import AuditLog, Call, Callback, OutboxEvent
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.calls import EndCallRequest
from scripts.seed import seed

STARTED = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
REPORTED = STARTED + timedelta(minutes=2)
CALLER = "+4972215550111"


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def make_call(
    session,
    tenant_id,
    *,
    sid: str = "1760000000.1",
    caller_id: str | None = CALLER,
    transfer_reason: str | None = "complaint",
    outcome: str | None = "transferred",
    started_at: datetime = STARTED,
) -> Call:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=sid,
        caller_id=caller_id,
        transfer_reason=transfer_reason,
        outcome=outcome,
        started_at=started_at,
        ended_at=None if outcome is None else started_at + timedelta(seconds=40),
        duration_seconds=None if outcome is None else 40,
        delete_after=date(2026, 12, 15),
    )
    session.add(call)
    session.commit()
    return call


def cards(session, call_id) -> list[Callback]:
    return list(
        session.scalars(select(Callback).where(Callback.call_id == call_id)).all()
    )


def test_an_unanswered_team_leg_becomes_a_card_under_the_callers_number(
    session, tenant_id
):
    call = make_call(session, tenant_id)

    task = team_missed(session, tenant_id, "1760000000.1", now=REPORTED)

    assert task is not None
    assert task.phone == CALLER
    assert task.reason == "complaint"
    assert "niemand abgenommen" in task.summary
    [card] = cards(session, call.id)
    assert card.status == "open"
    session.refresh(call)
    assert call.outcome == "callback"
    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "callback.created")
    ).all()
    assert [a.actor for a in audit] == ["telephony"]
    assert audit[0].payload["call_id"] == str(call.id)
    events = session.scalars(select(OutboxEvent)).all()
    assert [e.event_type for e in events] == ["callback.created"]


def test_a_number_the_guest_named_wins_over_the_caller_id(session, tenant_id):
    call = make_call(session, tenant_id)

    task = team_missed(
        session, tenant_id, "1760000000.1", phone="0721 5550222", now=REPORTED
    )

    assert task is not None and task.phone == "+497215550222"
    assert [c.phone for c in cards(session, call.id)] == ["+497215550222"]


def test_a_named_number_that_is_no_number_falls_back_to_the_caller_id(
    session, tenant_id
):
    make_call(session, tenant_id)

    task = team_missed(session, tenant_id, "1760000000.1", phone="abc", now=REPORTED)

    assert task is not None and task.phone == CALLER


@pytest.mark.parametrize("caller_id", [None, "anonymous"])
def test_without_a_usable_number_there_is_no_card(session, tenant_id, caller_id):
    """callbacks.phone is required, and nobody could call back anyway; the
    caller is told to call again instead of hearing a promise."""
    call = make_call(session, tenant_id, caller_id=caller_id)

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is None

    assert cards(session, call.id) == []
    session.refresh(call)
    assert call.outcome == "transferred"


@pytest.mark.parametrize(
    ("transfer_reason", "reason", "said"),
    [
        ("human_requested", "human_requested", "niemand abgenommen"),
        ("cancellation", "human_requested", "stornieren"),
        # Paused mode, shadow mode, the outage path: the core gave no reason.
        (None, "out_of_scope", "nicht bekannt"),
    ],
)
def test_the_reason_of_the_transfer_names_the_card(
    session, tenant_id, transfer_reason, reason, said
):
    make_call(session, tenant_id, transfer_reason=transfer_reason)

    task = team_missed(session, tenant_id, "1760000000.1", now=REPORTED)

    assert task is not None
    assert task.reason == reason
    assert said in task.summary


def test_an_unknown_call_has_no_card(session, tenant_id):
    make_call(session, tenant_id)

    assert team_missed(session, tenant_id, "1760000000.9", now=REPORTED) is None
    assert session.scalar(select(func.count()).select_from(Callback)) == 0


def test_a_call_of_another_tenant_is_not_found(session, tenant_id):
    other = uuid.UUID(
        seed(session, tenant_name="Anderer Betrieb", timezone="Europe/Berlin").tenant_id
    )
    make_call(session, other)

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is None


def test_the_newest_call_of_that_id_counts_and_an_old_one_never(session, tenant_id):
    """The line's ids are not unique for ever: an id seen again days later is
    another call."""
    old = make_call(session, tenant_id, started_at=STARTED - timedelta(days=3))

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is None
    assert cards(session, old.id) == []

    new = make_call(session, tenant_id)
    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is not None
    assert len(cards(session, new.id)) == 1


def test_a_second_report_returns_the_same_card(session, tenant_id):
    call = make_call(session, tenant_id)

    first = team_missed(session, tenant_id, "1760000000.1", now=REPORTED)
    again = team_missed(session, tenant_id, "1760000000.1", now=REPORTED)

    assert first is not None and again is not None
    assert first.callback_id == again.callback_id
    assert len(cards(session, call.id)) == 1
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_a_card_the_core_wrote_in_the_call_stays_the_only_one(session, tenant_id):
    call = make_call(session, tenant_id)
    earlier = create_callback(
        session,
        CreateCallbackRequest(
            call_id=call.id,
            tenant_id=tenant_id,
            phone=CALLER,
            reason="not_understood",
            summary="Anliegen konnte automatisch nicht geklärt werden.",
        ),
        now=STARTED,
    )

    task = team_missed(session, tenant_id, "1760000000.1", now=REPORTED)

    assert task is not None and task.callback_id == earlier.callback_id
    assert len(cards(session, call.id)) == 1


def test_an_error_outcome_stays(session, tenant_id):
    """The outage path logged the call as an error; the card does not hide it."""
    call = make_call(session, tenant_id, outcome="error")

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is not None

    session.refresh(call)
    assert call.outcome == "error"


def test_a_report_before_the_call_log_keeps_its_outcome(session, tenant_id):
    """A busy team telephone answers within a fraction of a second, while the
    call log of our leg may still be written: the log keeps 'callback'."""
    call = make_call(session, tenant_id, outcome=None)

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is not None
    ended = end_call(
        session,
        EndCallRequest(call_id=call.id, tenant_id=tenant_id, outcome="transferred"),
        now=REPORTED,
    )

    assert ended.outcome == "callback"
    session.refresh(call)
    assert call.ended_at is not None and call.outcome == "callback"


def test_an_outage_logged_after_the_report_stays_an_error(session, tenant_id):
    """The outage path hands the call on, the busy team answers before its log:
    the card stays, and so does the error the outage is counted by (Codex,
    own review of T-10.8)."""
    call = make_call(session, tenant_id, outcome=None)

    assert team_missed(session, tenant_id, "1760000000.1", now=REPORTED) is not None
    ended = end_call(
        session,
        EndCallRequest(call_id=call.id, tenant_id=tenant_id, outcome="error"),
        now=REPORTED,
    )

    assert ended.outcome == "error"
    assert len(cards(session, call.id)) == 1


@pytest.mark.parametrize(
    ("logged", "after"), [("transferred", "callback"), ("error", "error")]
)
def test_a_report_waits_for_the_call_log_and_reads_what_it_wrote(
    migrated_db_url, logged, after
):
    """Both take the call's row: the report that comes while end_call holds it
    waits, and reads the logged outcome after it. Without the lock it would
    turn an 'error' it never saw into 'callback'."""
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as setup:
            tenant_id = uuid.UUID(
                seed(
                    setup, tenant_name="Testbetrieb", timezone="Europe/Berlin"
                ).tenant_id
            )
            call_id = make_call(setup, tenant_id, outcome=None).id

        holder = Session(engine)
        holder.execute(
            text("select 1 from calls where id = :id for update"), {"id": call_id}
        )
        results: list[object] = []

        def report() -> None:
            with Session(engine) as s:
                results.append(team_missed(s, tenant_id, "1760000000.1", now=REPORTED))

        worker = threading.Thread(target=report)
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive()  # waits on the row
        end_call(
            holder,
            EndCallRequest(call_id=call_id, tenant_id=tenant_id, outcome=logged),
            now=REPORTED,
        )
        holder.close()
        worker.join(timeout=10)

        assert results and results[0] is not None
        with Session(engine) as check:
            assert check.get(Call, call_id).outcome == after
    finally:
        engine.dispose()
