"""domain/calls: Anruf-Log start/end, Zustands-Idempotenz, Löschfrist (T-1.9)."""

import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.calls import end_call, start_call
from api.domain.calls.start import _delete_after
from api.models import Call
from api.schemas.calls import EndCallRequest, StartCallRequest
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
    assert call.delete_after == date(2028, 9, 15)


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
    results: list[object] = []
    lock = threading.Lock()

    def run() -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                outcome: object = start_call(
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
    ids = {r.call_id for r in results}
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
        (date(2026, 9, 15), date(2028, 9, 15)),
        (date(2026, 1, 31), date(2028, 1, 31)),
        (date(2026, 12, 31), date(2028, 12, 31)),
        # Schaltjahr-Rand: 24 Monate nach dem 29. Februar 2028 gibt es keinen 29.
        # Februar 2030, der Tag wird auf den letzten Tag des Zielmonats begrenzt.
        (date(2028, 2, 29), date(2030, 2, 28)),
    ],
)
def test_loeschfrist_24_monate_mit_tagesbegrenzung(start_date, expected):
    result = _delete_after(
        datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
    )

    assert result == expected
