"""agent/dispatch.py: Tool-Name -> domain-Funktion ohne HTTP-Umweg (docs/11 §agent).

Deckt ab: jedes der sechs Stufe-1-Tools läuft durch, ein unbekannter Toolname und
fehlende Pflichtfelder werden als invalid_input beantwortet statt zu crashen, die
Reservierungs-/confirm-Idempotenz wird ohne Zutun des Modells erzeugt, und jeder
Aufruf landet in calls.tool_calls (docs/04 §Gemeinsame Regeln) -- auch fehlgeschlagene.
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.dispatch import dispatch
from api.models import Call
from scripts.seed import seed

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 8, 0, tzinfo=BERLIN)


def berlin(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=BERLIN)


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


@pytest.fixture
def call_id(session, tenant_id):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=NOW,
        delete_after=DIENSTAG,
    )
    session.add(call)
    session.commit()
    return call.id


def tool_calls(session, call_id) -> list[dict]:
    session.expire_all()
    return session.get(Call, call_id).tool_calls


def test_get_service_status_laeuft_ohne_zusatzargumente(session, tenant_id, call_id):
    result = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)

    assert result.ok
    assert "is_open" in result.data
    entries = tool_calls(session, call_id)
    assert entries[-1]["name"] == "get_service_status"
    assert entries[-1]["ok"] is True
    assert entries[-1]["duration_ms"] >= 0


def test_check_slot_frei(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": berlin(DIENSTAG, 18, 30), "party_size": 4},
        now=NOW,
    )

    assert result.ok
    assert result.data["available"] is True


def test_create_reservation_erzeugt_eigenen_idempotency_key(
    session, tenant_id, call_id
):
    """Das Modell liefert nie einen Schlüssel; Code entscheidet ihn (CLAUDE.md §2 Regel 1)."""
    args = {
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
    }

    first = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    assert first.ok
    assert first.data["status"] == "draft"

    # Gleicher Anruf, gleiches Tool, gleiche Eingabe -> gleicher Schlüssel, kein zweiter Entwurf.
    second = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    assert second.data["reservation_id"] == first.data["reservation_id"]


def test_confirm_erzeugt_eigenen_idempotency_key(session, tenant_id, call_id):
    draft = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            "guest_name": "Müller",
            "phone": "+4972215551234",
            "party_size": 4,
            "reserved_for": berlin(DIENSTAG, 18, 30),
        },
        now=NOW,
    )

    result = dispatch(
        session,
        call_id,
        tenant_id,
        "confirm",
        {"entity": "reservation", "entity_id": draft.data["reservation_id"]},
        now=NOW,
    )

    assert result.ok
    assert result.data["status"] == "confirmed"


def test_create_callback(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "create_callback",
        {
            "phone": "+4972215551234",
            "reason": "human_requested",
            "summary": "Wunsch nach Mensch",
        },
        now=NOW,
    )
    assert result.ok
    assert result.data["status"] == "open"


def test_transfer_to_team(session, tenant_id, call_id):
    result = dispatch(
        session,
        call_id,
        tenant_id,
        "transfer_to_team",
        {"reason": "complaint"},
        now=NOW,
    )
    assert result.ok
    assert "transfer_to" in result.data


def test_unbekanntes_tool_ist_invalid_input_statt_crash(session, tenant_id, call_id):
    result = dispatch(session, call_id, tenant_id, "does_not_exist", {}, now=NOW)

    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert tool_calls(session, call_id)[-1]["error_code"] == "invalid_input"


def test_fehlendes_pflichtfeld_ist_invalid_input(session, tenant_id, call_id):
    result = dispatch(
        session, call_id, tenant_id, "check_slot", {"party_size": 4}, now=NOW
    )

    assert result.ok is False
    assert result.error_code == "invalid_input"


def test_domain_fehler_wird_geloggt_und_sitzung_bleibt_nutzbar(
    session, tenant_id, call_id
):
    """Ein AppError (Zeitpunkt in der Vergangenheit) darf die Sitzung nicht für den
    nächsten Tool-Aufruf verderben -- die Sitzung lebt hier über den ganzen Anruf,
    anders als im HTTP-Pfad mit einer frischen Sitzung je Request."""
    failed = dispatch(
        session,
        call_id,
        tenant_id,
        "check_slot",
        {"reserved_for": NOW - timedelta(hours=1), "party_size": 4},
        now=NOW,
    )
    assert failed.ok is False
    assert failed.error_code == "invalid_input"

    # Nach dem Fehler funktioniert ein unabhängiges Tool auf derselben Sitzung immer noch.
    status = dispatch(session, call_id, tenant_id, "get_service_status", {}, now=NOW)
    assert status.ok

    entries = tool_calls(session, call_id)
    assert [e["name"] for e in entries] == ["check_slot", "get_service_status"]
    assert entries[0]["ok"] is False
    assert entries[0]["error_code"] == "invalid_input"
    assert entries[1]["ok"] is True


def test_geaenderte_notiz_ist_ein_neuer_entwurf(session, tenant_id, call_id):
    """Offener Punkt aus PR #127 (T-4.10): aendert der Gast nach dem Vorlesen nur
    die Notiz ("mit Hochstuhl"), entsteht ein neuer Entwurf mit neuem readback -
    sonst kaeme der alte ohne Notiz zurueck."""
    args = {
        "guest_name": "Müller",
        "phone": "+4972215551234",
        "party_size": 4,
        "reserved_for": berlin(DIENSTAG, 18, 30),
    }
    first = dispatch(
        session, call_id, tenant_id, "create_reservation", dict(args), now=NOW
    )
    second = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {**args, "note": "mit Hochstuhl"},
        now=NOW,
    )
    assert second.ok
    assert second.data["reservation_id"] != first.data["reservation_id"]
    assert second.data["note"] == "mit Hochstuhl"


def test_schluessel_gleich_bei_anderer_schreibweise(session, tenant_id, call_id):
    """Review PR #139: dieselbe Reservierung mit anders geschriebener Nummer und
    anderer Zeitzone im Zeitstempel ist kein zweiter Entwurf."""
    from datetime import UTC

    wann = berlin(DIENSTAG, 18, 30)
    args = {"guest_name": "Müller", "party_size": 4}
    first = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {**args, "phone": "+4972215551234", "reserved_for": wann.isoformat()},
        now=NOW,
    )
    second = dispatch(
        session,
        call_id,
        tenant_id,
        "create_reservation",
        {
            **args,
            "phone": "07221 5551234",
            "reserved_for": wann.astimezone(UTC).isoformat(),
        },
        now=NOW,
    )
    assert second.data["reservation_id"] == first.data["reservation_id"]
