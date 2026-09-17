"""Regelbasierter Ersatz für das Modell, bis T-2.4 ein echtes anbindet.

`api/agent/llm.py` hält die Schnittstelle (`LLMClient`) und ein Testdouble für die
Loop-Unit-Tests bereit; das hier ist der skriptfähige Fake für `sim/`, den sein
Docstring ankündigt. Er versteht genau den Ablauf aus `prompts/system_v1.md`:
Status abfragen, Reservierungsangaben einsammeln, `check_slot`, `create_reservation`,
vorlesen, auf ein Ja hin `confirm`.

Er ist ausdrücklich **kein** Sprachmodell: er rät nichts, er erkennt Muster. Wo er
nichts erkennt, meldet er einen Fehlversuch (`understanding_failure`) und überlässt
die Leiter `agent/ladder.py`. Fachdaten (Öffnung, Kapazität, Vorlesesatz) kommen
auch hier ausschließlich aus den Tool-Ergebnissen, nie aus diesem Modul
(CLAUDE.md §2 Regel 1).
"""

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from api.agent.llm import LLMTurn, ToolCall

# Reihenfolge, in der gefragt wird. Entspricht den Pflichtfeldern von
# CreateReservationRequest (api/schemas/reservations.py).
SLOT_ORDER = ("party_size", "reserved_for", "guest_name", "phone")

QUESTIONS = {
    "party_size": "Für wie viele Personen darf ich reservieren?",
    "reserved_for": "An welchem Tag und um wie viel Uhr möchten Sie kommen?",
    "guest_name": "Auf welchen Namen darf ich den Tisch notieren?",
    "phone": "Unter welcher Telefonnummer sind Sie erreichbar?",
}

GREETING = (
    "Guten Tag, hier ist der KI-Assistent des Restaurants. "
    "Ich kann einen Tisch für Sie reservieren."
)
SAY_CONFIRMED = "Ist notiert. Vielen Dank für den Anruf und bis dann."
SAY_ASK_AGAIN = "Was soll ich ändern?"
SAY_HANDOVER = "Ich gebe an das Team weiter."

# Anliegen, die Version 1 nicht kann (prompts/system_v1.md §Harte Regeln).
OUT_OF_SCOPE = ("speisekarte", "abhol", "liefer", "allergi", "karte")
SUMMARY_OUT_OF_SCOPE = "Anliegen außerhalb von Version 1."

YES = ("ja", "genau", "passt", "richtig", "stimmt", "gerne", "jawohl", "okay", "ok")
NO = ("nein", "nicht", "falsch", "doch nicht", "anders")

# Zahlwörter bis zwölf, wie der Vorlesesatz sie ausgibt (domain/reservations/spoken.py).
# T-0.8 bringt die vollständige Umwandlung als eigene Funktion; bis dahin reicht hier
# die kleine Tabelle, weil eine Personenzahl darüber am Telefon ohnehin selten ist.
NUMBER_WORDS = {
    "ein": 1,
    "eine": 1,
    "einen": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    # Im Terminal tippt man Umlaute oft als Ersatzschreibung; am Telefon liefert die
    # Erkennung den Umlaut. Beide Formen gelten.
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "zwoelf": 12,
}
WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}

_NUMBER = r"\d{1,2}|" + "|".join(NUMBER_WORDS)
_PARTY = re.compile(
    rf"(?:für\s+)?({_NUMBER})\s*(?:personen|person|leute|gäste|gast|mann)\b",
    re.IGNORECASE,
)
_PARTY_TABLE = re.compile(rf"tisch\s+für\s+({_NUMBER})\b", re.IGNORECASE)
# Gross- und Kleinschreibung nur fuer die Einleitung ignorieren: der Name selbst
# muss gross anfangen, sonst wuerde "auf morgen" den Namen "morgen" ergeben.
_NAME = re.compile(
    r"(?i:auf den namen|mein name ist|name ist|ich heiße|ich heisse|auf)\s+"
    r"([A-ZÄÖÜ][^\s,.;!?]{1,40})"
)
# Mindestens sieben Ziffern, damit Uhrzeit ("19:30") und Datum ("22.09.") nicht als
# Rufnummer durchgehen. Der Punkt fehlt deshalb bewusst in der Zeichenklasse.
_PHONE = re.compile(r"(\+?\d[\d\s/()-]{5,})")
_TIME_COLON = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME_DOT = re.compile(r"\b(\d{1,2})\.(\d{2})\s*uhr\b", re.IGNORECASE)
_TIME_HOUR = re.compile(r"\b(\d{1,2})\s*uhr(?:\s+(\d{1,2}))?\b", re.IGNORECASE)
_TIME_HALF = re.compile(rf"\bhalb\s+({_NUMBER})\b", re.IGNORECASE)
_DATE = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.(\d{4})?")


class ScriptedLLM:
    """Ein Objekt je Anruf: `sim/` baut es zusammen mit dem Gesprächszustand auf.

    `now` und `timezone` kommen von außen, damit ein Replay denselben Satz zweimal
    gleich versteht ("morgen um 19 Uhr" ist sonst vom Kalender des Rechners abhängig).
    """

    def __init__(self, *, now: datetime, timezone: str):
        self._now = now
        self._zone = ZoneInfo(timezone)
        self._status_checked = False
        self._greeted = False
        # Das zuletzt gehoerte Anliegen ausserhalb von Version 1, bis der Rueckruf
        # steht (Codex-Review PR #104, P1).
        self._out_of_scope_request: str | None = None

    def next_turn(
        self, system_prompt: str, state_json: dict[str, Any], input_text: str
    ) -> LLMTurn:
        result = _as_tool_result(input_text)
        if result is not None:
            return self._after_tool(state_json, result)
        return self._after_customer(state_json, input_text)

    # -- Kundenzug ---------------------------------------------------------

    def _after_customer(self, state: dict[str, Any], text: str) -> LLMTurn:
        patch = self._extract(text, state.get("slots", {}))
        slots = {**state.get("slots", {}), **patch}

        if state.get("stage") == "readback_pending":
            return self._after_readback(state, text, slots, patch)

        if _mentions_stem(text, OUT_OF_SCOPE) or self._out_of_scope_request:
            # Vor der Statusabfrage, sonst verschluckt der erste Zug den Sonderfall.
            # Und gemerkt, bis der Rückruf steht: der nächste Zug enthält oft nur noch
            # die Rufnummer und träfe kein Stichwort mehr (Codex-Review PR #104, P1).
            self._out_of_scope_request = self._out_of_scope_request or text
            return self._out_of_scope(slots, patch)

        if not self._status_checked:
            # Öffnung und Erreichbarkeit kennt nur die Datenbank (CLAUDE.md §2 Regel 1),
            # deshalb steht der Status vor jeder Zusage am Anfang des Gesprächs.
            self._status_checked = True
            return LLMTurn(
                tool_call=ToolCall("get_service_status"), state_patch=patch or None
            )

        return self._next_step(slots, patch=patch, understood=bool(patch))

    def _after_readback(
        self,
        state: dict[str, Any],
        text: str,
        slots: dict[str, Any],
        patch: dict[str, Any],
    ) -> LLMTurn:
        """Vom Entwurf zur Buchung führt nur ein klares Ja (CLAUDE.md §2 Regel 3)."""
        reservation_id = state.get("reservation_id")
        zustimmung = _mentions_word(text, YES) and not _mentions_word(text, NO)
        if zustimmung and reservation_id:
            return LLMTurn(
                tool_call=ToolCall(
                    "confirm", {"entity": "reservation", "entity_id": reservation_id}
                ),
                state_patch=patch or None,
            )
        if patch:
            # Korrektur beim Vorlesen ("Nein, wir sind fünf"): der bestehende Entwurf
            # trägt noch die alte Zahl, und ein späteres Ja bestätigte genau die
            # (Codex-Review PR #104, P1). Deshalb ein neuer Entwurf mit neuem
            # Vorlesesatz; der alte bleibt als Entwurf liegen und wird nie bestätigt.
            return self._next_step(slots, patch=patch)
        return LLMTurn(say=SAY_ASK_AGAIN, state_patch=patch or None)

    def _out_of_scope(self, slots: dict[str, Any], patch: dict[str, Any]) -> LLMTurn:
        phone = slots.get("phone")
        if not phone:
            return LLMTurn(say=QUESTIONS["phone"], state_patch=patch or None)
        return LLMTurn(
            tool_call=ToolCall(
                "create_callback",
                {
                    "phone": phone,
                    "reason": "out_of_scope",
                    "summary": (
                        f"{SUMMARY_OUT_OF_SCOPE} "
                        f"Kunde sagte: {self._out_of_scope_request}"
                    ),
                },
            ),
            state_patch=patch or None,
        )

    # -- Tool-Ergebnis -----------------------------------------------------

    def _after_tool(self, state: dict[str, Any], result: dict[str, Any]) -> LLMTurn:
        name = result.get("tool")
        data = result.get("data") or {}
        say = result.get("say")
        slots = state.get("slots", {})

        if not result.get("ok"):
            return self._after_failure(name, say)
        if name == "create_callback":
            self._out_of_scope_request = None
            return LLMTurn(say=say or SAY_HANDOVER)
        if name == "get_service_status":
            if self._out_of_scope_request:
                return self._out_of_scope(slots, {})
            return self._next_step(slots, prefix=self._greeting_once(), extra=say)
        if name == "check_slot":
            if data.get("available"):
                return LLMTurn(tool_call=ToolCall("create_reservation", _draft(slots)))
            # Alternativen stehen im vorgeschriebenen Satz aus dem Code (docs/05 §5).
            return LLMTurn(say=say or "Zu der Zeit ist leider nichts frei.")
        if name == "create_reservation":
            return LLMTurn(say=data["readback"])
        if name == "confirm":
            return LLMTurn(say=SAY_CONFIRMED)
        return LLMTurn(say=say or SAY_HANDOVER)

    def _after_failure(self, name: str | None, say: str | None) -> LLMTurn:
        """Ein Fehlschlag mit vorgeschriebenem Satz geht an den Kunden zurück, der
        dann etwas anderes vorschlagen kann. Ohne Satz bleibt nur die echte Übergabe
        an das Team, nie eine erfundene Auskunft (CLAUDE.md §2 Regeln 2 und 5)."""
        if say:
            return LLMTurn(say=say)
        if name == "transfer_to_team":
            return LLMTurn(say=SAY_HANDOVER)
        return LLMTurn(
            tool_call=ToolCall("transfer_to_team", {"reason": "not_understood"})
        )

    # -- gemeinsamer Fortschritt ------------------------------------------

    def _next_step(
        self,
        slots: dict[str, Any],
        *,
        patch: dict[str, Any] | None = None,
        prefix: str | None = None,
        extra: str | None = None,
        understood: bool = True,
    ) -> LLMTurn:
        missing = next((f for f in SLOT_ORDER if not slots.get(f)), None)
        if missing:
            say = " ".join(p for p in (prefix, extra, QUESTIONS[missing]) if p)
            return LLMTurn(
                say=say,
                state_patch=patch or None,
                # Nichts verstanden, obwohl gefragt war: das zählt die Leiter
                # (docs/05 §2), nicht dieses Modul.
                understanding_failure=None if understood else missing,
            )
        return LLMTurn(
            tool_call=ToolCall(
                "check_slot",
                {
                    "reserved_for": slots["reserved_for"],
                    "party_size": slots["party_size"],
                },
            ),
            state_patch=patch or None,
        )

    def _greeting_once(self) -> str | None:
        """Die Pflicht-Offenlegung als KI steht im ersten Satz (prompts/system_v1.md),
        danach nie wieder."""
        if self._greeted:
            return None
        self._greeted = True
        return GREETING

    # -- Erkennung ---------------------------------------------------------

    def _extract(self, text: str, slots: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        party = _party_size(text)
        if party:
            patch["party_size"] = party
        when = self._reserved_for(text, slots)
        if when:
            patch["reserved_for"] = when
        name = _NAME.search(text)
        if name:
            patch["guest_name"] = name.group(1)
        phone = _phone(text)
        if phone:
            patch["phone"] = phone
        return patch

    def _reserved_for(self, text: str, slots: dict[str, Any]) -> str | None:
        """Nur mit Uhrzeit: ein Tag allein reicht für keine Reservierung, und geraten
        wird nicht (CLAUDE.md §2 Regel 2). Ohne Tagesangabe gilt der schon genannte
        Tag - auf eine Alternative antwortet der Gast nur mit der Uhrzeit ("dann
        halb acht"), und ohne den gemerkten Tag buchte das den Abend auf heute um
        (Codex-Review PR #104, P1). Ist noch kein Tag genannt, gilt heute, sofern die
        Zeit noch kommt, sonst morgen."""
        clock = _clock(text)
        if clock is None:
            return None
        hour, minute = clock
        local_now = self._now.astimezone(self._zone)
        day = _day(text, local_now)
        if day is None and _DATE.search(text):
            # Ein genanntes, aber unmoegliches Datum ("am 31.02.") ist nicht
            # verstanden. Es stillschweigend durch den heutigen Tag zu ersetzen waere
            # geraten (CLAUDE.md §2 Regel 2), also gilt die Zeit als unverstanden.
            return None
        day = day or self._known_day(slots.get("reserved_for"))
        if day is None:
            candidate = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= local_now:
                candidate += timedelta(days=1)
            return candidate.isoformat()
        return datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=self._zone
        ).isoformat()

    def _known_day(self, reserved_for: Any) -> date | None:
        """Der Tag aus einer bereits genannten Wunschzeit, in der Zeitzone des Betriebs."""
        if not isinstance(reserved_for, str):
            return None
        try:
            return datetime.fromisoformat(reserved_for).astimezone(self._zone).date()
        except ValueError:
            return None


def _draft(slots: dict[str, Any]) -> dict[str, Any]:
    return {field: slots[field] for field in SLOT_ORDER}


def _as_tool_result(text: str) -> dict[str, Any] | None:
    """`agent/loop.py` gibt Tool-Ergebnisse als JSON-Zeile zurück statt als Kundenzug."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and "tool" in parsed:
        return parsed
    return None


def _mentions_stem(text: str, stems: tuple[str, ...]) -> bool:
    """Teilstueck-Treffer, gewollt: "liefer" deckt Lieferung, liefern, geliefert ab."""
    lowered = text.lower()
    return any(stem in lowered for stem in stems)


def _mentions_word(text: str, words: tuple[str, ...]) -> bool:
    """Ganze Woerter: "ja" darf nicht in "Januar" oder "Jana" treffen, sonst gilt ein
    Nachdenken als Zustimmung und bucht den Entwurf (Codex-Review PR #104, P1)."""
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


def _number(raw: str) -> int | None:
    lowered = raw.lower()
    if lowered in NUMBER_WORDS:
        return NUMBER_WORDS[lowered]
    return int(raw) if raw.isdigit() else None


def _party_size(text: str) -> int | None:
    match = _PARTY.search(text) or _PARTY_TABLE.search(text)
    return _number(match.group(1)) if match else None


def _phone(text: str) -> str | None:
    for match in _PHONE.finditer(text):
        raw = match.group(1).strip()
        if sum(character.isdigit() for character in raw) >= 7:
            # Normalisiert wird in domain/customers/phone.py, nicht hier.
            return raw
    return None


def _clock(text: str) -> tuple[int, int] | None:
    for pattern in (_TIME_COLON, _TIME_DOT):
        match = pattern.search(text)
        if match:
            return _valid_clock(int(match.group(1)), int(match.group(2)))
    match = _TIME_HOUR.search(text)
    if match:
        return _valid_clock(int(match.group(1)), int(match.group(2) or 0))
    match = _TIME_HALF.search(text)
    if match:
        hour = _number(match.group(1))
        # "halb acht" ist 19:30, nicht 20:30 - und am Telefon abends gemeint.
        return _valid_clock((hour - 1) % 24, 30) if hour else None
    return None


# Unter dieser Stunde ist die Abendangabe gemeint: "um sieben" heißt 19 Uhr, "halb
# acht" 19:30. Ab elf bleibt die Zahl stehen, sonst würde aus dem Mittagstisch um
# 11:30 die Nacht um 23:30.
PM_BELOW_HOUR = 11


def _valid_clock(hour: int, minute: int) -> tuple[int, int] | None:
    if hour < PM_BELOW_HOUR:
        hour += 12
    return (hour, minute) if hour < 24 and minute < 60 else None


def _day(text: str, local_now: datetime) -> date | None:
    lowered = text.lower()
    match = _DATE.search(text)
    if match:
        year = int(match.group(3)) if match.group(3) else local_now.year
        try:
            return date(year, int(match.group(2)), int(match.group(1)))
        except ValueError:
            # "am 31.02." ist ein zu erwartender Erkennungsfehler: nicht verstanden,
            # kein Absturz mitten im Gespräch (Codex-Review PR #104, P2).
            return None
    if "übermorgen" in lowered:
        return (local_now + timedelta(days=2)).date()
    if "morgen" in lowered:
        return (local_now + timedelta(days=1)).date()
    if "heute" in lowered:
        return local_now.date()
    for name, index in WEEKDAYS.items():
        if name in lowered:
            ahead = (index - local_now.weekday()) % 7 or 7
            return (local_now + timedelta(days=ahead)).date()
    return None
