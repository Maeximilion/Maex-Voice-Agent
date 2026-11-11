"""domain/calls: Anruf-Log start/end, Zustands-Idempotenz, Löschfrist (T-1.9)."""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import InvalidInput, NotFound
from api.domain.calls import close_left_open_calls, end_call, start_call
from api.domain.calls.end import LEFT_OPEN_GRACE
from api.domain.calls.start import _delete_after
from api.models import Call, ServiceConfig
from api.schemas.calls import CallStarted, EndCallRequest, StartCallRequest
from scripts.seed import seed

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)


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


def start_request(tenant_id, **overrides) -> StartCallRequest:
    return StartCallRequest(
        **{
            "tenant_id": tenant_id,
            "external_session_id": "ext-1",
            "caller_id": "0721 5551234",
            **overrides,
        }
    )


def test_anruf_beginnt_mit_normalisierter_nummer_und_loeschfrist(session, tenant_id):
    result = start_call(session, start_request(tenant_id), now=NOW)

    call = session.get(Call, result.call_id)
    assert call.external_session_id == "ext-1"
    assert call.caller_id == "+497215551234"
    assert call.started_at == NOW
    assert call.ended_at is None
    assert call.delete_after == date(2026, 12, 14)


def test_unterdrueckte_nummer_bleibt_null(session, tenant_id):
    result = start_call(session, start_request(tenant_id, caller_id=None), now=NOW)

    call = session.get(Call, result.call_id)
    assert call.caller_id is None


def test_unbrauchbare_anrufer_kennung_wird_unveraendert_uebernommen(session, tenant_id):
    """Der Anruf darf nie verlorengehen, nur weil die Plattform-Kennung keine
    gueltige Rufnummer ist (CLAUDE.md §2 Regel 5)."""
    result = start_call(
        session, start_request(tenant_id, caller_id="anonymous"), now=NOW
    )

    call = session.get(Call, result.call_id)
    assert call.caller_id == "anonymous"


def test_zweiter_start_derselben_session_liefert_denselben_anruf(session, tenant_id):
    erster = start_call(session, start_request(tenant_id), now=NOW)

    zweiter = start_call(session, start_request(tenant_id), now=NOW)

    assert zweiter.call_id == erster.call_id
    assert session.scalar(select(func.count()).select_from(Call)) == 1


def test_neue_session_nach_ende_bekommt_einen_neuen_anruf(session, tenant_id):
    erster = start_call(session, start_request(tenant_id), now=NOW)
    call = session.get(Call, erster.call_id)
    call.ended_at = NOW
    call.duration_seconds = 60
    call.outcome = "completed"
    session.commit()

    zweiter = start_call(session, start_request(tenant_id), now=NOW)

    assert zweiter.call_id != erster.call_id
    assert session.scalar(select(func.count()).select_from(Call)) == 2


def test_unbekannter_mandant_ist_not_found(session):
    with pytest.raises(NotFound):
        start_call(session, start_request(uuid.uuid4()), now=NOW)


def test_gleichzeitige_starts_derselben_session_erzeugen_nur_einen_anruf(
    migrated_db_url,
):
    """Zwei Plattform-Retries fuer dieselbe Session gleichzeitig: ohne Sperre
    findet keiner einen offenen Anruf, beide legen einen an (Codex-Review PR #99,
    P1), und die Folge-Tool-Aufrufe landen auf zwei verschiedenen call_id."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )

    start = threading.Barrier(6)
    results: list[CallStarted | Exception] = []
    lock = threading.Lock()

    def run() -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                outcome: CallStarted | Exception = start_call(
                    own, start_request(tenant, external_session_id="ext-race"), now=NOW
                )
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    haengende = [t for t in threads if t.is_alive()]
    assert not haengende, f"{len(haengende)} Aufrufe haengen nach 30 s"
    assert len(results) == 6

    fehler = [r for r in results if isinstance(r, Exception)]
    assert not fehler, f"kein Aufruf darf scheitern, bekam {fehler}"

    with Session(engine) as s:
        anzahl = s.scalar(select(func.count()).select_from(Call))
    engine.dispose()

    assert anzahl == 1, f"erwartet ein Anruf, bekam {anzahl}"
    ids = {r.call_id for r in results if isinstance(r, CallStarted)}
    assert len(ids) == 1, f"alle Aufrufe sollen dieselbe call_id liefern, bekam {ids}"


def test_leere_session_id_ist_invalid_input(session, tenant_id):
    with pytest.raises(InvalidInput):
        start_call(
            session, start_request(tenant_id, external_session_id="   "), now=NOW
        )


def end_request(call_id, tenant_id, **overrides) -> EndCallRequest:
    return EndCallRequest(
        **{
            "call_id": call_id,
            "tenant_id": tenant_id,
            "outcome": "completed",
            **overrides,
        }
    )


def test_anruf_endet_mit_dauer_und_ergebnis(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)

    ended = end_call(
        session,
        end_request(
            started.call_id, tenant_id, outcome="transferred", intent="complaint"
        ),
        now=datetime(2026, 9, 15, 18, 3, 20, tzinfo=UTC),
    )

    assert ended.duration_seconds == 200
    assert ended.outcome == "transferred"
    call = session.get(Call, started.call_id)
    assert call.ended_at is not None
    assert call.intent == "complaint"


def test_zweiter_end_aufruf_liest_das_erste_ergebnis(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)
    erster = end_call(
        session,
        end_request(started.call_id, tenant_id, outcome="completed"),
        now=datetime(2026, 9, 15, 18, 1, 0, tzinfo=UTC),
    )

    zweiter = end_call(
        session,
        end_request(started.call_id, tenant_id, outcome="error"),
        now=datetime(2026, 9, 15, 18, 5, 0, tzinfo=UTC),
    )

    assert zweiter.outcome == erster.outcome == "completed"
    assert zweiter.duration_seconds == erster.duration_seconds


def test_unbekannter_anruf_ist_not_found(session, tenant_id):
    with pytest.raises(NotFound):
        end_call(session, end_request(uuid.uuid4(), tenant_id), now=NOW)


def test_anruf_eines_anderen_mandanten_ist_not_found(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)
    fremder = uuid.UUID(
        seed(session, tenant_name="Fremdbetrieb", timezone="Europe/Berlin").tenant_id
    )

    with pytest.raises(NotFound):
        end_call(session, end_request(started.call_id, fremder), now=NOW)


@pytest.mark.parametrize(
    ("start_date", "expected"),
    [
        (date(2026, 9, 15), date(2026, 12, 14)),
        # Across the turn of the year and a leap day: plain days, no month rule.
        (date(2026, 12, 31), date(2027, 3, 31)),
        (date(2028, 2, 29), date(2028, 5, 29)),
    ],
)
def test_caller_number_period_is_90_days_from_the_setting(start_date, expected):
    """docs/03 Loeschkonzept, legal check L8: the caller number goes 90 days
    after the call (was 24 months); the period is RETENTION_CALLER_ID_DAYS."""
    started = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
    assert _delete_after(started) == expected


def test_caller_number_period_follows_a_changed_setting(monkeypatch):
    monkeypatch.setattr(settings, "retention_caller_id_days", 30)
    assert _delete_after(NOW) == date(2026, 10, 15)


# -- calls a process left open (#545) -------------------------------------------


def _limit(session, tenant_id) -> timedelta:
    """How long a call of this tenant may stay open before the job closes it."""
    seconds = session.scalar(
        select(ServiceConfig.max_call_seconds).where(
            ServiceConfig.tenant_id == tenant_id
        )
    )
    return timedelta(seconds=seconds) + LEFT_OPEN_GRACE


def test_a_call_left_open_past_its_limit_is_closed_as_error(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)
    later = NOW + _limit(session, tenant_id) + timedelta(seconds=1)

    assert close_left_open_calls(session, later) == 1

    call = session.get(Call, started.call_id)
    assert call.ended_at == later
    assert call.outcome == "error"
    # Nobody knows when the leg really ended or what it cost.
    assert call.duration_seconds is None
    assert call.cost_cents is None


def test_a_call_within_its_limit_stays_open(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)

    assert close_left_open_calls(session, NOW + _limit(session, tenant_id)) == 0

    assert session.get(Call, started.call_id).ended_at is None


def test_an_ended_call_keeps_its_log(session, tenant_id):
    started = start_call(session, start_request(tenant_id), now=NOW)
    ended_at = NOW + timedelta(minutes=2)
    end_call(session, end_request(started.call_id, tenant_id), now=ended_at)

    assert close_left_open_calls(session, NOW + timedelta(days=1)) == 0

    call = session.get(Call, started.call_id)
    assert (call.ended_at, call.outcome) == (ended_at, "completed")


def test_each_tenant_is_measured_by_its_own_limit(session, tenant_id):
    other = uuid.UUID(
        seed(session, tenant_name="Langbetrieb", timezone="Europe/Berlin").tenant_id
    )
    session.execute(
        update(ServiceConfig)
        .where(ServiceConfig.tenant_id == other)
        .values(max_call_seconds=3600)
    )
    session.commit()
    short = start_call(session, start_request(tenant_id), now=NOW)
    long = start_call(
        session, start_request(other, external_session_id="ext-2"), now=NOW
    )

    later = NOW + _limit(session, tenant_id) + timedelta(seconds=1)
    assert close_left_open_calls(session, later) == 1

    assert session.get(Call, short.call_id).outcome == "error"
    assert session.get(Call, long.call_id).ended_at is None


def test_a_late_end_of_the_leg_keeps_the_error(session, tenant_id):
    """A process that was only slow, not dead, reports the end after the job:
    the log stays as the job wrote it (end_call's state idempotency)."""
    started = start_call(session, start_request(tenant_id), now=NOW)
    later = NOW + _limit(session, tenant_id) + timedelta(seconds=1)
    close_left_open_calls(session, later)

    ended = end_call(
        session,
        end_request(started.call_id, tenant_id),
        now=later + timedelta(minutes=1),
    )

    assert ended.outcome == "error"


def test_the_close_never_overwrites_an_end_that_commits_first(migrated_db_url):
    """end_call holds the row lock while the job runs: the job waits for it and
    then finds the call ended, instead of writing over a correct outcome."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        call_id = start_call(s, start_request(tenant), now=NOW).call_id
        later = NOW + _limit(s, tenant) + timedelta(seconds=1)

    closed: list[object] = []

    def sweep() -> None:
        with Session(engine) as own:
            try:
                closed.append(close_left_open_calls(own, later))
            except Exception as exc:  # noqa: BLE001 - every error must show
                closed.append(exc)

    with Session(engine) as ending:
        call = ending.execute(
            select(Call).where(Call.id == call_id).with_for_update()
        ).scalar_one()
        call.ended_at = later
        call.outcome = "completed"
        ending.flush()
        worker = threading.Thread(target=sweep)
        worker.start()
        worker.join(timeout=1)
        assert worker.is_alive(), "the job must wait for the row lock"
        ending.commit()
    worker.join(timeout=10)

    assert closed == [0]
    with Session(engine) as s:
        closed_call = s.get(Call, call_id)
        assert closed_call is not None
        assert closed_call.outcome == "completed"
    engine.dispose()
