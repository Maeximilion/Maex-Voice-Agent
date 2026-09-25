#!/usr/bin/env python3
"""Eval-Runner: jeder Fall aus `evals/cases/` als Anruf, geprüft am Datenbankzustand (docs/08 §3).

Für jeden Fall:
  1. frischer Anruf im Gesprächskern (sim/replay.py, wie das Text-Telefon)
  2. Kundensätze der Reihe nach einspielen
  3. Tool-Aufrufe gegen die echte Fachlogik auf einer Wegwerf-Datenbank
  4. Endzustand aus der Datenbank lesen, nicht aus dem Modelltext (evals/judge.py)
  5. mit `expected` vergleichen, harte Metriken mitschreiben (evals/recorder.py)
Danach Report als JSON und Markdown in `evals/reports/` (evals/report.py).

Aufruf:

    make eval                          # alle Fälle, im API-Container
    make eval TAGS=abholung,noise      # nur Fälle mit einem dieser Tags
    python -m evals.runner --model scripted --keep-db

Exit-Code: 0 bestanden · 1 durchgefallen (harte Metrik verletzt oder Genauigkeit
unter dem letzten Lauf) · 2 Fall oder Aufruf kaputt.

Die Datenbank ist eine eigene, frisch migrierte je Lauf (evals/scratch_db.py),
mit einem eigenen Mandanten je Fall und der Evalkarte aus `evals/menu/`
(Testdaten nach docs/14).
Entwicklungs- und Betriebsdaten berührt der Lauf nie.

Abhängigkeiten: nur das Projekt selbst (Datenbank aus DATABASE_URL).
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.llm import LLMClient
from api.domain.menu.importer import apply, parse
from evals.judge import CaseError, judge, observe, validate_case
from evals.recorder import RecordingLLM
from evals.report import CaseResult, RunReport, previous_run, write
from evals.scratch_db import create_scratch_db, drop_scratch_db, migrate
from scripts.import_menu import read_files
from scripts.seed import seed
from sim.replay import customer_lines, replay
from sim.scripted_llm import ScriptedLLM
from sim.session import resolve_tenant

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
MENU = HERE / "menu"
REPORTS = HERE / "reports"
TENANT = "Evalbetrieb"
TIMEZONE = "Europe/Berlin"
# Ein Dienstag im Abendfenster: Abholung und Reservierung offen. Ein Fall kann
# mit "now" einen eigenen Zeitpunkt setzen (Schliesszeit, Tageswechsel).
DEFAULT_NOW = datetime.fromisoformat("2026-09-15T18:00:00+02:00")
MODELS = ("scripted",)


class UsageError(ValueError):
    """Aufruf nicht ausführbar, etwa ein Modell, das es noch nicht gibt."""


def load_cases(folder: Path, tags: list[str]) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(folder.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CaseError(f"{path.name}: nicht lesbar ({exc})") from exc
        validate_case(case, path.name)
        if not customer_lines(case):
            raise CaseError(f"{path.name}: kein Kundensatz im Transkript")
        cases.append(case)
    # Ueber alle Faelle, vor dem Tag-Filter: zwei Dateien mit derselben id und
    # verschiedenen Tags waeren sonst je nach Filter mal der eine, mal der
    # andere Fall - und die Regressionsregel verglich Aepfel mit Birnen.
    ids = [c["id"] for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CaseError(f"doppelte Fall-id: {duplicates}")
    return [c for c in cases if not tags or set(tags) & set(c.get("tags", []))]


def model_factory(model: str) -> Callable[[datetime], LLMClient]:
    if model == "scripted":
        return lambda now: ScriptedLLM(now=now, timezone=TIMEZONE)
    # Ein echtes Modell kommt mit T-2.4 (agent/llm.py); bis dahin kein stilles
    # Zurückfallen auf das Skript, sonst verglich ein Modellvergleich das Skript
    # mit sich selbst.
    raise UsageError(f"Modell '{model}' gibt es noch nicht (kommt mit T-2.4)")


def _prepare(session: Session, now: datetime, name: str = TENANT):
    """Mandant mit Öffnungszeiten, Kapazität und Evalkarte.

    Je Fall ein eigener: sonst füllen frühere Fälle die Kapazität eines Slots,
    zählen den Abholcode hoch oder lassen Rückrufe offen, und ob ein Fall grün
    ist, hinge von Reihenfolge und Tag-Filter ab (docs/08 §3 Schritt 1).
    """
    seed(session, tenant_name=name, timezone=TIMEZONE)
    tenant = resolve_tenant(session, name)
    plan = parse(read_files(MENU))
    if not plan.ok:
        raise CaseError(f"Evalkarte evals/menu/ ungültig: {plan.errors}")
    apply(session, tenant.id, plan, now=now)
    return tenant


def run_case(session: Session, case: dict[str, Any], make_llm) -> CaseResult:
    now = datetime.fromisoformat(case["now"]) if case.get("now") else DEFAULT_NOW
    expected = case["expected"]
    result = CaseResult(
        id=case["id"],
        name=case.get("name", ""),
        tags=case.get("tags", []),
        passed=False,
        expected_escalation=bool(expected.get("escalated")),
    )
    llm = RecordingLLM(make_llm(now))
    try:
        tenant = _prepare(session, DEFAULT_NOW, name=f"{TENANT} {case['id']}")
        call, turns = replay(session, case, tenant, now=now, llm=llm)
        call.finish()
        seen = observe(session, call.call_id, llm.recording.confirms)
        diffs = judge(expected, seen)
    except Exception as exc:  # noqa: BLE001 - ein abgestuerzter Fall ist ein roter Fall, kein Abbruch
        session.rollback()
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    rec = llm.recording
    result.turns = len(turns)
    result.diffs = diffs
    result.guessed_items = len(rec.guessed)
    result.unconfirmed = len(rec.unconfirmed) + seen.confirmed_without_confirm
    result.missed_escalation = result.expected_escalation and not seen.escalated
    result.false_escalation = not result.expected_escalation and seen.escalated
    if rec.guessed:
        result.diffs.append(f"geratene Position ohne Suchtreffer: {rec.guessed}")
    if rec.unconfirmed:
        result.diffs.append(f"confirm ohne Ja davor: {rec.unconfirmed}")
    if seen.confirmed_without_confirm:
        result.diffs.append("bestätigt ohne confirm des Modells")
    result.passed = not result.diffs
    return result


def run(
    *,
    cases_dir: Path = CASES,
    tags: list[str] | None = None,
    model: str = "scripted",
    report_dir: Path = REPORTS,
    db_url: str | None = None,
    keep_db: bool = False,
    stamp: datetime | None = None,
) -> RunReport:
    """Ein ganzer Lauf. `db_url` setzt eine migrierte Datenbank von aussen (Tests)."""
    tags = sorted(tags or [])
    make_llm = model_factory(model)
    cases = load_cases(cases_dir, tags)
    if not cases:
        raise CaseError(f"kein Fall mit den Tags {tags} in {cases_dir}")
    stamp = stamp or datetime.now(UTC)

    own_db = db_url is None
    if own_db:
        db_url = create_scratch_db(prefix="maex_eval")
    engine = create_engine(db_url)
    try:
        if own_db:
            migrate(db_url)
        with Session(engine) as session:
            # Die Karte einmal vorab prüfen: ist sie kaputt, bricht der Lauf ab,
            # statt jeden Fall mit demselben Fehler rot zu zeigen.
            if not parse(read_files(MENU)).ok:
                raise CaseError("Evalkarte evals/menu/ ungültig")
            results = [run_case(session, c, make_llm) for c in cases]
    finally:
        engine.dispose()
        if own_db and not keep_db:
            drop_scratch_db(db_url)

    report = RunReport(
        run_at=stamp.isoformat(timespec="seconds"),
        model=model,
        tags=tags,
        cases=results,
        previous=previous_run(report_dir, model, tags),
    )
    report.decide()
    write(report, report_dir, stamp)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Eval-Suite laufen lassen (docs/08)")
    parser.add_argument("--tags", default="", help="Komma-Liste, z. B. abholung,noise")
    parser.add_argument("--model", default="scripted", help=f"eines von {MODELS}")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--report-dir", type=Path, default=REPORTS)
    parser.add_argument(
        "--keep-db", action="store_true", help="Wegwerf-Datenbank nicht löschen"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    try:
        report = run(
            cases_dir=args.cases,
            tags=tags,
            model=args.model,
            report_dir=args.report_dir,
            keep_db=args.keep_db,
        )
    except (CaseError, UsageError) as exc:
        print(f"Eval nicht gestartet: {exc}", file=sys.stderr)
        return 2
    print(report.to_markdown())
    return 0 if report.verdict == "bestanden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
