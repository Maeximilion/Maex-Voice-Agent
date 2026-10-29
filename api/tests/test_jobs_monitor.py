"""api/jobs/monitor.py: alarms and the day report for the operator (T-8.3,
docs/02 §5, docs/13 §5). The push itself is a fake; what the push server
does with it is checked in test_push_service.py."""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.config import settings
from api.jobs import monitor
from api.models import AuditLog, Call, OutboxEvent
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


def call(session, tenant_id, at, outcome="completed", cost=None) -> None:
    session.add(
        Call(
            tenant_id=tenant_id,
            external_session_id=uuid.uuid4().hex,
            started_at=at,
            ended_at=at + timedelta(minutes=2),
            outcome=outcome,
            cost_cents=cost,
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

    assert first.titles() == ["Kosten über der Tagesgrenze"]
    assert "3,50" in first.sent[0][1]
    assert again.sent == []


def test_cost_at_the_limit_stays_quiet(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(hours=1), cost=300)

    assert run(session).sent == []


def test_cost_of_the_day_before_does_not_count(session, tenant_id):
    """The business day began at 05:00; a call at 04:30 belongs to yesterday."""
    call(session, tenant_id, NOW.replace(hour=4, minute=30), cost=500)
    call(session, tenant_id, NOW - timedelta(hours=1), cost=100)

    assert "Kosten über der Tagesgrenze" not in run(session).titles()


# --- Errors ------------------------------------------------------------------


def test_error_calls_in_the_last_hour_alarm(session, tenant_id):
    for minutes in (10, 20, 50):
        call(session, tenant_id, NOW - timedelta(minutes=minutes), outcome="error")

    pushes = run(session)

    assert pushes.titles() == ["Anrufe mit Fehler"]
    assert pushes.sent[0][2] == "high"


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

    assert run(session, now=later).titles() == ["Anrufe mit Fehler"]


# --- Outbox ------------------------------------------------------------------


def failed_event(session, tenant_id, at) -> None:
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            event_type="callback.created",
            payload={},
            status="failed",
            attempts=20,
            last_error="n8n down",
            created_at=at,
            updated_at=at,
        )
    )
    session.commit()


def test_a_failed_outbox_event_alarms(session, tenant_id):
    """n8n down: events pile up as failed, and this push does not go through n8n."""
    failed_event(session, tenant_id, NOW - timedelta(minutes=10))
    failed_event(session, tenant_id, NOW - timedelta(minutes=5))

    pushes = run(session)

    assert pushes.titles() == ["Ereignisse nicht zugestellt"]
    assert "2" in pushes.sent[0][1]
    assert run(session).sent == []


def test_an_event_the_deletion_job_expires_stays_quiet(session, tenant_id):
    """api/jobs/retention.py marks an unsent row of 30 days `failed` and so
    touches its updated_at: that is no n8n outage."""
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            event_type="order.confirmed",
            payload={},
            status="failed",
            created_at=NOW - timedelta(days=31),
            updated_at=NOW - timedelta(minutes=5),
        )
    )
    session.commit()

    assert run(session).sent == []


def test_an_old_failed_event_stays_quiet(session, tenant_id):
    failed_event(session, tenant_id, NOW - timedelta(hours=3))

    assert run(session).sent == []


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
    assert title == "Tagesbericht 28.10.2026"
    assert "3 Anrufe" in text
    assert "1 Fehler" in text
    assert "0,50" in text
    assert priority == "normal"
    assert again.sent == []


def test_no_day_report_before_its_hour(session, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    call(session, tenant_id, NOW - timedelta(days=1), cost=40)

    assert run(session, now=NOW.replace(hour=7)).sent == []


def test_a_day_without_calls_still_reports(session, tenant_id, monkeypatch):
    """Silence is news too: no call at all can mean the line is down."""
    monkeypatch.setattr(settings, "monitor_report_hour", 8)
    assert run(session).titles() == ["Tagesbericht 28.10.2026"]


# --- Delivery ----------------------------------------------------------------


def test_a_push_that_fails_is_sent_on_the_next_pass(session, tenant_id):
    call(session, tenant_id, NOW - timedelta(hours=1), cost=400)

    run(session, pushes=Pushes(fail=True))

    assert (
        session.scalars(select(AuditLog).where(AuditLog.actor == "monitor")).all() == []
    )
    assert "Kosten über der Tagesgrenze" in run(session).titles()


def test_without_a_token_nothing_is_posted(monkeypatch):
    monkeypatch.setattr(settings, "monitor_push_token", "")
    posted = []
    monkeypatch.setattr(monitor.httpx, "post", lambda *a, **k: posted.append(a))

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

    monkeypatch.setattr(monitor.httpx, "post", fake_post)

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
