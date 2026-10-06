"""sim/replay.py und sim/session.py gegen eine echte Datenbank.

Das ist der Durchstich ohne Telefon (docs/11 §sim): Transkript -> Agent -> Fachlogik
-> DB. Geprueft wird der Datenbankzustand, nicht der Text des Agenten (docs/08 §3).
"""

import logging
import random
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.llm import ChatCompletionsLLM
from api.config import settings
from api.models import Call, Callback, Reservation
from scripts.seed import seed
from sim.replay import customer_lines, load_case, replay
from sim.session import SimCall, render_turn, resolve_tenant

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN)  # Dienstag im Abendfenster

FALL = {
    "id": "reservierung_0001",
    "name": "Tisch fuer vier am Abend",
    "transcript": [
        {
            "role": "customer",
            "text": "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
        },
        {"role": "agent", "text": "Auf welchen Namen?"},
        {"role": "customer", "text": "Auf den Namen Mueller."},
        {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
        {"role": "customer", "text": "Ja, passt so."},
    ],
}
FALL_BESCHWERDE = {
    "id": "eskalation_0001",
    "transcript": [
        {
            "role": "customer",
            "text": "Ich moechte mich beschweren, das Essen war kalt.",
        },
    ],
}


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant(session):
    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    return resolve_tenant(session, "Testbetrieb")


def test_transkript_nimmt_nur_die_kundenzuege():
    assert customer_lines(FALL) == [
        "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
        "Auf den Namen Mueller.",
        "Meine Nummer ist 0721 5551234.",
        "Ja, passt so.",
    ]


def test_fall_ohne_kundenzug_wird_abgelehnt(tmp_path):
    """Ein Transkript ohne Kundensatz ergibt kein Gespraech, sondern einen stillen
    Leerlauf - lieber sofort ein Fehler als ein Lauf, der nichts geprueft hat."""
    pfad = tmp_path / "leer.json"
    pfad.write_text('{"id": "leer", "transcript": []}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_case(pfad)


def test_durchstich_reservierung_landet_bestaetigt_in_der_db(session, tenant):
    call, turns = replay(session, FALL, tenant, now=NOW)

    assert turns[-1].ended is True
    reservierung = session.scalars(
        select(Reservation).where(Reservation.call_id == call.call_id)
    ).one()
    assert reservierung.status == "confirmed"
    assert reservierung.party_size == 4
    assert reservierung.guest_name == "Mueller"
    # Rufnummer normalisiert die Fachlogik, nicht der Simulator (CLAUDE.md §8).
    assert reservierung.phone == "+497215551234"

    ended = call.finish()
    assert ended.outcome == "completed"


def test_jeder_tool_aufruf_steht_im_anruf_log(session, tenant):
    """docs/04 §Gemeinsame Regeln: jeder Aufruf mit Dauer und Ergebnis in
    `calls.tool_calls` - auch auf dem direkten Weg ohne HTTP."""
    call, turns = replay(session, FALL, tenant, now=NOW)

    protokoll = session.execute(
        select(Call.tool_calls).where(Call.id == call.call_id)
    ).scalar_one()
    namen = [eintrag["name"] for eintrag in protokoll]
    assert namen == [
        "get_service_status",
        "check_slot",
        "create_reservation",
        "confirm",
    ]
    assert all(eintrag["duration_ms"] >= 0 for eintrag in protokoll)
    # Was angezeigt wird, kommt aus genau diesem Protokoll.
    assert sum(len(turn.tools) for turn in turns) == len(protokoll)


def test_beschwerde_bucht_nichts_und_gibt_ab(session, tenant):
    """docs/05 §4: Beschwerde ist ein Sofort-Ausloeser, vor dem Modell."""
    call, turns = replay(session, FALL_BESCHWERDE, tenant, now=NOW)

    assert turns[-1].ended is True
    assert call.state.stage in {"transferred", "callback", "ended"}
    assert (
        session.scalars(
            select(Reservation).where(Reservation.call_id == call.call_id)
        ).all()
        == []
    )
    assert call.finish().outcome in {"transferred", "callback", "abandoned"}


def test_rauschen_laesst_das_gespraech_nicht_abstuerzen(session, tenant):
    """Voll verrauscht versteht der Agent nichts mehr. Erwartet wird kein Erfolg,
    sondern ein sauberes Ende: kein Absturz, keine halbe Reservierung."""
    call, turns = replay(
        session, FALL, tenant, noise=1.0, rng=random.Random(5), now=NOW
    )

    assert turns
    offen = session.scalars(
        select(Reservation).where(
            Reservation.call_id == call.call_id, Reservation.status == "confirmed"
        )
    ).all()
    assert offen == []


def test_unbekannter_mandant_faellt_auf(session):
    from api.core.errors import NotFound

    with pytest.raises(NotFound):
        resolve_tenant(session, "gibt es nicht")


def test_zustand_je_zug_wird_festgehalten(session, tenant):
    """Der Zustand in der Ausgabe ist der von damals, nicht der vom Gespraechsende."""
    _call, turns = replay(session, FALL, tenant, now=NOW)

    assert turns[0].state["stage"] == "start"
    assert turns[-1].state["stage"] == "confirmed"
    assert "Kunde:" in render_turn(turns[0])


def test_zwei_anrufe_stoeren_sich_nicht(session, tenant):
    """Jeder Lauf bekommt seine eigene Anruf-Zeile: sonst schriebe der zweite Lauf
    seine Tool-Aufrufe in den Anruf des ersten."""
    erst, _ = replay(session, FALL, tenant, now=NOW)
    zweit, _ = replay(session, FALL, tenant, now=NOW)

    assert erst.call_id != zweit.call_id


def test_rueckruf_bleibt_ohne_reservierung(session, tenant):
    """Anliegen ausserhalb von Version 1 (Speisekarte) endet als Rueckruf-Aufgabe."""
    fall = {
        "id": "rueckruf_0001",
        "transcript": [
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Haben Sie eine Speisekarte?"},
        ],
    }

    call, _ = replay(session, fall, tenant, now=NOW)

    rueckrufe = session.scalars(
        select(Callback).where(Callback.call_id == call.call_id)
    ).all()
    assert [r.reason for r in rueckrufe] == ["out_of_scope"]


def test_sim_call_ohne_transkript_endet_als_abbruch(session, tenant):
    call = SimCall(session, tenant, now=NOW, external_session_id=f"sim-{uuid.uuid4()}")

    assert call.finish().outcome == "abandoned"


def test_dauer_wird_beim_abschluss_gemessen(session, tenant, monkeypatch):
    """Codex-Review PR #104 (P2): ein Gespraech im Terminal dauert so lange, wie es
    dauert. Mit dem Startzeitpunkt auch am Ende stuende in jedem Anruf 0 Sekunden
    und die Gespraechsdauer waere als Kennzahl wertlos."""
    zeiten = iter(
        [
            datetime(2026, 9, 15, 18, 0, tzinfo=BERLIN),
            datetime(2026, 9, 15, 18, 2, tzinfo=BERLIN),
        ]
    )
    monkeypatch.setattr("sim.session.utcnow", lambda: next(zeiten))

    call = SimCall(session, tenant, external_session_id=f"sim-{uuid.uuid4()}")

    assert call.finish().duration_seconds == 120


def test_fester_zeitpunkt_bleibt_fuer_die_wiedergabe_fest(session, tenant):
    """Mit `--now` bleibt der Lauf reproduzierbar: derselbe Fall ergibt dieselbe Dauer."""
    call = SimCall(session, tenant, now=NOW, external_session_id=f"sim-{uuid.uuid4()}")

    assert call.finish().duration_seconds == 0


def test_korrektur_beim_vorlesen_bucht_die_neue_zahl(session, tenant):
    """Codex-Review PR #104 (P1): der bestaetigte Vorgang muss der korrigierte sein."""
    fall = {
        "id": "reservierung_0002",
        "transcript": [
            {
                "role": "customer",
                "text": "Einen Tisch fuer vier Personen morgen um 19 Uhr.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Nein, wir sind fuenf Personen."},
            {"role": "customer", "text": "Ja, passt so."},
        ],
    }

    call, _ = replay(session, fall, tenant, now=NOW)

    bestaetigt = session.scalars(
        select(Reservation).where(
            Reservation.call_id == call.call_id, Reservation.status == "confirmed"
        )
    ).all()
    assert [r.party_size for r in bestaetigt] == [5]


def test_tisch_bestellen_bleibt_eine_reservierung(session, tenant):
    """Codex PR #130, P1: "einen Tisch bestellen" ist eine Reservierung, keine
    Abholung. "bestell" allein schaltet nicht in den Bestellfluss."""
    fall = {
        "id": "tisch_bestellen",
        "transcript": [
            {
                "role": "customer",
                "text": "Ich moechte fuer morgen um 19 Uhr einen Tisch fuer vier "
                "Personen bestellen.",
            },
            {"role": "customer", "text": "Auf den Namen Mueller."},
            {"role": "customer", "text": "Meine Nummer ist 0721 5551234."},
            {"role": "customer", "text": "Ja, passt so."},
        ],
    }
    call, turns = replay(session, fall, tenant, now=NOW)

    assert all("search_menu" not in str(t.tools) for t in turns)
    reservierung = session.scalars(
        select(Reservation).where(Reservation.call_id == call.call_id)
    ).one()
    assert reservierung.status == "confirmed"


# --- Model, tokens and cost in the call log (T-2.4) -----------------------------


def model_answering(content, usage):
    """A real model client whose server always gives the same recorded answer."""

    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}], "usage": usage},
        )

    return ChatCompletionsLLM(
        "http://model.test/v1", "test-model", transport=httpx.MockTransport(handler)
    )


def usage_lines(caplog):
    return [r.extra for r in caplog.records if r.getMessage() == "model usage"]


def test_call_with_a_model_logs_model_tokens_and_cost(
    session, tenant, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 1500)
    llm = model_answering(
        '{"say": "Für wie viele Personen?"}',
        {"prompt_tokens": 9000, "completion_tokens": 400},
    )
    call = SimCall(session, tenant, llm=llm, now=NOW)
    call.say("Guten Tag, einen Tisch bitte.")
    call.say("Morgen Abend.")

    with caplog.at_level(logging.INFO, logger="sim.session"):
        call.finish()

    row = session.get(Call, call.call_id)
    assert row.model == "test-model"
    # 18000 x 300 + 800 x 1500 = 6.6 million, per million tokens: 6.6 cents, up.
    assert row.cost_cents == 7
    assert usage_lines(caplog) == [
        {
            "call_id": str(call.call_id),
            "model": "test-model",
            "requests": 2,
            "prompt_tokens": 18000,
            "completion_tokens": 800,
            "unmetered": 0,
            "cost_cents": 7,
        }
    ]


def test_call_with_an_unmetered_answer_logs_no_cost(
    session, tenant, monkeypatch, caplog
):
    """Prices are set, but the server sent no token numbers: the cost of the
    call is unknown, not 0 (Codex PR #211, P1)."""
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 1500)
    call = SimCall(
        session, tenant, llm=model_answering('{"say": "Gern."}', {}), now=NOW
    )
    call.say("Hallo")

    with caplog.at_level(logging.INFO, logger="sim.session"):
        call.finish()

    row = session.get(Call, call.call_id)
    assert row.model == "test-model"
    assert row.cost_cents is None
    (line,) = usage_lines(caplog)
    assert line["unmetered"] == 1
    assert line["cost_cents"] is None


def test_call_that_never_asked_its_model_logs_no_model(
    session, tenant, monkeypatch, caplog
):
    """The caller hangs up before the first sentence: the call log must not
    claim that a model ran (Codex PR #211, P2)."""
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 1500)
    llm = model_answering(
        '{"say": "Gern."}', {"prompt_tokens": 5, "completion_tokens": 1}
    )
    call = SimCall(session, tenant, llm=llm, now=NOW)

    with caplog.at_level(logging.INFO, logger="sim.session"):
        call.finish()

    row = session.get(Call, call.call_id)
    assert row.model is None
    assert row.cost_cents is None
    assert usage_lines(caplog) == []


def test_call_with_a_model_without_prices_logs_tokens_and_no_cost(
    session, tenant, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", None)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", None)
    llm = model_answering(
        '{"say": "Gern."}', {"prompt_tokens": 500, "completion_tokens": 10}
    )
    call = SimCall(session, tenant, llm=llm, now=NOW)
    call.say("Hallo")

    with caplog.at_level(logging.INFO, logger="sim.session"):
        call.finish()

    row = session.get(Call, call.call_id)
    assert row.model == "test-model"
    assert row.cost_cents is None
    (line,) = usage_lines(caplog)
    assert line["prompt_tokens"] == 500
    assert line["cost_cents"] is None


def test_call_on_the_scripted_stand_in_logs_no_model_and_no_cost(
    session, tenant, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", 300)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", 1500)
    call = SimCall(session, tenant, now=NOW)
    call.say("Guten Tag")

    with caplog.at_level(logging.INFO, logger="sim.session"):
        call.finish()

    row = session.get(Call, call.call_id)
    assert row.model is None
    assert row.cost_cents is None
    assert usage_lines(caplog) == []
