"""Replay one call and wait after every turn, so the host can look at the tablet.

    python -u -m scripts.demo.paced_replay scripts/demo/pickup_call.json

The same `sim.replay.replay` and `sim.session.render_turn` as `python -m sim.replay`,
with the real clock and the default scripted stand-in model. After each turn it prints
the marker line and blocks until the host writes one line to stdin. Run by
`scripts/readme_demo.py` through `docker compose exec`.
"""

import sys
from pathlib import Path

from api.db import SessionLocal
from sim.replay import load_case, replay
from sim.session import Turn, render_turn, resolve_tenant

MARKER = "@@END-OF-TURN"


def main(argv: list[str]) -> int:
    case = load_case(Path(argv[0]))

    def on_turn(turn: Turn) -> None:
        print(render_turn(turn))
        print(MARKER, flush=True)
        sys.stdin.readline()

    with SessionLocal() as session:
        tenant = resolve_tenant(session, None)
        print(f"--- {case.get('id', 'ohne-id')}: {case.get('name', '')}".rstrip())
        call, _ = replay(session, case, tenant, on_turn=on_turn)
        ended = call.finish()
        print(f"Anruf beendet: {ended.outcome}, {ended.duration_seconds} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
