"""Ein Transkript aus `evals/cases/` abspielen (docs/11 §sim, Format docs/08 §1).

    python -m sim.replay evals/cases/reservierung_0001_tisch_fuer_vier.json
    python -m sim.replay evals/cases/*.json --noise 0.2 --seed 7

Bewertet **nicht**: der Abgleich mit `expected` und die Metriken sind Sache des
Eval-Runners (T-5.1, docs/08 §3). Hier geht es darum, denselben Anruf beliebig oft
identisch zu fahren und zu sehen, was dabei in der Datenbank landet.
"""

import argparse
import json
import random
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import AppError
from api.db import SessionLocal
from api.models import Reservation, Tenant
from sim.noise import noisy_text
from sim.session import SimCall, Turn, render_turn, resolve_tenant

CUSTOMER = "customer"


def load_case(path: Path) -> dict[str, Any]:
    case = json.loads(path.read_text(encoding="utf-8"))
    if not customer_lines(case):
        raise ValueError(f"{path}: kein Kundensatz im Transkript")
    return case


def customer_lines(case: dict[str, Any]) -> list[str]:
    """Nur die Kundenzüge: was der Agent damals gesagt hat, ist für den Lauf
    bedeutungslos - er sagt es jetzt neu."""
    return [
        entry["text"]
        for entry in case.get("transcript", [])
        if entry.get("role") == CUSTOMER and entry.get("text")
    ]


def replay(
    session: Session,
    case: dict[str, Any],
    tenant: Tenant,
    *,
    noise: float = 0.0,
    rng: random.Random | None = None,
    now: datetime | None = None,
    on_turn: Callable[[Turn], None] | None = None,
) -> tuple[SimCall, list[Turn]]:
    rng = rng or random.Random()
    call = SimCall(
        session,
        tenant,
        now=now,
        external_session_id=f"replay-{case.get('id', 'ohne-id')}-{rng.random():.6f}",
        # Rufnummernerkennung: ohne das Feld ist die Nummer unterdrueckt.
        caller_id=case.get("caller_id"),
    )
    turns = []
    for line in customer_lines(case):
        turn = call.say(noisy_text(line, noise, rng))
        turns.append(turn)
        if on_turn:
            on_turn(turn)
        if turn.ended:
            break
    return call, turns


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transkript abspielen")
    parser.add_argument("cases", nargs="+", type=Path, help="Falldateien (JSON)")
    parser.add_argument("--tenant", help="Name des Mandanten, sonst der erste")
    parser.add_argument("--noise", type=float, default=0.0)
    parser.add_argument("--seed", type=int, help="Saat für das Rauschen")
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        help="Zeitpunkt des Anrufs mit Zeitzone, z. B. 2026-09-15T18:00+02:00",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with SessionLocal() as session:
        try:
            tenant = resolve_tenant(session, args.tenant)
        except AppError as exc:
            print(exc.message, file=sys.stderr)
            return 2
        for path in args.cases:
            try:
                case = load_case(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Fall nicht lesbar: {exc}", file=sys.stderr)
                return 2
            _run_one(session, case, tenant, args)
    return 0


def _run_one(
    session: Session, case: dict[str, Any], tenant: Tenant, args: argparse.Namespace
) -> None:
    print(f"--- {case.get('id', 'ohne-id')}: {case.get('name', '')}".rstrip())
    call, _ = replay(
        session,
        case,
        tenant,
        noise=args.noise,
        rng=random.Random(args.seed),
        now=args.now,
        on_turn=lambda turn: print(render_turn(turn)),
    )
    ended = call.finish()
    print(f"Anruf beendet: {ended.outcome}, {ended.duration_seconds} s")
    print(f"  Datenbank: {_db_summary(session, call)}")


def _db_summary(session: Session, call: SimCall) -> str:
    """Gelesen wird der Datenbankzustand, nicht der Modelltext (docs/08 §3)."""
    rows = session.execute(
        select(Reservation.id, Reservation.status).where(
            Reservation.call_id == call.call_id
        )
    ).all()
    if not rows:
        return "keine Reservierung"
    return ", ".join(f"Reservierung {row.id} {row.status}" for row in rows)


if __name__ == "__main__":
    raise SystemExit(main())
