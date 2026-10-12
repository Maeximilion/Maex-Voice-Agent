"""Beobachter zwischen Gesprächskern und Modell: was hat das Modell wann getan (docs/08 §2).

Zwei der drei harten Metriken lassen sich am Datenbankzustand allein nicht
ablesen, weil `calls.tool_calls` nur Name, Dauer und Erfolg festhält:

- **Geratene Position:** eine `menu_item_id` in `draft_order`, die keine Suche
  (`search_menu`, `get_item_details`) im selben Anruf geliefert hat. Das Modell
  hätte sie dann erfunden oder aus einem anderen Anruf mitgebracht
  (CLAUDE.md §2 Regel 2).
- **Unbestätigter Vorgang:** ein `confirm`, vor dem der letzte Kundensatz kein
  Ja war (CLAUDE.md §2 Regel 3), or whose draft was not what the model said
  last before that yes (#251).

Both count only for a call that went through. One the core refused or the tool
rejected booked nothing; it is taken back and noted as a refused attempt
(`Recording.refused_attempts`, docs/08 §2, Maxi 07.10.2026).

Der Recorder sitzt deshalb an der Stelle, an der alles vorbeikommt: jeder
Kundensatz und jedes Tool-Ergebnis geht als `user_input` ans Modell, jeder
Tool-Aufruf kommt als `LLMTurn` zurück. Er verändert nichts, er schreibt nur mit.
Weil er für jeden `LLMClient` gleich funktioniert, misst er das Skript-Modell
von heute und das echte Modell aus T-2.4 mit derselben Regel.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

# The yes detector lives in the core since the core refuses a `confirm` without
# a yes itself (agent/guards.py); the recorder measures with the same rule.
from api.agent.consent import is_yes, read_out
from api.agent.llm import LLMClient, LLMTurn, Usage
from api.agent.state import cart_lines, same_order

SEARCH_TOOLS = frozenset({"search_menu", "get_item_details"})
# Legen einen Entwurf an, der vorgelesen und dann bestaetigt werden muss.
DRAFT_TOOLS = frozenset({"draft_order", "create_reservation"})
# Take a readback away in the core whatever their result (`agent/state.py`
# `_supersedes`): the guest is correcting, and a yes afterwards answers the new
# question, not the old draft (Codex PR #239).
SUPERSEDING_TOOLS = frozenset({"search_menu", "check_slot"})
# The name under which a refused `cart` comes back (`agent/loop.py`).
CART = "cart"


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
    # Erfolgreiche Tool-Ergebnisse (Name, Daten): `expected.tools` zaehlt nur,
    # was wirklich geliefert hat, nicht jeden Versuch (Codex PR #145, P1).
    ok_results: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Rejected tool results (name, error_code): a delivery outside the zone is
    # only handled correctly when the check rejected it (Codex PR #162).
    error_results: list[tuple[str, str]] = field(default_factory=list)
    # Arguments of every call that succeeded (name, args): a zone check only
    # counts for the address it was actually asked about (Codex PR #162).
    ok_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    # Anzahl der Kundensaetze beim letzten Entwurf: das Ja muss danach kommen.
    drafted_at: int | None = None
    # The sentence of the last draft, and whether it was what the model said
    # last: a yes answers what the guest heard (`consent.read_out`, the rule
    # of the core, `agent/guards.py`).
    readback: str | None = None
    read_out: bool = False
    # (tool, guest sentence before it) for every call that would have been a
    # hard violation and did not go through: the core refused it
    # (`agent/guards.py`) or the tool rejected it. Nothing was booked, so it
    # is no violation; the number shows how often a model tries (Maxi,
    # 07.10.2026).
    refused_attempts: list[tuple[str, str]] = field(default_factory=list)


class RecordingLLM:
    """Hüllt einen `LLMClient` ein und schreibt mit, ohne einzugreifen."""

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self.recording = Recording()
        # What the recording held before the open call (`_retract`).
        self._before: tuple[int, int, int, dict[str, Any] | None] = (0, 0, 0, None)
        # The call whose result arrives with the next input.
        self._open_call: tuple[str, dict[str, Any]] | None = None
        # The order as the core holds it (`ConversationState.cart`): the lines
        # of the last draft, or of the last `cart` the core took. A `cart`
        # that differs from it after a read-back is a correction, and the core
        # drops the draft (`state.replace_cart`, `guards.take_cart`).
        self._cart: list[dict[str, Any]] = []
        self._cart_before: list[dict[str, Any]] = []

    @property
    def usage(self) -> Usage | None:
        """What the wrapped model counted (`ChatCompletionsLLM.usage`), passed
        on so the text phone and the report see it through the recorder. None
        for the scripted stand-in, which counts nothing."""
        return getattr(self._inner, "usage", None)

    def next_turn(
        self, system_prompt: str, state: dict[str, Any], user_input: str
    ) -> LLMTurn:
        self._observe_input(user_input)
        turn = self._inner.next_turn(system_prompt, state, user_input)
        if turn.tool_call is not None:
            self._observe_call(turn.tool_call.name, turn.tool_call.args)
        elif turn.say is not None:
            rec = self.recording
            rec.read_out = bool(rec.readback) and read_out(turn.say, rec.readback)
        if turn.cart is not None:
            self._observe_cart(turn.cart)
        return turn

    def _observe_cart(self, cart: Any) -> None:
        """A `cart` next to the answer. The core checks it before it speaks or
        dispatches (`guards.take_cart`): a changed order after a read-back is a
        correction and drops the draft, and so does a `cart` it refuses. A
        refusal comes back as the next input and is handled there."""
        try:
            lines = cart_lines(cart)
        except ValidationError:
            return
        if same_order(lines, self._cart):
            return
        self._cart_before, self._cart = self._cart, lines
        self._drop_draft()

    def _drop_draft(self) -> None:
        self.recording.drafted_at = None
        self.recording.read_out = False

    def _observe_input(self, user_input: str) -> None:
        result = _tool_result(user_input)
        if result is None:
            self.recording.customer_lines.append(user_input)
            return
        call, self._open_call = self._open_call, None
        if result.get("tool") == CART and not result.get("ok"):
            # The core refused the `cart`: the answer next to it was neither
            # spoken nor carried out, the order stays what it was, and a draft
            # that waited for its yes is gone (`guards.take_cart`).
            self._cart = self._cart_before
            self._drop_draft()
        if call is not None and not result.get("ok"):
            # Whatever failed, the tool or the answer around it (a refused
            # `cart` stops the call next to it), the call did not go through.
            self._retract(call[0])
        if result.get("ok"):
            self.recording.ok_results.append(
                (str(result.get("tool")), result.get("data") or {})
            )
            if call is not None and call[0] == result.get("tool"):
                self.recording.ok_calls.append(call)
        elif result.get("error_code"):
            self.recording.error_results.append(
                (str(result.get("tool")), str(result["error_code"]))
            )
        if result.get("tool") in DRAFT_TOOLS and result.get("ok"):
            # A new draft is a new sentence, and nobody heard it yet.
            self.recording.readback = (result.get("data") or {}).get("readback")
            self.recording.read_out = False
            if call is not None and call[0] == "draft_order":
                # The order in the state is the one that was drafted.
                try:
                    self._cart = cart_lines(call[1].get("items"))
                except ValidationError:
                    self._cart = []
        if result.get("tool") in SEARCH_TOOLS and result.get("ok"):
            self.recording.searched_ids |= set(_menu_item_ids(result.get("data")))

    def _retract(self, name: str) -> None:
        """Takes back what `_observe_call` noted for a call that did not go
        through. Hard is what happened, not what a model tried: a `confirm`
        on a "Nein" that the core refused booked nothing. A call whose result
        never comes back keeps its entries, the careful side."""
        rec = self.recording
        guessed, unconfirmed, rec.confirms, rec.last_confirm = self._before
        if len(rec.guessed) > guessed or len(rec.unconfirmed) > unconfirmed:
            last = rec.customer_lines[-1] if rec.customer_lines else ""
            rec.refused_attempts.append((name, last))
        del rec.guessed[guessed:]
        del rec.unconfirmed[unconfirmed:]
        if name in DRAFT_TOOLS:
            # No draft a yes could answer, not even the one before: a failed
            # correction takes the readback away in the core (`agent/state.py`
            # `_supersedes`), and the recorder has to agree (Codex PR #239).
            rec.drafted_at = None

    def _observe_call(self, name: str, args: dict[str, Any]) -> None:
        rec = self.recording
        # The state before this call, for `_retract`.
        self._before = (
            len(rec.guessed),
            len(rec.unconfirmed),
            rec.confirms,
            rec.last_confirm,
        )
        rec.tool_calls.append(name)
        self._open_call = (name, dict(args))
        last = rec.customer_lines[-1] if rec.customer_lines else ""
        if name in DRAFT_TOOLS:
            rec.drafted_at = len(rec.customer_lines)
        elif name in SUPERSEDING_TOOLS:
            rec.drafted_at = None
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
            if not (after_draft and rec.read_out and is_yes(last)):
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
