"""Eval-Runner (T-5.1, docs/08 §3): Fälle laufen, Datenbank entscheidet, harte Metriken greifen."""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.agent.llm import FakeLLM, LLMTurn, ToolCall
from api.domain.confirm import confirm
from api.domain.ordering import draft_order
from api.models import MenuItem
from api.schemas.confirm import ConfirmRequest
from api.schemas.orders import DraftOrderRequest
from evals import runner
from evals.judge import CaseError, Observed, judge, observe
from evals.recorder import RecordingLLM, is_yes
from evals.report import CaseResult, RunReport, previous_run, write
from sim.session import SimCall

CASES = Path(runner.__file__).parent / "cases"


def fall(case_id: str, lines: list[str], expected: dict, **extra) -> dict:
    return {
        "id": case_id,
        "name": case_id,
        "tags": extra.pop("tags", ["test"]),
        "transcript": [{"role": "customer", "text": t} for t in lines],
        "expected": expected,
        **extra,
    }


def write_cases(folder: Path, *cases: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for c in cases:
        (folder / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    return folder


def run(migrated_db_url, cases_dir, report_dir, **kw) -> RunReport:
    return runner.run(
        cases_dir=cases_dir,
        report_dir=report_dir,
        db_url=migrated_db_url,
        stamp=kw.pop("stamp", datetime.now(UTC)),
        **kw,
    )


# --- Ganzer Lauf -------------------------------------------------------------------


def test_suite_aus_dem_repo_besteht_mit_report(migrated_db_url, tmp_path):
    cases = tmp_path / "cases"
    shutil.copytree(CASES, cases, ignore=shutil.ignore_patterns("*.jsonl"))
    report = run(migrated_db_url, cases, tmp_path / "reports")

    assert report.total == len(list(CASES.glob("*.json")))
    assert report.verdict == "bestanden", report.to_markdown()
    assert report.accuracy == 1.0
    assert report.hard() == {
        "guessed_items": 0,
        "unconfirmed": 0,
        "missed_escalations": 0,
    }
    [json_file] = (tmp_path / "reports").glob("*.json")
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["accuracy"] == 1.0 and data["verdict"] == "bestanden"
    assert list((tmp_path / "reports").glob("*.md"))


def test_falsche_erwartung_ist_roter_fall_und_regression_faellt_durch(
    migrated_db_url, tmp_path
):
    gut = fall(
        "gut",
        [
            "Hallo, ich moechte etwas zum Abholen bestellen.",
            "Einmal die 13.",
            "Nein, das wars.",
            "Auf den Namen Schmidt.",
            "Ja, genau.",
        ],
        {
            "intent": "pickup",
            "confirmed": True,
            "items": [{"number": "13", "quantity": 1}],
        },
        caller_id="+497215551234",
    )
    cases = write_cases(tmp_path / "c", gut)
    reports = tmp_path / "r"
    erster = run(
        migrated_db_url, cases, reports, stamp=datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    )
    assert erster.accuracy == 1.0, erster.to_markdown()

    # Dieselbe Bestellung, aber der Fall erwartet zwei Stück: der Code bucht eins.
    falsch = dict(
        gut, expected={**gut["expected"], "items": [{"number": "13", "quantity": 2}]}
    )
    write_cases(cases, falsch)
    zweiter = run(
        migrated_db_url, cases, reports, stamp=datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    )

    [case] = zweiter.cases
    assert not case.passed
    assert "items" in case.diffs[0]
    assert zweiter.verdict == "durchgefallen"
    assert any("Genauigkeit gefallen" in r for r in zweiter.reasons)


def test_verpasste_eskalation_ist_hart(migrated_db_url, tmp_path):
    # Nichts im Transkript verlangt nach einem Menschen, der Fall erwartet es aber.
    cases = write_cases(
        tmp_path / "c",
        fall("still", ["Einmal die 13 zum Abholen."], {"escalated": True}),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    assert report.hard()["missed_escalations"] == 1
    assert report.verdict == "durchgefallen"


def test_tag_filter_und_leere_auswahl(migrated_db_url, tmp_path):
    cases = write_cases(
        tmp_path / "c",
        fall(
            "a", ["Ich habe eine Beschwerde."], {"escalated": True}, tags=["eskalation"]
        ),
        fall("b", ["Einmal die 13."], {"confirmed": False}, tags=["abholung"]),
    )
    report = run(migrated_db_url, cases, tmp_path / "r", tags=["eskalation"])
    assert [c.id for c in report.cases] == ["a"]
    assert report.cases[0].passed, report.to_markdown()
    with pytest.raises(CaseError):
        run(migrated_db_url, cases, tmp_path / "r", tags=["gibtsnicht"])


# --- Kaputte Fälle und Aufrufe ------------------------------------------------------


@pytest.mark.parametrize(
    "kaputt",
    [
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"confimed": True},
        },
        {"id": "x", "transcript": [{"role": "customer", "text": "Hallo"}]},
        {"id": "x", "transcript": [], "expected": {}},
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"items": [{"number": "13"}]},
        },
        {
            "id": "x",
            "now": "2026-09-15T18:00",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {},
        },
    ],
)
def test_kaputter_fall_bricht_ab_statt_gruen(tmp_path, kaputt):
    folder = tmp_path / "c"
    folder.mkdir()
    (folder / "x.json").write_text(json.dumps(kaputt), encoding="utf-8")
    with pytest.raises(CaseError):
        runner.load_cases(folder, [])


def test_doppelte_id_und_unbekanntes_modell(tmp_path):
    folder = write_cases(tmp_path / "c", fall("a", ["Hallo"], {}))
    (folder / "a2.json").write_text(
        json.dumps(fall("a", ["Hallo"], {})), encoding="utf-8"
    )
    with pytest.raises(CaseError):
        runner.load_cases(folder, [])
    assert runner.main(["--model", "gpt-irgendwas", "--cases", str(folder)]) == 2


# --- Beobachter: geratene Position und confirm ohne Ja ------------------------------


def _search_result(*ids: str) -> str:
    hits = [{"menu_item_id": i, "number": "13"} for i in ids]
    return json.dumps({"tool": "search_menu", "ok": True, "data": {"hits": hits}})


def test_position_ohne_suchtreffer_gilt_als_geraten():
    known, invented = str(uuid.uuid4()), str(uuid.uuid4())
    draft = LLMTurn(
        tool_call=ToolCall(
            "draft_order",
            {"items": [{"menu_item_id": known}, {"menu_item_id": invented}]},
        )
    )
    rec = RecordingLLM(FakeLLM([LLMTurn(say="ok"), draft]))
    rec.next_turn("", {}, "Einmal die 13 und die Ente.")
    rec.next_turn("", {}, _search_result(known))
    assert [g[0] for g in rec.recording.guessed] == [invented]


def test_confirm_nach_nein_gilt_als_unbestaetigt():
    confirm_turn = LLMTurn(tool_call=ToolCall("confirm", {}))
    rec = RecordingLLM(FakeLLM([confirm_turn, confirm_turn]))
    rec.next_turn("", {}, "Nein, das stimmt nicht.")
    rec.next_turn("", {}, "Ja, passt so.")
    assert rec.recording.unconfirmed == ["Nein, das stimmt nicht."]
    assert rec.recording.confirms == 2


@pytest.mark.parametrize(
    ("text", "yes"),
    [
        ("Ja, passt so.", True),
        ("Genau.", True),
        ("Ja genau", True),
        ("Nein, das wars.", False),
        ("Nein, ja doch nicht", False),
        ("Im Januar", False),
        ("Müller", False),
    ],
)
def test_ja_erkennung(text, yes):
    assert is_yes(text) is yes


# --- Datenbank statt Modelltext -----------------------------------------------------


def test_gebucht_ohne_confirm_des_modells_wird_erkannt(migrated_db_url):
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session:
            tenant = runner._prepare(session, runner.DEFAULT_NOW)
            call = SimCall(session, tenant, now=runner.DEFAULT_NOW)
            item_id = session.scalar(
                select(MenuItem.id).where(
                    MenuItem.tenant_id == tenant.id, MenuItem.number == "13"
                )
            )
            draft = draft_order(
                session,
                DraftOrderRequest(
                    call_id=call.call_id,
                    tenant_id=tenant.id,
                    idempotency_key=uuid.uuid4().hex,
                    type="pickup",
                    customer={"name": "Schmidt", "phone": "0721 5551234"},
                    items=[{"menu_item_id": item_id, "quantity": 1}],
                ),
                now=runner.DEFAULT_NOW,
            )
            # An der Regel vorbei bestaetigt: kein confirm vom Modell.
            confirm(
                session,
                ConfirmRequest(
                    call_id=call.call_id,
                    tenant_id=tenant.id,
                    entity="order",
                    entity_id=draft.order_id,
                    idempotency_key="k",
                ),
            )
            seen = observe(session, call.call_id, confirms=0)
            assert seen.confirmed and seen.confirmed_without_confirm == 1
            assert judge({"items": [{"number": "13", "quantity": 1}]}, seen) == []
    finally:
        engine.dispose()


def test_vergleich_nur_felder_aus_expected():
    seen = Observed(
        intent="pickup",
        confirmed=True,
        escalated=False,
        items=[{"number": "47", "quantity": 1, "options": ["Huhn"]}],
        customer_name="Müller",
        party_size=None,
    )
    assert judge({"items": [{"number": "47", "quantity": 1}]}, seen) == []
    assert judge({"customer_name": "müller"}, seen) == []
    diffs = judge(
        {"items": [{"number": "47", "quantity": 1, "options": ["Ente"]}]}, seen
    )
    assert diffs and "items" in diffs[0]


def test_vorheriger_lauf_nur_mit_gleichem_modell_und_tags(tmp_path):
    def rep(tags, acc_cases):
        r = RunReport(run_at="t", model="scripted", tags=tags, cases=acc_cases)
        r.decide()
        return r

    ok = CaseResult(id="a", name="", tags=[], passed=True)
    write(rep(["menu"], [ok]), tmp_path, datetime(2026, 9, 25, 8, tzinfo=UTC))
    assert previous_run(tmp_path, "scripted", []) is None
    assert previous_run(tmp_path, "scripted", ["menu"])["accuracy"] == 1.0
