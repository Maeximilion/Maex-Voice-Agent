"""Abholbestellung im regelbasierten Modell-Ersatz (`sim/scripted_llm.py`).

Der Ablauf aus `prompts/system_v2.md` §Ablauf 4: Gerichte ueber `search_menu`,
Pflichtoptionen erfragen, Name und Rufnummer, `draft_order`, vorlesen, auf ein
Ja hin `confirm`. Wie der Rest des Skripts erkennt das hier nur Muster und raet
nichts: eine Position kommt nur mit einer `menu_item_id` aus `search_menu` in
den Warenkorb, bei mehreren Treffern werden sie angeboten, nie einer gewaehlt
(CLAUDE.md §2 Regel 2). Namen von Gerichten und Optionen stammen aus den
Tool-Ergebnissen, nie aus diesem Modul.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from api.agent.llm import LLMTurn, ToolCall
from api.domain.menu.numberwords import find_quantity, parse_cardinal

SAY_WHAT = "Was möchten Sie bestellen?"
SAY_MORE = "Darf es noch etwas sein?"
SAY_NAME = "Auf welchen Namen darf ich die Bestellung notieren?"
SAY_PHONE = "Unter welcher Telefonnummer sind Sie erreichbar?"
SAY_RESTART = "Dann noch einmal von vorn: was möchten Sie bestellen?"
SAY_THANKS = "Vielen Dank und bis gleich."
SAY_APPROVAL = "Das Team bestätigt die Bestellung gleich noch."

# Wer das sagt, ist mit den Gerichten fertig.
DONE_PHRASES = ("nein", "das war", "das wars", "nichts mehr", "alles", "sonst nichts")
CLEAR_MATCHES = ("exact_number", "alias", "fuzzy_single")


@dataclass
class CartItem:
    menu_item_id: str
    number: str
    name: str
    quantity: int
    options: list[dict[str, str]] = field(default_factory=list)
    # Pflichtgruppen ohne Wahl: [{"group": ..., "options": [namen]}]
    pending: list[dict[str, Any]] = field(default_factory=list)


class PickupScript:
    """Ein Objekt je Anruf, gehalten vom `ScriptedLLM`."""

    def __init__(self) -> None:
        self.cart: list[CartItem] = []
        self.phase = "dishes"  # dishes · choose · option · more · customer
        self._suggestions: list[dict[str, Any]] = []
        self._suggestion_query = ""
        # Die order_id, deren readback gerade offen ist. Nach einem Nein ist sie
        # weg: der Gespraechszustand bleibt readback_pending, bis ein neuer
        # Entwurf kommt, und ein spaeteres Ja bestaetigte sonst die verworfene
        # Bestellung (CLAUDE.md §2 Regel 3).
        self.readback_for: str | None = None
        # Weitere unklare Teile eines Satzes: eine Rueckfrage nach der anderen,
        # keiner faellt still weg. Nach der ersten Antwort wird der naechste gesucht.
        self._later: list[str] = []

    # -- Kundenzug ---------------------------------------------------------

    def start(self, prefix: str | None = None) -> LLMTurn:
        return LLMTurn(say=_join(prefix, SAY_WHAT))

    def on_customer(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        if self.phase == "choose":
            hit = self._pick_suggestion(text)
            if hit is not None:
                self._add(hit, self._suggestion_query)
                self.phase = "dishes"
                return self._next(slots, patch)
            # Keine der angebotenen: neu suchen mit dem, was jetzt gesagt wurde.
            return _search(text, patch)
        if self.phase == "option":
            return self._answer_option(text, slots, patch)
        if self.phase == "more":
            if _is_done(text):
                self.phase = "customer"
                return self._next(slots, patch)
            return _search(text, patch)
        if self.phase == "customer":
            return self._next(slots, patch, understood=bool(patch))
        return _search(text, patch)

    def on_draft(self, data: dict[str, Any]) -> LLMTurn:
        self.readback_for = data["order_id"]
        return LLMTurn(say=data["readback"])

    def on_readback(self, yes: bool) -> LLMTurn:
        order_id = self.readback_for
        if yes and order_id:
            return LLMTurn(
                tool_call=ToolCall(
                    "confirm", {"entity": "order", "entity_id": order_id}
                )
            )
        # Eine Korrektur am vorgelesenen Warenkorb wird nicht geraten, sondern neu
        # aufgenommen: der alte Entwurf bleibt Entwurf und wird nie bestaetigt.
        self.readback_for = None
        self.cart = []
        self.phase = "dishes"
        return LLMTurn(say=SAY_RESTART)

    # -- Tool-Ergebnis -----------------------------------------------------

    def on_search(
        self,
        query: str,
        data: dict[str, Any],
        slots: dict[str, Any],
        say: str | None = None,
    ) -> LLMTurn:
        """`say` wiederholt, was eindeutig verstanden wurde (aus dem Code, Maxi PR
        #127); das Skript spricht es wie ein Modell, das der Regel im Prompt folgt."""
        if data.get("match_type") == "positions":
            unclear: str | None = None
            for part in data["positions"]:
                if not part["ok"]:
                    unclear = unclear or part.get("say")
                    continue
                if unclear and part.get("match_type") not in CLEAR_MATCHES:
                    self._later.append(part["query"])
                    continue
                unclear = unclear or self._take(part["query"], part)
            return self._next(slots, {}, lead=_join(data.get("say"), unclear))
        taken = self._take(query, data)
        return self._next(slots, {}, lead=taken if taken is not None else say)

    def on_confirmed(self, data: dict[str, Any]) -> LLMTurn:
        self.readback_for = None
        code = data.get("pickup_code")
        parts = [f"Ihr Abholcode ist {code}." if code else None]
        if data.get("handover") == "awaiting_approval":
            parts.append(SAY_APPROVAL)
        parts.append(SAY_THANKS)
        return LLMTurn(say=_join(*parts))

    # -- intern ------------------------------------------------------------

    def _take(self, query: str, found: dict[str, Any]) -> str | None:
        """Nimmt einen eindeutigen Treffer auf. Sonst der Satz, der gesagt werden
        muss: ausverkauft (aus dem Code) oder die Rueckfrage bei mehreren."""
        hits = found.get("results") or []
        match_type = found.get("match_type")
        if match_type in CLEAR_MATCHES and hits:
            hit = hits[0]
            if hit.get("sold_out"):
                return found.get("say") or f"{hit['name']} ist heute leider aus."
            self._add(hit, query)
            return None
        if hits:
            self.phase = "choose"
            self._suggestions = hits
            self._suggestion_query = query
            offer = " oder ".join(f"Nummer {h['number']} {h['name']}" for h in hits)
            return f"Meinen Sie {offer}?"
        return found.get("say")

    def _add(self, hit: dict[str, Any], query: str) -> None:
        pending = [
            {"group": g["group"], "options": [o["name"] for o in g["options"]]}
            for g in hit.get("option_groups", [])
            if g.get("required")
        ]
        self.cart.append(
            CartItem(
                menu_item_id=hit["menu_item_id"],
                number=hit["number"],
                name=hit["name"],
                quantity=_quantity(query, hit["number"]),
                pending=pending,
            )
        )
        self._suggestions = []

    def _pick_suggestion(self, text: str) -> dict[str, Any] | None:
        """Nur eine eindeutige Nennung zaehlt: die Nummer oder ein Name, der genau
        auf eine der angebotenen passt."""
        lowered = text.lower()
        numbers = set(re.findall(r"\d+[a-f]?", lowered))
        by_number = [h for h in self._suggestions if h["number"].lower() in numbers]
        if len(by_number) == 1:
            return by_number[0]
        by_name = [h for h in self._suggestions if h["name"].lower() in lowered]
        return by_name[0] if len(by_name) == 1 else None

    def _answer_option(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        item, group = self._open_option()
        lowered = text.lower()
        chosen = [o for o in group["options"] if o.lower() in lowered]
        if len(chosen) != 1:
            return LLMTurn(
                say=_option_question(item, group),
                state_patch=patch or None,
                understanding_failure="option",
            )
        item.options.append({"group": group["group"], "name": chosen[0]})
        item.pending.remove(group)
        return self._next(slots, patch)

    def _open_option(self) -> tuple[CartItem, dict[str, Any]]:
        item = next(i for i in self.cart if i.pending)
        return item, item.pending[0]

    def _next(
        self,
        slots: dict[str, Any],
        patch: dict[str, Any],
        *,
        lead: str | None = None,
        understood: bool = True,
    ) -> LLMTurn:
        """Der naechste Schritt aus dem, was schon feststeht."""
        state_patch = patch or None
        if self._later and self.phase != "choose" and not lead:
            return _search(self._later.pop(0), patch)
        if self.phase == "choose":
            # Hier steht immer die Rueckfrage mit den Vorschlaegen (_take).
            return LLMTurn(say=_join(lead) or SAY_WHAT, state_patch=state_patch)
        if any(i.pending for i in self.cart):
            self.phase = "option"
            item, group = self._open_option()
            return LLMTurn(
                say=_join(lead, _option_question(item, group)), state_patch=state_patch
            )
        if not self.cart:
            self.phase = "dishes"
            return LLMTurn(say=_join(lead, SAY_WHAT), state_patch=state_patch)
        if self.phase != "customer":
            self.phase = "more"
            return LLMTurn(say=_join(lead, SAY_MORE), state_patch=state_patch)
        if not slots.get("guest_name"):
            return LLMTurn(
                say=_join(lead, SAY_NAME),
                state_patch=state_patch,
                understanding_failure=None if understood else "guest_name",
            )
        if not slots.get("phone"):
            return LLMTurn(
                say=_join(lead, SAY_PHONE),
                state_patch=state_patch,
                understanding_failure=None if understood else "phone",
            )
        return LLMTurn(
            tool_call=ToolCall("draft_order", self._draft_args(slots)),
            state_patch=state_patch,
        )

    def _draft_args(self, slots: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "pickup",
            "customer": {"name": slots["guest_name"], "phone": slots["phone"]},
            "items": [
                {
                    "menu_item_id": i.menu_item_id,
                    "quantity": i.quantity,
                    "options": i.options,
                }
                for i in self.cart
            ],
        }


def _search(text: str, patch: dict[str, Any]) -> LLMTurn:
    return LLMTurn(
        tool_call=ToolCall("search_menu", {"query": text}), state_patch=patch or None
    )


def _quantity(query: str, number: str) -> int:
    """Menge nur mit Marker ("zweimal", "2 x") oder als fuehrendes Zahlwort, das
    nicht die Kartennummer selbst ist ("zwei Frühlingsrollen", nicht "23").
    Sonst eins - die Menge wird beim Vorlesen bestaetigt."""
    marked = find_quantity(query)
    if marked:
        return marked
    words = re.findall(r"[\wäöüß]+", query.lower())
    first = next((w for w in words if w not in ("die", "der", "das", "den")), None)
    if first is None or first == number.lower():
        return 1
    value = parse_cardinal(first)
    return value if value and str(value) != number else 1


def _option_question(item: CartItem, group: dict[str, Any]) -> str:
    offer = " oder ".join(group["options"])
    return f"Welche Auswahl bei {group['group']} möchten Sie zu {item.name}: {offer}?"


def _is_done(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in DONE_PHRASES)


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)
