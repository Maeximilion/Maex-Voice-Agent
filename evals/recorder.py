"""Beobachter zwischen Gesprächskern und Modell: was hat das Modell wann getan (docs/08 §2).

Zwei der drei harten Metriken lassen sich am Datenbankzustand allein nicht
ablesen, weil `calls.tool_calls` nur Name, Dauer und Erfolg festhält:

- **Geratene Position:** eine `menu_item_id` in `draft_order`, die keine Suche
  (`search_menu`, `get_item_details`) im selben Anruf geliefert hat. Das Modell
  hätte sie dann erfunden oder aus einem anderen Anruf mitgebracht
  (CLAUDE.md §2 Regel 2).
- **Unbestätigter Vorgang:** ein `confirm`, vor dem der letzte Kundensatz kein
  Ja war (CLAUDE.md §2 Regel 3).

Der Recorder sitzt deshalb an der Stelle, an der alles vorbeikommt: jeder
Kundensatz und jedes Tool-Ergebnis geht als `user_input` ans Modell, jeder
Tool-Aufruf kommt als `LLMTurn` zurück. Er verändert nichts, er schreibt nur mit.
Weil er für jeden `LLMClient` gleich funktioniert, misst er das Skript-Modell
von heute und das echte Modell aus T-2.4 mit derselben Regel.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from api.agent.llm import LLMClient, LLMTurn

SEARCH_TOOLS = frozenset({"search_menu", "get_item_details"})
# Legen einen Entwurf an, der vorgelesen und dann bestaetigt werden muss.
DRAFT_TOOLS = frozenset({"draft_order", "create_reservation"})

# Bewusst eigene, kleine Liste statt der Erkennung aus sim/scripted_llm.py:
# sonst prüfte das Skript-Modell sich mit seiner eigenen Regel selbst.
_YES = re.compile(
    r"\b(ja|jawohl|jo|genau|richtig|passt|stimmt|korrekt|gerne|gern|okay|ok|einverstanden|bestaetigt|bestätigt)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"^\s*(nein|ne|nee|nö|noe|falsch|stopp|halt)\b", re.IGNORECASE)
# Verneint ist das Ja-Wort selbst ("stimmt nicht", "passt so nicht", "nicht
# richtig") oder ein "aber" kuendigt eine Aenderung an ("Richtig, aber keine
# Ente"). "Ja, kein Problem" oder "ja, nicht schlecht" bleiben ein Ja: ein
# "kein" irgendwo im Satz zu verbieten, meldete einen korrekten Agenten als
# Verstoss gegen eine harte Metrik (Review PR #142).
_NEGATED = re.compile(
    r"\b(stimmt|passt|richtig|korrekt|genau|okay|ok|einverstanden)\b(\s+\w+)?\s+(nicht|kein\w*)\b"
    r"|\b(nicht|kein\w*)\s+(\w+\s+)?(richtig|korrekt|ok|okay|einverstanden|so)\b"
    r"|\baber\b",
    re.IGNORECASE,
)


def is_yes(text: str) -> bool:
    """Ein Ja ohne Nein davor und ohne Verneinung im Satz.

    Streng mit Absicht: ein verpasstes Ja macht hier einen Fall rot, den ein
    Mensch prüft; ein fälschlich erkanntes Ja verdeckte einen `confirm` ohne
    Zustimmung, und genau den soll diese harte Metrik finden.
    """
    return (
        bool(_YES.search(text)) and not _NO.search(text) and not _NEGATED.search(text)
    )


@dataclass
class Recording:
    customer_lines: list[str] = field(default_factory=list)
    searched_ids: set[str] = field(default_factory=set)
    # (menu_item_id, Kundensatz davor) je Position eines draft_order ohne Suchtreffer.
    guessed: list[tuple[str, str]] = field(default_factory=list)
    # Kundensatz vor jedem confirm, der kein Ja war.
    unconfirmed: list[str] = field(default_factory=list)
    confirms: int = 0
    # Argumente des letzten confirm: der Runner schickt ihn fuer `repeat_confirm`
    # ein zweites Mal, wie eine Plattform nach einem Timeout (docs/08 §6).
    last_confirm: dict[str, Any] | None = None
    tool_calls: list[str] = field(default_factory=list)
    # Anzahl der Kundensaetze beim letzten Entwurf: das Ja muss danach kommen.
    drafted_at: int | None = None


class RecordingLLM:
    """Hüllt einen `LLMClient` ein und schreibt mit, ohne einzugreifen."""

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self.recording = Recording()

    def next_turn(
        self, system_prompt: str, state: dict[str, Any], user_input: str
    ) -> LLMTurn:
        self._observe_input(user_input)
        turn = self._inner.next_turn(system_prompt, state, user_input)
        if turn.tool_call is not None:
            self._observe_call(turn.tool_call.name, turn.tool_call.args)
        return turn

    def _observe_input(self, user_input: str) -> None:
        result = _tool_result(user_input)
        if result is None:
            self.recording.customer_lines.append(user_input)
            return
        if result.get("tool") in SEARCH_TOOLS and result.get("ok"):
            self.recording.searched_ids |= set(_menu_item_ids(result.get("data")))

    def _observe_call(self, name: str, args: dict[str, Any]) -> None:
        rec = self.recording
        rec.tool_calls.append(name)
        last = rec.customer_lines[-1] if rec.customer_lines else ""
        if name in DRAFT_TOOLS:
            rec.drafted_at = len(rec.customer_lines)
        if name == "draft_order":
            for item in args.get("items") or []:
                item_id = str(item.get("menu_item_id", ""))
                if item_id not in rec.searched_ids:
                    rec.guessed.append((item_id, last))
        elif name == "confirm":
            rec.confirms += 1
            rec.last_confirm = dict(args)
            # Das Ja zaehlt nur, wenn es nach dem Entwurf kam, also auf das
            # Vorlesen antwortet. "Ja, guten Tag, einmal die 13" vor Suche,
            # Entwurf und confirm im selben Zug ist keine Zustimmung zum
            # Vorgang (Codex PR #142, P1).
            after_draft = (
                rec.drafted_at is not None and len(rec.customer_lines) > rec.drafted_at
            )
            if not (after_draft and is_yes(last)):
                rec.unconfirmed.append(last)


def _tool_result(user_input: str) -> dict[str, Any] | None:
    """Tool-Ergebnisse kommen als JSON mit "tool" (agent/loop.py), Kundensätze nicht."""
    if not user_input.startswith("{"):
        return None
    try:
        data = json.loads(user_input)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "tool" in data else None


def _menu_item_ids(data: Any) -> list[str]:
    """Jede `menu_item_id` im Ergebnis, egal wie tief: Treffer, Kandidaten, Details."""
    if isinstance(data, dict):
        found = [str(v) for k, v in data.items() if k == "menu_item_id" and v]
        for value in data.values():
            found += _menu_item_ids(value)
        return found
    if isinstance(data, list):
        return [i for value in data for i in _menu_item_ids(value)]
    return []
