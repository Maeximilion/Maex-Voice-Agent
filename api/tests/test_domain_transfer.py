"""domain/callbacks/transfer: Durchwahl, Erreichbarkeit, Schleifenschutz (T-1.8)."""

import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.domain.callbacks import transfer_to_team
from api.models import AuditLog, Call
from api.schemas.transfer import TransferToTeamRequest
from scripts.seed import seed

# Dienstag, 20:00 Ortszeit (18:00 UTC) - liegt im Abendfenster 17:00-22:00 der
# Testkonfiguration (docs/01_STATUS.md "Made Assumptions").
OFFEN = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
# Montag ist in der Testkonfiguration durchgehend geschlossen.
GESCHLOSSEN = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


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
        started_at=OFFEN,
        delete_after=date(2026, 9, 15),
    )
    session.add(call)
    session.commit()
    return call.id


@pytest.fixture
def call_id(session, tenant_id) -> uuid.UUID:
    return make_call(session, tenant_id)


def request(tenant_id, call_id, **overrides) -> TransferToTeamRequest:
    return TransferToTeamRequest(
        **{
            "call_id": call_id,
            "tenant_id": tenant_id,
            "reason": "human_requested",
            **overrides,
        }
    )


def test_uebergabe_liefert_durchwahl_und_setzt_audit(session, tenant_id, call_id):
    result = transfer_to_team(session, request(tenant_id, call_id), now=OFFEN)

    assert result.transfer_to  # die Durchwahl aus service_config, nie eine Hauptnummer
    assert result.available is True

    call = session.get(Call, call_id)
    assert call.transfer_reason == "human_requested"

    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "call.transferred")
    ).all()
    assert len(audit) == 1
    assert audit[0].entity == "call"
    assert audit[0].payload["reason"] == "human_requested"


def test_ausserhalb_der_oeffnungszeit_ist_niemand_erreichbar(
    session, tenant_id, call_id
):
    result = transfer_to_team(session, request(tenant_id, call_id), now=GESCHLOSSEN)

    assert result.available is False
    # Ziel bleibt die Durchwahl; der Agent legt stattdessen einen Rückruf an.
    assert result.transfer_to

    # Kein Übergang fand statt: weder Zustand noch Audit dürfen davon ausgehen,
    # dass das Team den Anruf schon bekommen hat (Codex-Review PR #98, P2).
    call = session.get(Call, call_id)
    assert call.transfer_reason is None
    audits = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "call.transferred")
    )
    assert audits == 0


def test_spaeterer_echter_uebergang_ist_nach_nicht_erreichbar_noch_moeglich(
    session, tenant_id, call_id
):
    """Ein folgenloser Versuch außerhalb der Öffnungszeit darf einen späteren
    echten Übergang im selben Anruf nicht blockieren."""
    transfer_to_team(session, request(tenant_id, call_id), now=GESCHLOSSEN)

    result = transfer_to_team(session, request(tenant_id, call_id), now=OFFEN)

    assert result.available is True
    call = session.get(Call, call_id)
    assert call.transfer_reason == "human_requested"
    audits = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "call.transferred")
    )
    assert audits == 1


def test_zweiter_aufruf_im_selben_anruf_schreibt_kein_zweites_audit(
    session, tenant_id, call_id
):
    erster = transfer_to_team(session, request(tenant_id, call_id), now=OFFEN)

    zweiter = transfer_to_team(
        session, request(tenant_id, call_id, reason="complaint"), now=OFFEN
    )

    assert zweiter.transfer_to == erster.transfer_to
    call = session.get(Call, call_id)
    # Der erste Grund bleibt stehen, der zweite Aufruf ändert den Zustand nicht mehr.
    assert call.transfer_reason == "human_requested"
    audits = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "call.transferred")
    )
    assert audits == 1


def test_anderer_anruf_bekommt_ein_eigenes_audit(session, tenant_id, call_id):
    transfer_to_team(session, request(tenant_id, call_id), now=OFFEN)
    anderer_anruf = make_call(session, tenant_id)

    transfer_to_team(session, request(tenant_id, anderer_anruf), now=OFFEN)

    audits = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "call.transferred")
    )
    assert audits == 2


def test_unbekannter_anruf_ist_not_found_mit_stoerungssatz(session, tenant_id):
    with pytest.raises(NotFound) as fehler:
        transfer_to_team(session, request(tenant_id, uuid.uuid4()), now=OFFEN)

    assert fehler.value.say is not None


def test_anruf_eines_anderen_mandanten_ist_not_found(session, tenant_id, call_id):
    fremder = uuid.UUID(
        seed(session, tenant_name="Fremdbetrieb", timezone="Europe/Berlin").tenant_id
    )

    with pytest.raises(NotFound):
        transfer_to_team(session, request(fremder, call_id), now=OFFEN)


def test_unbekannter_mandant_ist_not_found(session, call_id):
    with pytest.raises(NotFound):
        transfer_to_team(session, request(uuid.uuid4(), call_id), now=OFFEN)


def test_unbekannter_grund_wird_vom_vertrag_abgelehnt(tenant_id, call_id):
    with pytest.raises(ValueError):
        request(tenant_id, call_id, reason="kaese")


def test_storno_ist_ein_gueltiger_grund(session, tenant_id, call_id):
    """docs/05 §Harte Regeln: Storno löst transfer_to_team aus wie Beschwerde
    oder Mensch-Wunsch, ist aber kein Rückruf-Grund aus docs/03 (Codex PR #98, P1)."""
    result = transfer_to_team(
        session, request(tenant_id, call_id, reason="cancellation"), now=OFFEN
    )

    assert result.available is True
    call = session.get(Call, call_id)
    assert call.transfer_reason == "cancellation"


def test_parallele_uebergabe_desselben_anrufs_legt_nur_ein_audit_an(migrated_db_url):
    """Acht gleichzeitige transfer_to_team-Aufrufe auf denselben Anruf: ein Audit-Eintrag."""
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        tenant = uuid.UUID(
            seed(s, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        call = Call(
            tenant_id=tenant,
            external_session_id="ext",
            started_at=OFFEN,
            delete_after=date(2026, 9, 15),
        )
        s.add(call)
        s.commit()
        call_id = call.id

    start = threading.Barrier(8)
    results: list[object] = []
    lock = threading.Lock()

    def run() -> None:
        start.wait(timeout=10)
        with Session(engine) as own:
            try:
                transfer_to_team(own, request(tenant, call_id), now=OFFEN)
                outcome: object = "ok"
            except Exception as exc:  # noqa: BLE001 - im Test soll jeder Fehler auffallen
                outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as s:
        audits = s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "call.transferred")
        )
    engine.dispose()

    assert results.count("ok") == 8, f"alle Aufrufe sollen antworten, bekam {results}"
    assert audits == 1, f"erwartet ein Audit-Eintrag, bekam {audits}"
