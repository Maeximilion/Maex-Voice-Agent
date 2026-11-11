"""Display of the call list (T-8.7, docs/06 §4): German words and numbers.

The rows come from `domain/calls/log.py`; this file only formats them and
reads the three filters of the page.
"""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from api.core.time import business_day, to_local
from api.domain.calls.log import list_calls
from api.gui.figures_view import DASH, WEEKDAYS
from api.models import Tenant

# The days the page offers: the caller id is kept far longer (docs/03), a
# list of the last two weeks is what the team looks back at.
DAYS = 14
OUTCOMES = {
    "completed": "abgeschlossen",
    "transferred": "an Team übergeben",
    "callback": "Rückruf angelegt",
    "abandoned": "abgebrochen",
    "error": "Fehler",
}
RUNNING = "läuft"
INTENTS = {
    "reservation": "Reservierung",
    "pickup": "Abholung",
    "delivery": "Lieferung",
    "info": "Auskunft",
    "complaint": "Beschwerde",
    "unknown": "unklar",
}
# What was wrong with the line (`calls.line_note`), in the team's words.
NOTES = {"no_caller_audio": "Anrufer nicht hörbar (kein Ton angekommen)"}
WITHHELD = "unterdrückt"
# Shown of a number, as in the mockup: enough to tell callers apart, not the
# whole number on a screen that stays open in the restaurant.
NUMBER_SHOWN = 8


def _number(caller_id: str | None) -> str:
    if not caller_id:
        return WITHHELD
    if len(caller_id) <= NUMBER_SHOWN:
        return caller_id
    return f"{caller_id[:NUMBER_SHOWN]}\N{HORIZONTAL ELLIPSIS}"


def _duration(seconds: int | None) -> str:
    return DASH if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


def _euro(cents: int | None) -> str:
    return DASH if cents is None else f"{cents // 100},{cents % 100:02d} €"


def page(
    session: Session,
    tenant: Tenant,
    day: str | None = None,
    outcome: str | None = None,
    intent: str | None = None,
) -> dict:
    """The page's context. A filter value the page did not offer is ignored."""
    today = business_day(tz_name=tenant.timezone)
    days = [today - timedelta(days=back) for back in range(DAYS)]
    chosen = next((d for d in days if d.isoformat() == day), today)
    outcome = outcome if outcome in OUTCOMES else None
    intent = intent if intent in INTENTS else None
    rows = [
        {
            "time": f"{to_local(row.started_at, tenant.timezone):%H:%M}",
            "number": _number(row.caller_id),
            "intent": INTENTS.get(row.intent or "", DASH),
            "outcome": OUTCOMES.get(row.outcome or "", RUNNING),
            "duration": _duration(row.duration_seconds),
            "cost": _euro(row.cost_cents),
            "tools": row.tool_calls,
            "note": NOTES.get(row.line_note or ""),
        }
        for row in list_calls(session, tenant, chosen, outcome=outcome, intent=intent)
    ]
    return {
        "rows": rows,
        "count": f"{len(rows)} {'Anruf' if len(rows) == 1 else 'Anrufe'} "
        f"{'heute' if chosen == today else _day(chosen)}",
        "days": [
            (d.isoformat(), "Heute" if d == today else _day(d), d == chosen)
            for d in days
        ],
        "outcomes": [(key, word, key == outcome) for key, word in OUTCOMES.items()],
        "intents": [(key, word, key == intent) for key, word in INTENTS.items()],
    }


def _day(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m.}"
