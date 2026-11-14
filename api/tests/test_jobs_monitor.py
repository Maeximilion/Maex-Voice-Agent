"""api/jobs/monitor.py: alarms and the day report for the operator (T-8.3,
docs/02 §5, docs/13 §5). The push itself is a fake; what the push server
does with it is checked in test_push_service.py."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.domain.status import bridge
from api.events.outbox import MAX_ATTEMPTS, mark_attempt_failed
from api.jobs import monitor
from api.models import (
    AuditLog,
    Call,
    OpeningHours,
    OutboxEvent,
    Recording,
    Reservation,
)
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
# Thursday 29.10.2026, 14:00 in Berlin: the business day began at 05:00.
NOW = datetime(2026, 10, 29, 14, 0, tzinfo=BERLIN)


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


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "alarm_cost_per_day_cents", 300)
    monkeypatch.setattr(settings, "alarm_error_calls_per_hour", 3)
    monkeypatch.setattr(settings, "alarm_no_audio_calls", 3)
    # Off here: each group of tests below switches its own on.
    monkeypatch.setattr(settings, "voice_url", "")
    monkeypatch.setattr(settings, "alarm_backup_hours", 0)
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", "")
    monkeypatch.setattr(settings, "alarm_bridge_silent_minutes", 5)
    # Late, so the day report stays out of the alarm tests; its own tests set 8.
    monkeypatch.setattr(settings, "monitor_report_hour", 23)


class Pushes:
    """Stands in for the push server: records what would reach the operator."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, title: str, text: str, priority: str) -> None:
        if self.fail:
            raise httpx.ConnectError("push server down")
        self.sent.append((title, text, priority))

    def titles(self) -> list[str]:
        return [title for title, _, _ in self.sent]


def call(session, tenant_id, at, outcome="completed", cost=None, note=None) -> None:
    session.add(
        Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            started_at=at,
            ended_at=at + timedelta(minutes=2),
            outcome=outcome,
            cost_cents=cost,
            line_note=note,
            delete_after=date(2028, 10, 29),
        )
    )
    session.commit()


def run(session, now=NOW, pushes=None) -> Pushes:
    pushes = pushes or Pushes()
    monitor.check(session, now, send=pushes)
    return pushes


# --- Cost --------------------------------------------------------------------


def test_cost_above_the_daily_limit_alarms_once(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(hours=2), cost=200)
    call(session, tenant_id, NOW - timedelta(hours=1), cost=150)

    first = run(session)
    again = run(session)

    assert first.titles() == ["Cost above the daily limit"]
    assert "3.50 EUR" in first.sent[0][1]
    assert again.sent == []


def test_calls_without_a_cost_are_named_not_counted_as_zero(session, tenant_id):
    """No price in .env or an unmeasured transfer leaves cost_cents empty (Codex
    PR #435, P1): the push says how many, it never reads as a cost of zero."""
    call(session, tenant_id, NOW - timedelta(hours=2), cost=400)
    call(session, tenant_id, NOW - timedelta(hours=1), cost=None)
    call(session, tenant_id, NOW - timedelta(minutes=30), cost=None)

    [(_, text, _)] = run(session).sent

    assert text.startswith("Today 4.00 EUR plus 2 calls of unknown cost")


def test_cost_at_the_limit_stays_quiet(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(hours=1), cost=300)

    assert run(session).sent == []


def test_cost_of_the_day_before_does_not_count(session, tenant_id):
    """The business day began at 05:00; a call at 04:30 belongs to yesterday."""
    call(session, tenant_id, NOW.replace(hour=4, minute=30), cost=500)
    call(session, tenant_id, NOW - timedelta(hours=1), cost=100)

    assert "Cost above the daily limit" not in run(session).titles()


# --- Errors ------------------------------------------------------------------


def test_error_calls_in_the_last_hour_alarm(session, tenant_id):
    for minutes in (10, 20, 50):
        call(session, tenant_id, NOW - timedelta(minutes=minutes), outcome="error")

    pushes = run(session)

    assert pushes.titles() == ["Calls with errors"]
    assert pushes.sent[0][2] == "high"


def quiet_call(session, tenant_id, minutes: int) -> None:
    at = NOW - timedelta(minutes=minutes)
    call(session, tenant_id, at, outcome="abandoned", note="no_caller_audio")


def test_calls_in_a_row_without_caller_audio_alarm(session, tenant_id):
    """#651: each such call ends without the team; three in a row are no
    muted telephones any more, but one-way audio on our side."""
    call(session, tenant_id, NOW - timedelta(minutes=50))
    for minutes in (40, 30, 10):
        quiet_call(session, tenant_id, minutes)

    pushes = run(session)

    assert pushes.titles() == ["No audio from callers"]
    assert pushes.sent[0][2] == "high"
    # Once a clock hour, like the other alarms.
    assert run(session).sent == []


def test_a_call_with_audio_in_between_ends_the_row(session, tenant_id):
    quiet_call(session, tenant_id, 40)
    quiet_call(session, tenant_id, 30)
    call(session, tenant_id, NOW - timedelta(minutes=20))
    quiet_call(session, tenant_id, 10)

    assert run(session).sent == []


def test_fewer_calls_than_the_limit_or_an_old_row_stay_quiet(session, tenant_id):
    quiet_call(session, tenant_id, 30)
    quiet_call(session, tenant_id, 10)
    assert run(session).sent == []

    # A third one, and all of them hours ago: nobody has called since.
    quiet_call(session, tenant_id, 50)
    assert run(session, now=NOW + timedelta(hours=3)).sent == []


def test_the_alarm_for_calls_without_audio_can_be_switched_off(
    session, tenant_id, monkeypatch
):
    monkeypatch.setattr(settings, "alarm_no_audio_calls", 0)
    for minutes in (40, 30, 10):
        quiet_call(session, tenant_id, minutes)

    assert run(session).sent == []


def test_errors_below_the_limit_or_older_stay_quiet(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(minutes=10), outcome="error")
    call(session, tenant_id, NOW - timedelta(minutes=20), outcome="error")
    call(session, tenant_id, NOW - timedelta(minutes=90), outcome="error")
    call(session, tenant_id, NOW - timedelta(minutes=5), outcome="transferred")

    assert run(session).sent == []


def test_errors_alarm_again_in_the_next_hour(session, tenant_id):
    for minutes in (10, 20, 30):
        call(session, tenant_id, NOW - timedelta(minutes=minutes), outcome="error")
    run(session)
    later = NOW + timedelta(hours=1)
    for minutes in (5, 15, 25):
        call(session, tenant_id, later - timedelta(minutes=minutes), outcome="error")

    assert run(session, now=later).titles() == ["Calls with errors"]


# --- Outbox ------------------------------------------------------------------


def failed_event(session, tenant_id, last_attempt, created=None, touched=None):
    """A row the dispatcher gave up on: its last attempt ran at `next_attempt_at`.
    `touched` is a later write to the row, as by the deletion job."""
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            event_type="callback.created",
            payload={},
            status="failed",
            attempts=5,
            last_error="n8n down",
            next_attempt_at=last_attempt,
            created_at=created or last_attempt - timedelta(minutes=13),
            updated_at=touched or last_attempt,
        )
    )
    session.commit()


def test_a_failed_outbox_event_alarms(session, tenant_id):
    """n8n down: events pile up as failed, and this push does not go through n8n."""
    failed_event(session, tenant_id, NOW - timedelta(minutes=10))
    failed_event(session, tenant_id, NOW - timedelta(minutes=5))

    pushes = run(session)

    assert pushes.titles() == ["Events not delivered"]
    assert "2" in pushes.sent[0][1]
    assert run(session).sent == []


def test_a_row_the_dispatcher_gives_up_on_alarms(session, tenant_id):
    """Through events/outbox.py itself: the final failure stamps next_attempt_at,
    which is what the alarm reads."""
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type="callback.created",
        payload={},
        attempts=MAX_ATTEMPTS - 1,
        next_attempt_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(days=3),
    )
    session.add(event)
    session.commit()

    assert mark_attempt_failed(event, "ConnectError: n8n down", NOW)
    session.commit()

    assert run(session).titles() == ["Events not delivered"]


def test_an_overdue_last_attempt_that_fails_now_alarms(session, tenant_id):
    """The dispatcher was stopped after scheduling the last attempt and runs it
    two hours late: the failure is now, not at the scheduled time (Codex PR
    #435, P2). events/outbox.py stamps the moment of the final failure."""
    event = OutboxEvent(
        tenant_id=tenant_id,
        event_type="callback.created",
        payload={},
        attempts=MAX_ATTEMPTS - 1,
        next_attempt_at=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(hours=3),
    )
    session.add(event)
    session.commit()

    assert mark_attempt_failed(event, "ConnectError: n8n down", NOW)
    session.commit()

    assert event.next_attempt_at == NOW
    assert run(session).titles() == ["Events not delivered"]


def test_a_kitchen_slip_the_bridge_gives_up_on_is_no_n8n_alarm(session, tenant_id):
    """Kitchen slips go to the print bridge, never to n8n (events/types.py
    KITCHEN); the tablet shows a failed one red (Codex PR #435, P2)."""
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            event_type="order.confirmed",
            payload={},
            status="failed",
            next_attempt_at=NOW - timedelta(minutes=5),
        )
    )
    session.commit()

    assert run(session).sent == []


def test_an_old_failed_event_stays_quiet(session, tenant_id):
    failed_event(session, tenant_id, NOW - timedelta(hours=3))

    assert run(session).sent == []


def test_a_failed_row_the_deletion_job_rewrites_stays_quiet(session, tenant_id):
    """The deletion job rewrites the payload of a failed row (allergy notes,
    expiry), which moves updated_at but not the time of its last attempt
    (Codex PR #435, P2)."""
    failed_event(
        session,
        tenant_id,
        NOW - timedelta(days=1),
        touched=NOW - timedelta(minutes=5),
    )

    assert run(session).sent == []


def test_an_event_the_deletion_job_expires_stays_quiet(session, tenant_id):
    """api/jobs/retention.py marks an unsent row of 30 days `failed`; it was due
    when it was written and never tried, so it never failed in the last hour."""
    failed_event(
        session,
        tenant_id,
        NOW - timedelta(days=31),
        created=NOW - timedelta(days=31),
        touched=NOW - timedelta(minutes=5),
    )

    assert run(session).sent == []


def test_a_backlog_older_than_a_day_that_fails_now_alarms(session, tenant_id):
    """A dispatcher stopped for two days resumes its backlog; events that fail
    now alarm, whatever their age (Codex PR #435, P2)."""
    failed_event(
        session, tenant_id, NOW - timedelta(minutes=5), created=NOW - timedelta(days=2)
    )

    assert run(session).titles() == ["Events not delivered"]


# --- Day report --------------------------------------------------------------


def test_the_day_report_names_yesterday_once(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    yesterday = NOW - timedelta(days=1)
    call(session, tenant_id, yesterday, cost=40)
    call(session, tenant_id, yesterday, outcome="transferred", cost=10)
    call(session, tenant_id, yesterday, outcome="error")
    call(session, tenant_id, NOW - timedelta(hours=1), cost=99)

    first = run(session)
    again = run(session)

    [(title, text, priority)] = first.sent
    assert title == "Day report 2026-10-28"
    assert "3 calls" in text
    assert "1 error" in text
    assert "0.50 EUR plus 1 calls of unknown cost" in text
    assert priority == "normal"
    assert again.sent == []


def test_the_day_report_counts_the_agents_tables_only(session, tenant_id, monkeypatch):
    """A table the team entered by hand took no call (T-3.12)."""
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    yesterday = NOW - timedelta(days=1)
    agent_call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=yesterday,
        delete_after=yesterday.date(),
    )
    session.add(agent_call)
    session.flush()
    for call_id, source in ((agent_call.id, "agent"), (None, "tablet")):
        session.add(
            Reservation(
                tenant_id=tenant_id,
                call_id=call_id,
                source=source,
                status="confirmed",
                guest_name="Müller",
                phone="",
                party_size=2,
                reserved_for=NOW + timedelta(days=1),
                idempotency_key=uuid.uuid4().hex,
                created_at=yesterday,
            )
        )
    session.commit()

    [(_, text, _)] = run(session).sent

    assert "1 reservations" in text


def test_no_day_report_before_its_hour(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    call(session, tenant_id, NOW - timedelta(days=1), cost=40)

    assert run(session, now=NOW.replace(hour=7)).sent == []


def test_a_day_without_calls_still_reports(session, tenant_id, monkeypatch):
    """Silence is news too: no call at all can mean the line is down."""
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    assert run(session).titles() == ["Day report 2026-10-28"]


# --- Heartbeats -------------------------------------------------------------


def run_voice(session, seen, now=NOW) -> Pushes:
    """A pass on a stack with `voice`; `seen` is what it answers, or an error."""

    def probe() -> float | None:
        if isinstance(seen, Exception):
            raise seen
        return seen

    pushes = Pushes()
    monitor.check(session, now, send=pushes, probe=probe)
    return pushes


@pytest.fixture
def with_voice(monkeypatch):
    monkeypatch.setattr(settings, "voice_url", "http://voice:8001")


def test_a_voice_that_does_not_answer_alarms_once_an_hour(
    session, tenant_id, with_voice
):
    down = httpx.ConnectError("refused")

    first = run_voice(session, down)
    again = run_voice(session, down)
    later = run_voice(session, down, now=NOW + timedelta(hours=1))

    assert first.titles() == ["Voice layer not answering"]
    assert first.sent[0][2] == "high"
    assert again.sent == []
    assert later.titles() == ["Voice layer not answering"]


def test_asterisk_silent_for_too_long_alarms(session, tenant_id, with_voice):
    pushes = run_voice(session, monitor.ASTERISK_SILENT_SECONDS + 1)

    assert pushes.titles() == ["Asterisk not registered"]


def test_asterisk_never_seen_alarms(session, tenant_id, with_voice):
    assert run_voice(session, None).titles() == ["Asterisk not registered"]


def test_a_healthy_voice_layer_stays_quiet(session, tenant_id, with_voice):
    assert run_voice(session, 15.0).sent == []


def test_a_stack_without_voice_is_not_probed(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "voice_url", "")

    def probe() -> float | None:
        raise AssertionError("no voice to ask")

    pushes = Pushes()
    monitor.check(session, NOW, send=pushes, probe=probe)

    assert pushes.sent == []


def test_the_probe_reads_what_voice_answers(monkeypatch):
    monkeypatch.setattr(settings, "voice_url", "http://voice:8001")
    asked = []

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"seconds": 12.5}

    def fake_get(url, **kwargs):
        asked.append(url)
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    assert monitor.asterisk_seen() == 12.5
    assert asked == ["http://voice:8001/asterisk/seen"]


# --- Backup ------------------------------------------------------------------


def backup_done(session, tenant_id, at) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            at=at,
            actor="backup",
            action=monitor.BACKUP_DONE,
            entity="database",
        )
    )
    session.commit()


@pytest.fixture
def backup_limit(monkeypatch):
    monkeypatch.setattr(settings, "alarm_backup_hours", 26)


def test_a_stale_backup_alarms_once_a_day(session, tenant_id, backup_limit):
    backup_done(session, tenant_id, NOW - timedelta(hours=27))

    first = run(session)
    again = run(session)

    assert first.titles() == ["Backup overdue"]
    assert first.sent[0][2] == "high"
    assert again.sent == []
    assert run(session, now=NOW + timedelta(days=1)).titles() == ["Backup overdue"]


def test_no_backup_at_all_alarms(session, tenant_id, backup_limit):
    """A cron line never installed reports nothing by itself."""
    assert run(session).titles() == ["Backup overdue"]


def test_a_recent_backup_stays_quiet(session, tenant_id, backup_limit):
    backup_done(session, tenant_id, NOW - timedelta(hours=25))

    assert run(session).sent == []


def test_a_limit_of_zero_switches_the_backup_alarm_off(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "alarm_backup_hours", 0)

    assert run(session).sent == []


# --- Recordings the deletion job could not take (#252, audit of 02.11.2026) --


@pytest.fixture
def periods(monkeypatch):
    monkeypatch.setattr(settings, "retention_recording_days", 30)
    monkeypatch.setattr(settings, "retention_interval_seconds", 3600.0)


def recording(session, tenant_id, started, withdrawn=None) -> None:
    sid = uuid.uuid4().hex
    row = Call(
        tenant_id=tenant_id,
        external_session_id=sid,
        started_at=started,
        delete_after=date(2028, 10, 29),
    )
    session.add(row)
    session.flush()
    session.add(
        Recording(
            tenant_id=tenant_id,
            call_id=row.id,
            file_name=f"{sid}.wav",
            started_at=started,
            withdrawn_at=withdrawn,
        )
    )
    session.commit()


def test_a_recording_past_its_period_and_a_day_alarms_once_a_day(
    session, tenant_id, periods
):
    recording(session, tenant_id, NOW - timedelta(days=31, minutes=1))

    first = run(session)
    again = run(session)

    assert first.titles() == ["Recordings not deleted"]
    assert "withdrawal: 1." in first.sent[0][1]
    assert first.sent[0][2] == "high"
    assert again.sent == []
    assert run(session, now=NOW + timedelta(days=1)).titles() == [
        "Recordings not deleted"
    ]


def test_a_recording_withdrawn_over_two_hours_ago_alarms(session, tenant_id, periods):
    recording(
        session,
        tenant_id,
        NOW - timedelta(hours=5),
        NOW - timedelta(hours=2, seconds=1),
    )
    recording(session, tenant_id, NOW - timedelta(days=40))

    pushes = run(session)

    assert pushes.titles() == ["Recordings not deleted"]
    assert "withdrawal: 2." in pushes.sent[0][1]


def test_recordings_the_job_still_has_time_for_stay_quiet(session, tenant_id, periods):
    """The job deletes after 30 days and on its next pass after a withdrawal:
    a day of grace, and two of its passes."""
    recording(session, tenant_id, NOW - timedelta(days=31))
    recording(session, tenant_id, NOW - timedelta(hours=3), NOW - timedelta(hours=2))

    assert run(session).sent == []


def test_a_slow_deletion_job_gets_two_of_its_passes(
    session, tenant_id, periods, monkeypatch
):
    monkeypatch.setattr(settings, "retention_interval_seconds", 6 * 3600.0)
    recording(session, tenant_id, NOW - timedelta(days=1), NOW - timedelta(hours=11))

    assert run(session).sent == []
    assert run(session, now=NOW + timedelta(hours=1, seconds=1)).titles() == [
        "Recordings not deleted"
    ]


def test_a_recording_of_another_tenant_is_not_counted(session, tenant_id, periods):
    other = uuid.UUID(
        seed(session, tenant_name="Zweitbetrieb", timezone="Europe/Berlin").tenant_id
    )
    recording(session, other, NOW - timedelta(days=40))

    pushes = run(session)

    # One push for the other tenant, and its count is its own.
    assert pushes.titles() == ["Recordings not deleted"]
    assert session.scalars(
        select(AuditLog.tenant_id).where(AuditLog.action == monitor.ACTION)
    ).all() == [other]


# --- Print bridge ------------------------------------------------------------


@pytest.fixture
def bridge_silent(tenant_id, session, monkeypatch):
    """A bridge set up for this tenant that never polled, on a day open now."""
    monkeypatch.setattr(settings, "kitchen_bridge_tenant_id", str(tenant_id))
    session.add(
        OpeningHours(
            tenant_id=tenant_id,
            weekday=NOW.weekday(),
            opens_at=time(0),
            closes_at=time(23, 59),
            service="pickup",
        )
    )
    session.commit()


def test_a_silent_bridge_alarms_once_an_hour(session, tenant_id, bridge_silent):
    first = run(session)
    again = run(session)
    later = run(session, now=NOW + timedelta(hours=1))

    assert first.titles() == ["Print bridge silent"]
    assert first.sent[0][2] == "high"
    assert again.sent == []
    assert later.titles() == ["Print bridge silent"]


def test_a_bridge_that_polled_stays_quiet(session, tenant_id, bridge_silent):
    bridge.note_poll(session, tenant_id, NOW - timedelta(minutes=1))
    session.commit()

    assert run(session).sent == []


# --- Delivery ----------------------------------------------------------------


def test_a_push_that_fails_is_sent_on_the_next_pass(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(hours=1), cost=400)

    run(session, pushes=Pushes(fail=True))

    assert (
        session.scalars(select(AuditLog).where(AuditLog.actor == "monitor")).all() == []
    )
    assert "Cost above the daily limit" in run(session).titles()


def test_without_a_token_nothing_is_posted(monkeypatch):
    monkeypatch.setattr(settings, "monitor_push_token", "")
    posted = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a))

    monitor.push("Titel", "Text", "normal")

    assert posted == []


def test_the_push_carries_the_token_and_priority(monkeypatch):
    monkeypatch.setattr(settings, "monitor_push_token", "tk_test")
    monkeypatch.setattr(settings, "monitor_push_url", "http://push/")
    posted = []

    class Response:
        def raise_for_status(self) -> None:
            pass

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)

    monitor.push("Titel", "Text", "high")

    [(url, kwargs)] = posted
    assert url == "http://push/"
    assert kwargs["headers"]["Authorization"] == "Bearer tk_test"
    assert kwargs["json"] == {
        "topic": "ops",
        "title": "Titel",
        "message": "Text",
        "priority": 4,
    }
