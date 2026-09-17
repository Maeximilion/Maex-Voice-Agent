"""domain/callbacks: Rückruf anlegen, Zustands-Idempotenz, Audit, Outbox, Grenzfälle (T-1.7)."""

import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.callbacks import create_callback
from api.domain.callbacks.create import _lock_call
from api.models import AuditLog, Call, Callback, OutboxEvent
from api.schemas.callbacks import CreateCallbackRequest
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


def make_call(session, tenant_id) -> uuid.UUID:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=NOW,
        delete_after=date(2026, 9, 15),
    )
    session.add(call)
    session.commit()
    return call.id


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return make_call(session, tenant_id)


def request(tenant_id, call_id, **overrides) -> CreateCallbackRequest:
    return CreateCallbackRequest(
        **{
            "call_id": call_id,
            "tenant_id": tenant_id,
            "phone": "0721 555-1234",
            "reason": "not_understood",
            "summary": "Möchte eine große Bestellung für Samstag, Leitung war schlecht.",
            **overrides,
        }
    )


def test_rueckruf_wird_angelegt_mit_audit_und_outbox(session, tenant_id, call_id):
    task = create_callback(session, request(tenant_id, call_id), now=NOW)

    callback = session.get(Callback, task.callback_id)
    assert callback.status == "open"
    assert callback.phone == "+497215551234"
    assert callback.reason == "not_understood"
    assert callback.done_by is None and callback.done_at is None
    # Der Agent liest den Satz vor, er gehoert nicht in die Daten.
    assert task.say is not None
    assert "callback_id" in task.model_dump()
    assert "say" not in task.model_dump()

    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "callback.created")
    ).all()
    assert len(audit) == 1
    assert audit[0].entity == "callback"
    assert audit[0].payload["reason"] == "not_understood"

    events = session.scalars(select(OutboxEvent)).all()
    assert len(events) == 1
    assert events[0].event_type == "callback.created"
    assert events[0].status == "pending"
    assert events[0].payload["phone"] == "+497215551234"
    assert events[0].payload["callback_id"] == str(task.callback_id)


def test_telefonnummer_wird_normalisiert(session, tenant_id, call_id):
    # Visitenkarten-Schreibweise mit (0): die Ziffer entfaellt hinter der Landesvorwahl,
    # sonst waehlt das Team eine Nummer, die es nicht gibt.
    task = create_callback(
        session, request(tenant_id, call_id, phone="+49 (0)721 5551234"), now=NOW
    )

    assert task.phone == "+497215551234"


def test_zweiter_aufruf_im_selben_anruf_liefert_denselben_rueckruf(
    session, tenant_id, call_id
):
    erster = create_callback(session, request(tenant_id, call_id), now=NOW)

    zweiter = create_callback(
        session, request(tenant_id, call_id, summary="Anderer Text"), now=NOW
    )

    assert zweiter.callback_id == erster.callback_id
    assert zweiter.summary == erster.summary
    assert session.scalar(select(func.count()).select_from(Callback)) == 1
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_erledigter_rueckruf_blockiert_einen_neuen_nicht(session, tenant_id, call_id):
    erster = create_callback(session, request(tenant_id, call_id), now=NOW)
    erledigt = session.get(Callback, erster.callback_id)
    erledigt.status = "done"
    erledigt.done_by = "gui:team"
    erledigt.done_at = NOW
    session.commit()

    zweiter = create_callback(session, request(tenant_id, call_id), now=NOW)

    assert zweiter.callback_id != erster.callback_id
    assert session.scalar(select(func.count()).select_from(Callback)) == 2


def test_anderer_anruf_bekommt_einen_eigenen_rueckruf(session, tenant_id, call_id):
    erster = create_callback(session, request(tenant_id, call_id), now=NOW)
    anderer_anruf = make_call(session, tenant_id)

    zweiter = create_callback(session, request(tenant_id, anderer_anruf), now=NOW)

    assert zweiter.callback_id != erster.callback_id
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2


def test_unbekannter_anruf_ist_not_found_mit_stoerungssatz(session, tenant_id):
    with pytest.raises(NotFound) as fehler:
        create_callback(session, request(tenant_id, uuid.uuid4()), now=NOW)

    assert fehler.value.say is not None
    assert session.scalar(select(func.count()).select_from(Callback)) == 0


def test_unbekannter_mandant_ist_not_found(session, call_id):
    with pytest.raises(NotFound):
        create_callback(session, request(uuid.uuid4(), call_id), now=NOW)


def test_anruf_eines_anderen_mandanten_ist_not_found(session, tenant_id, call_id):
    fremder = uuid.UUID(
        seed(session, tenant_name="Fremdbetrieb", timezone="Europe/Berlin").tenant_id
    )

    with pytest.raises(NotFound):
        create_callback(session, request(fremder, call_id), now=NOW)


def test_unbrauchbare_rufnummer_ist_invalid_input_mit_nachfrage(
    session, tenant_id, call_id
):
    with pytest.raises(InvalidInput) as fehler:
        create_callback(session, request(tenant_id, call_id, phone="123"), now=NOW)

    assert fehler.value.say is not None
    assert session.scalar(select(func.count()).select_from(Callback)) == 0


def test_summary_aus_leerzeichen_ist_invalid_input(session, tenant_id, call_id):
    with pytest.raises(InvalidInput):
        create_callback(session, request(tenant_id, call_id, summary="   "), now=NOW)


def test_unbekannter_grund_wird_vom_vertrag_abgelehnt(tenant_id, call_id):
    with pytest.raises(ValueError):
        request(tenant_id, call_id, reason="kaese")


def test_gleichzeitige_erstaufrufe_erzeugen_nur_einen_rueckruf(migrated_db_url):
    """Zwei Anrufe gleichzeitig in der Leitung: ohne Sperre findet keiner einen
    bestehenden Rückruf, beide legen an, und das Team ruft den Gast zweimal an."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        call = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=NOW,
            delete_after=date(2026, 9, 15),
        )
        s.add(call)
        s.commit()
        call_id = call.id

    start = threading.Barrier(6)
    results: list[object] = []
    lock = threading.Lock()

    def run() -> None:
        # Auch das Warten an der Barriere gehoert in die Fehlererfassung, sonst
        # verschwindet ein Thread, der gar nicht bis zum Aufruf kommt, spurlos.
        try:
            start.wait(timeout=10)
            with Session(engine) as own:
                outcome: object = create_callback(
                    own, request(tenant, call_id), now=NOW
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
    assert len(results) == 6, f"erwartet sechs Ergebnisse, bekam {len(results)}"

    with Session(engine) as s:
        callbacks = s.scalar(select(func.count()).select_from(Callback))
        events = s.scalar(select(func.count()).select_from(OutboxEvent))
        audits = s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "callback.created")
        )
    engine.dispose()

    fehler = [r for r in results if isinstance(r, Exception)]
    assert not fehler, f"kein Aufruf darf scheitern, bekam {fehler}"
    assert callbacks == 1, f"erwartet ein Rückruf, bekam {callbacks}"
    assert events == 1, f"erwartet ein Ereignis, bekam {events}"
    assert audits == 1, f"erwartet ein Audit-Eintrag, bekam {audits}"
    ids = {r.callback_id for r in results}
    assert len(ids) == 1, f"alle Aufrufe sollen denselben Rückruf liefern, bekam {ids}"


def test_zweiter_aufruf_wartet_auf_die_sperre_und_liest_danach_den_ersten(
    migrated_db_url,
):
    """Kontrollierte Ueberlappung statt Zufall: A haelt die Sperre, B muss warten.

    Die gemeinsame Startbarriere im Test darueber erzwingt keine echte Ueberlappung
    der Transaktionen. Hier wird sie hergestellt: B laeuft erst weiter, wenn A
    committet hat, und muss dann A's Rueckruf lesen statt einen zweiten anzulegen.
    """
    engine = create_engine(migrated_db_url, isolation_level="READ COMMITTED")
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        call = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=NOW,
            delete_after=date(2026, 9, 15),
        )
        s.add(call)
        s.commit()
        call_id = call.id

    ergebnis: list[object] = []

    def spaeter() -> None:
        try:
            with Session(engine) as own:
                ergebnis.append(create_callback(own, request(tenant, call_id), now=NOW))
        except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
            ergebnis.append(exc)

    with Session(engine) as a:
        _lock_call(a, tenant, call_id)
        erster = Callback(
            tenant_id=tenant,
            call_id=call_id,
            phone="+497215551234",
            reason="not_understood",
            summary="Erster Rueckruf",
            status="open",
            created_at=NOW,
        )
        a.add(erster)
        a.flush()
        erste_id = erster.id

        b = threading.Thread(target=spaeter)
        b.start()
        b.join(timeout=1.0)
        assert b.is_alive(), "B haette an der Sperre warten muessen"
        assert not ergebnis, f"B war schon fertig, bevor A committet hat: {ergebnis}"

        a.commit()

    b.join(timeout=30)
    assert not b.is_alive(), "B haengt nach dem Commit von A"
    assert len(ergebnis) == 1
    assert not isinstance(ergebnis[0], Exception), ergebnis[0]
    assert ergebnis[0].callback_id == erste_id, "B haette A's Rueckruf lesen muessen"
    assert ergebnis[0].summary == "Erster Rueckruf"

    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Callback)) == 1
    engine.dispose()


def test_fehler_nach_dem_flush_laesst_keinen_halben_rueckruf_zurueck(
    session, tenant_id, call_id, monkeypatch
):
    """Die Outbox-Zeile scheitert: Rueckruf und Audit duerfen nicht stehenbleiben."""
    import api.domain.callbacks.create as modul

    def kaputt(*_args, **_kwargs):
        raise RuntimeError("Outbox nicht schreibbar")

    monkeypatch.setattr(modul, "enqueue", kaputt)

    with pytest.raises(RuntimeError):
        create_callback(session, request(tenant_id, call_id), now=NOW)

    session.rollback()
    assert session.scalar(select(func.count()).select_from(Callback)) == 0
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
