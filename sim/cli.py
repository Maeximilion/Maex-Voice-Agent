"""Gespräch im Terminal: du tippst den Kunden, der Agent antwortet (docs/11 §sim).

    python -m sim.cli
    python -m sim.cli --noise 0.2 --seed 7
    python -m sim.cli --now 2026-09-15T18:00+02:00

Schreibt in dieselbe Datenbank wie die API: eine bestätigte Reservierung steht
danach wirklich in `reservations` und taucht später in der GUI auf.
"""

import argparse
import random
import sys
from datetime import datetime

from api.core.errors import AppError
from api.db import SessionLocal
from sim.noise import noisy_text
from sim.session import SimCall, render_turn, resolve_tenant

QUIT_COMMANDS = ("quit", "ende", "q")
PROMPT = "> "


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Text-Telefon: Gespräch im Terminal")
    parser.add_argument("--tenant", help="Name des Mandanten, sonst der erste")
    parser.add_argument(
        "--noise",
        type=float,
        default=0.0,
        help="Anteil absichtlich verrauschter Wörter, 0.0 bis 1.0",
    )
    parser.add_argument("--seed", type=int, help="Saat für das Rauschen")
    parser.add_argument("--caller", help="Rufnummer des Anrufers")
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        help="Zeitpunkt des Anrufs mit Zeitzone, z. B. 2026-09-15T18:00+02:00",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rng = random.Random(args.seed)
    with SessionLocal() as session:
        try:
            tenant = resolve_tenant(session, args.tenant)
        except AppError as exc:
            print(exc.message, file=sys.stderr)
            return 2
        call = SimCall(session, tenant, now=args.now, caller_id=args.caller)
        print(f"Anruf {call.call_id} bei {tenant.name}. Beenden mit :quit")
        _converse(call, noise=args.noise, rng=rng)
        ended = call.finish()
        print(f"Anruf beendet: {ended.outcome}, {ended.duration_seconds} s")
    return 0


def _converse(call: SimCall, *, noise: float, rng: random.Random) -> None:
    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            # Auflegen mitten im Satz ist der Normalfall am Telefon, kein Fehler.
            print()
            return
        if not line:
            continue
        if line.lstrip(":").lower() in QUIT_COMMANDS:
            return
        turn = call.say(noisy_text(line, noise, rng))
        print(render_turn(turn))
        if turn.ended:
            return


if __name__ == "__main__":
    raise SystemExit(main())
