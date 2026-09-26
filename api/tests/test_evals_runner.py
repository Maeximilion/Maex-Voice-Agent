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
from api.models import AuditLog, MenuItem, Tenant
from api.schemas.confirm import ConfirmRequest
from api.schemas.orders import DraftOrderRequest
from evals import runner
from evals.judge import CaseError, Observed, judge, observe, offered_alternatives
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
    # Jeder Fall ohne `pending` ist gruen. Eine bekannte Luecke muss rot sein wie
    # ein striktes xfail: wird sie gruen, faellt das hier auf, und `pending`
    # kommt weg, statt still weiter als Luecke zu gelten (docs/08 §3).
    red = [c.id for c in report.cases if not c.passed and not c.pending]
    assert red == [], report.to_markdown()
    healed = [c.id for c in report.cases if c.passed and c.pending]
    assert healed == [], f"pending entfernen: {healed}"
    assert report.hard() == {
        "guessed_items": 0,
        "unconfirmed": 0,
        "missed_escalations": 0,
    }
    [json_file] = (tmp_path / "reports").glob("*.json")
    data = json.loads(json_file.read_text(encoding="utf-8"))
    assert data["accuracy"] == report.accuracy and data["verdict"] == "bestanden"
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
    assert any("Regression" in r and "gut" in r for r in zweiter.reasons)


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
        {
            "id": "x",
            "sold_out": "48",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"tools": "get_item_details"},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"tools": [{"tool": "get_item_details", "nummer": "23"}]},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"alternatives": "2026-09-21T19:30"},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"alternatives": ["not-a-date"]},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"alternatives": ["2026-09-21T19:30:00+02:00"]},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"alternatives": ["2026-9-21T19:30"]},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {"alternatives": ["2026-02-30T19:30"]},
        },
        {
            "id": "x",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {
                "alternatives": [
                    "2026-09-21T19:00",
                    "2026-09-21T19:30",
                    "2026-09-21T20:00",
                ]
            },
        },
        {
            "id": "x",
            "repeat_confirm": "ja",
            "transcript": [{"role": "customer", "text": "Hallo"}],
            "expected": {},
        },
        {
            "id": "x",
            "pending": " ",
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


def _draft_result() -> str:
    return json.dumps({"tool": "draft_order", "ok": True, "data": {}})


def test_confirm_nach_nein_gilt_als_unbestaetigt():
    draft = LLMTurn(tool_call=ToolCall("draft_order", {"items": []}))
    confirm_turn = LLMTurn(tool_call=ToolCall("confirm", {}))
    rec = RecordingLLM(
        FakeLLM([draft, LLMTurn(say="vorlesen"), confirm_turn, confirm_turn])
    )
    rec.next_turn("", {}, "Einmal die 13.")
    rec.next_turn("", {}, _draft_result())
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
        ("Das stimmt nicht.", False),
        ("Passt so nicht, ich wollte zwei.", False),
        ("Richtig, aber keine Ente.", False),
        ("Das ist nicht richtig.", False),
        ("Ja, passt, kein Problem.", True),
        ("Ja, nicht schlecht.", True),
    ],
)
def test_ja_erkennung(text, yes):
    assert is_yes(text) is yes


# --- Datenbank statt Modelltext -----------------------------------------------------


def test_gebucht_ohne_confirm_des_modells_wird_erkannt(migrated_db_url):
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session:
            tenant = runner._prepare(session, runner.DEFAULT_NOW, runner.menu_plan())
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


# --- Befunde aus dem Review von PR #142 ----------------------------------------------


def _report(cases, previous=None):
    r = RunReport(run_at="t", model="scripted", tags=[], cases=cases, previous=previous)
    r.decide()
    return r


def test_neuer_roter_fall_ist_keine_regression():
    before = {"file": "alt.json", "accuracy": 1.0, "passed_ids": ["a", "b"]}
    neu_rot = [
        CaseResult(id="a", name="", tags=[], passed=True),
        CaseResult(id="b", name="", tags=[], passed=True),
        CaseResult(id="bug", name="", tags=[], passed=False),
    ]
    assert _report(neu_rot, before).verdict == "bestanden"
    kaputt = [
        CaseResult(id="a", name="", tags=[], passed=False),
        CaseResult(id="b", name="", tags=[], passed=True),
        CaseResult(id="neu", name="", tags=[], passed=True),
    ]
    assert _report(kaputt, before).verdict == "durchgefallen"


def test_durchgefallener_lauf_ist_kein_massstab(tmp_path):
    gruen = _report([CaseResult(id="a", name="", tags=[], passed=True)])
    write(gruen, tmp_path, datetime(2026, 9, 25, 8, tzinfo=UTC))
    rot = _report(
        [CaseResult(id="a", name="", tags=[], passed=False)],
        previous_run(tmp_path, "scripted", []),
    )
    assert rot.verdict == "durchgefallen"
    write(rot, tmp_path, datetime(2026, 9, 25, 9, tzinfo=UTC))
    # Der zweite Aufruf ohne Fix vergleicht weiter mit dem gruenen Lauf.
    again = previous_run(tmp_path, "scripted", [])
    assert again["passed_ids"] == ["a"]


def test_optionen_nur_an_der_position_die_sie_nennt():
    seen = Observed(
        intent="pickup",
        confirmed=True,
        escalated=False,
        items=[
            {"number": "23", "quantity": 2, "options": ["Erdnuss"]},
            {"number": "47", "quantity": 1, "options": ["Huhn"]},
        ],
        customer_name=None,
        party_size=None,
    )
    want = [
        {"number": "23", "quantity": 2},
        {"number": "47", "quantity": 1, "options": ["Huhn"]},
    ]
    assert judge({"items": want}, seen) == []
    assert judge({"items": [{"number": "23", "quantity": 2}]}, seen)  # 47 zu viel


def test_faelle_teilen_keinen_mandanten(migrated_db_url, tmp_path):
    # Derselbe Tisch zweimal: mit geteiltem Mandanten naehme der erste dem
    # zweiten die Kapazitaet nicht, hier aber zaehlt jeder Fall fuer sich.
    zeilen = [
        "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr.",
        "Auf den Namen Mueller.",
        "Meine Nummer ist 0721 5551234.",
        "Ja, passt so.",
    ]
    cases = write_cases(
        tmp_path / "c",
        fall("t1", zeilen, {"confirmed": True}),
        fall("t2", zeilen, {"confirmed": True}),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    assert report.accuracy == 1.0, report.to_markdown()
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session:
            names = sorted(session.scalars(select(Tenant.name)))
    finally:
        engine.dispose()
    assert names == ["Evalbetrieb t1", "Evalbetrieb t2"]


def test_abgestuerzter_fall_laesst_den_lauf_durchfallen():
    crashed = CaseResult(
        id="x", name="", tags=[], passed=False, error="TypeError: kaputt"
    )
    report = _report([crashed])
    assert report.verdict == "durchgefallen"
    assert any("Abgestürzt" in r for r in report.reasons)


def test_doppelte_id_auch_mit_getrennten_tags(tmp_path):
    folder = write_cases(tmp_path / "c", fall("a", ["Hallo"], {}, tags=["x"]))
    (folder / "a2.json").write_text(
        json.dumps(fall("a", ["Hallo"], {}, tags=["y"])), encoding="utf-8"
    )
    with pytest.raises(CaseError):
        runner.load_cases(folder, ["x"])


def test_ja_vor_dem_entwurf_bestaetigt_nichts():
    # "Ja" im ersten Satz, danach Entwurf und confirm im selben Zug, ohne
    # Vorlesen: kein Ja zum Vorgang.
    rec = RecordingLLM(
        FakeLLM(
            [
                LLMTurn(tool_call=ToolCall("draft_order", {"items": []})),
                LLMTurn(tool_call=ToolCall("confirm", {})),
            ]
        )
    )
    rec.next_turn("", {}, "Ja, guten Tag, einmal die 13.")
    rec.next_turn("", {}, _draft_result())
    assert rec.recording.unconfirmed == ["Ja, guten Tag, einmal die 13."]


# --- Eval-Suite v1 (T-5.2) -----------------------------------------------------------

BESTELLUNG = [
    "Guten Tag, ich moechte etwas zum Abholen bestellen.",
    "Die 48.",
    "Dann die 13.",
    "Nein, das wars.",
    "Auf den Namen Mueller.",
    "Meine Nummer ist 0721 5551234.",
    "Ja, passt so.",
]


def test_ausverkauft_aus_dem_fall(migrated_db_url, tmp_path):
    """`sold_out` setzt "heute aus" nur fuer diesen Fall: die 48 kommt nicht in
    die Bestellung, im Fall daneben ist sie normal bestellbar."""
    cases = write_cases(
        tmp_path / "c",
        fall(
            "aus",
            BESTELLUNG,
            {"confirmed": True, "items": [{"number": "13", "quantity": 1}]},
            sold_out=["48"],
        ),
        fall(
            "da",
            BESTELLUNG,
            {
                "confirmed": True,
                "items": [
                    {"number": "48", "quantity": 1},
                    {"number": "13", "quantity": 1},
                ],
            },
        ),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    assert {c.id: c.passed for c in report.cases} == {"aus": True, "da": True}


def test_ausverkauft_mit_unbekannter_nummer_stuerzt_ab(migrated_db_url, tmp_path):
    cases = write_cases(
        tmp_path / "c", fall("x", ["Hallo"], {"confirmed": False}, sold_out=["99"])
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    [result] = report.cases
    assert "sold_out" in result.error
    assert report.verdict == "durchgefallen"


def test_bekannte_luecke_im_report(migrated_db_url, tmp_path):
    cases = write_cases(
        tmp_path / "c",
        fall(
            "luecke",
            ["Hallo, liefern Sie auch? Die 13 bitte.", "0721 5551234"],
            {"escalated": False},
            pending="T-6.5: Lieferung",
        ),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    [result] = report.cases
    assert result.pending == "T-6.5: Lieferung" and not result.passed
    # Eine bekannte Luecke ist rot, aber keine Regression und kein Absturz.
    assert report.verdict == "bestanden"
    markdown = report.to_markdown()
    assert "## Bekannte Lücken" in markdown and "T-6.5: Lieferung (rot)" in markdown
    assert "## Rote Fälle" not in markdown


def test_ausverkauft_bis_ende_des_betriebstags():
    """Wie "Heute aus" im Tablet: bis 05:00 des Folgetags, nicht 24 Stunden."""
    abend = datetime.fromisoformat("2026-09-15T23:30:00+02:00")
    ende = runner._end_of_business_day(abend)
    assert ende == datetime.fromisoformat("2026-09-16T05:00:00+02:00")
    nacht = datetime.fromisoformat("2026-09-16T01:00:00+02:00")
    assert runner._end_of_business_day(nacht) == ende


def test_wiederholter_confirm_wird_wirklich_gesendet(migrated_db_url, tmp_path):
    """Codex PR #145: das Replay endet mit der Bestaetigung, ein zweites Ja des
    Gasts kaeme nie an. `repeat_confirm` schickt den letzten confirm des Modells
    selbst ein zweites Mal - und ohne confirm ist der Fall rot, nicht still gruen."""
    zeilen = [
        "Guten Tag, ich moechte etwas zum Abholen bestellen.",
        "Die 23.",
        "Nein, das wars.",
        "Auf den Namen Mueller.",
        "Meine Nummer ist 0721 5551234.",
        "Ja, passt so.",
    ]
    cases = write_cases(
        tmp_path / "c",
        fall("zweimal", zeilen, {"confirmed": True}, repeat_confirm=True),
        fall("ohne", zeilen[:2], {"confirmed": False}, repeat_confirm=True),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    result = {c.id: c for c in report.cases}
    assert result["zweimal"].passed, result["zweimal"].diffs
    assert not result["ohne"].passed
    assert any("nie confirm" in d for d in result["ohne"].diffs)


def test_doppelte_bestaetigung_wird_gezaehlt(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        tenant = runner._prepare(session, runner.DEFAULT_NOW, runner.menu_plan())
        call = SimCall(session, tenant, now=runner.DEFAULT_NOW)
        item = session.scalar(
            select(MenuItem.id).where(
                MenuItem.tenant_id == tenant.id, MenuItem.number == "23"
            )
        )
        draft = draft_order(
            session,
            DraftOrderRequest(
                call_id=call.call_id,
                tenant_id=tenant.id,
                idempotency_key="k1",
                type="pickup",
                customer={"name": "Mueller", "phone": "0721 5551234"},
                items=[{"menu_item_id": item, "quantity": 1}],
            ),
            now=runner.DEFAULT_NOW,
        )
        req = ConfirmRequest(
            call_id=call.call_id,
            tenant_id=tenant.id,
            entity="order",
            entity_id=draft.order_id,
            idempotency_key="c1",
        )
        confirm(session, req)
        confirm(session, req)
        assert observe(session, call.call_id, confirms=2).duplicate_confirms == 0
        # Ein nicht idempotentes confirm schriebe einen zweiten Eintrag.
        session.add(
            AuditLog(
                tenant_id=tenant.id,
                actor="agent",
                action="order.confirmed",
                entity="order",
                entity_id=draft.order_id,
                payload={},
            )
        )
        session.commit()
        seen = observe(session, call.call_id, confirms=2)
        assert seen.duplicate_confirms == 1
        assert any("doppelt" in d for d in judge({}, seen))
    engine.dispose()


def test_hinweis_und_tools_aus_dem_fall(migrated_db_url, tmp_path):
    """Codex PR #145: der Allergiehinweis zaehlt nur, wenn er gespeichert ist, und
    eine Allergiefrage nur, wenn das Modell get_item_details gerufen hat."""
    zeilen = [
        "Guten Tag, ich moechte etwas zum Abholen bestellen.",
        "Die 13, ich habe eine Erdnussallergie.",
        "Nein, das wars.",
        "Auf den Namen Mueller.",
        "Meine Nummer ist 0721 5551234.",
        "Ja, passt so.",
    ]
    hinweis = "WICHTIG: Keine Erdnuss. Grund: Allergie"
    cases = write_cases(
        tmp_path / "c",
        fall(
            "hinweis",
            zeilen,
            {"items": [{"number": "13", "quantity": 1, "note": hinweis}]},
        ),
        fall(
            "falscher_hinweis",
            zeilen,
            {
                "items": [
                    {"number": "13", "quantity": 1, "note": "WICHTIG: Keine Karotte."}
                ]
            },
        ),
        fall("ohne_tool", zeilen, {"confirmed": True, "tools": ["get_item_details"]}),
    )
    report = run(migrated_db_url, cases, tmp_path / "r")
    result = {c.id: c for c in report.cases}
    assert result["hinweis"].passed, result["hinweis"].diffs
    assert not result["falscher_hinweis"].passed
    assert result["ohne_tool"].diffs == [
        "tools: kein erfolgreicher Aufruf ['get_item_details']"
    ]


def test_nur_erfolgreicher_aufruf_fuer_das_richtige_gericht_zaehlt():
    """Codex PR #145: ein gescheiterter get_item_details oder einer fuer ein
    anderes Gericht beantwortet keine Allergiefrage."""
    from evals.judge import missing_tools

    want = [{"tool": "get_item_details", "number": "23"}]
    assert missing_tools(want, []) == want
    assert missing_tools(want, [("get_item_details", {"number": "24"})]) == want
    assert missing_tools(want, [("search_menu", {"number": "23"})]) == want
    assert missing_tools(want, [("get_item_details", {"number": "23"})]) == []
    assert missing_tools(["get_item_details"], [("get_item_details", {})]) == []


def test_recorder_merkt_nur_erfolgreiche_ergebnisse():
    llm = RecordingLLM(FakeLLM([LLMTurn(say="a"), LLMTurn(say="b")]))
    llm.next_turn("", {}, json.dumps({"tool": "get_item_details", "ok": False}))
    llm.next_turn(
        "",
        {},
        json.dumps({"tool": "get_item_details", "ok": True, "data": {"number": "23"}}),
    )
    assert llm.recording.ok_results == [("get_item_details", {"number": "23"})]


# --- Angebotene Alternativen (expected.alternatives) ---------------------------


def test_alternativen_des_letzten_abgelehnten_check_slot_in_ortszeit():
    results = [
        ("check_slot", {"available": False, "alternatives": ["2026-09-20T19:30:00Z"]}),
        ("search_menu", {"items": []}),
        (
            "check_slot",
            {
                "available": False,
                "alternatives": ["2026-09-22T17:00:00Z", "2026-09-22T17:30:00Z"],
            },
        ),
    ]
    assert offered_alternatives(results, "Europe/Berlin") == [
        "2026-09-22T19:00",
        "2026-09-22T19:30",
    ]


def test_spaeteres_freies_check_slot_ueberdeckt_das_angebot_nicht():
    """Review PR #152: Gast nimmt die Alternative, das Modell prueft sie nach."""
    results = [
        (
            "check_slot",
            {
                "available": False,
                "alternatives": ["2026-09-22T17:00:00Z", "2026-09-22T17:30:00Z"],
            },
        ),
        ("check_slot", {"available": True, "alternatives": []}),
    ]
    assert offered_alternatives(results, "Europe/Berlin") == [
        "2026-09-22T19:00",
        "2026-09-22T19:30",
    ]


def test_ohne_abgelehnten_wunsch_ist_das_angebot_none_nicht_leer():
    """Review PR #152: "nichts angeboten" und "Wunsch war frei" sind verschieden,
    ein Fall mit alternatives: [] darf nicht gruen werden, nur weil frei war."""
    assert offered_alternatives([], "Europe/Berlin") is None
    frei = [("check_slot", {"available": True, "alternatives": []})]
    assert offered_alternatives(frei, "Europe/Berlin") is None
    voll = [("check_slot", {"available": False, "alternatives": []})]
    assert offered_alternatives(voll, "Europe/Berlin") == []
