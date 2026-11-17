"""Display of the call list (T-8.7, docs/06 §4): German words and numbers.

The rows come from `domain/calls/log.py`; this file only formats them and
reads the three filters of the page.
"""

from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from api.core.time import business_day, to_local
from api.domain.calls.log import DAYS_LISTED, SupportCallLog, list_calls
from api.gui.figures_view import DASH, WEEKDAYS
from api.models import Tenant

# The days the page offers: what the team looks back at.
DAYS = DAYS_LISTED
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
# Why a call went to the team (`calls.transfer_reason`, the reason of a
# callback), in the team's words, never the code (docs/06 section 1 rule 2).
REASONS = {
    "complaint": "Beschwerde",
    "not_understood": "Nicht verstanden",
    "human_requested": "Will Mitarbeiter sprechen",
    "out_of_scope": "Anderes Anliegen",
}
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


def duration(seconds: int | None) -> str:
    return DASH if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


def euro(cents: int | None) -> str:
    return DASH if cents is None else f"{cents // 100},{cents % 100:02d} €"


def page(
    session: Session,
    tenant: Tenant,
    day: str | None = None,
    outcome: str | None = None,
    intent: str | None = None,
) -> dict[str, Any]:
    """The page's context. A filter value the page did not offer is ignored."""
    today = business_day(tz_name=tenant.timezone)
    days = [today - timedelta(days=back) for back in range(DAYS)]
    chosen = next((d for d in days if d.isoformat() == day), today)
    outcome = outcome if outcome in OUTCOMES else None
    intent = intent if intent in INTENTS else None
    rows = [
        {
            "id": row.id,
            "time": f"{to_local(row.started_at, tenant.timezone):%H:%M}",
            "number": _number(row.caller_id),
            "intent": INTENTS.get(row.intent or "", DASH),
            "outcome": OUTCOMES.get(row.outcome or "", RUNNING),
            "duration": duration(row.duration_seconds),
            "cost": euro(row.cost_cents),
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


def detail(log: SupportCallLog, tenant: Tenant) -> dict[str, Any]:
    """One call for the row detail (#571): what the row says, when it ended, why
    it went to the team, and its tool calls in their order with how long each
    took. No number: the row above it shows what the list shows. No transcript
    is stored, and the page says so."""
    started = to_local(log.started_at, tenant.timezone)
    ended = to_local(log.ended_at, tenant.timezone) if log.ended_at else None
    facts = [
        ("Beginn", f"{started:%d.%m.%Y %H:%M:%S}"),
        ("Ende", f"{ended:%H:%M:%S}" if ended else RUNNING),
        ("Dauer", duration(log.duration_seconds)),
        ("Anliegen", INTENTS.get(log.intent or "", DASH)),
        ("Ergebnis", OUTCOMES.get(log.outcome or "", RUNNING)),
        ("Kosten", euro(log.cost_cents)),
    ]
    if log.transfer_reason:
        reason = REASONS.get(log.transfer_reason, log.transfer_reason[:64])
        facts.append(("An das Team wegen", reason))
    if log.line_note:
        facts.append(("Leitung", NOTES.get(log.line_note, log.line_note[:64])))
    tools = [
        {
            "name": tool.name,
            "duration": DASH
            if tool.duration_ms is None
            else f"{round(tool.duration_ms)} ms",
            "ok": tool.ok,
            "error": tool.error_code,
        }
        for tool in log.tool_calls
    ]
    return {
        "facts": facts,
        "tools": tools,
        "tools_left": log.tool_calls_total - len(tools),
    }
