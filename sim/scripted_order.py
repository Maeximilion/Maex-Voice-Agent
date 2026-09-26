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
from api.domain.menu.numberwords import (
    canonical_card,
    find_quantity,
    fold,
    parse_cardinal,
    sole_item_number,
)
from api.domain.menu.search import (
    SAY_ALLERGY_NOTE,
    SAY_NOT_FOUND,
    SAY_SOLD_OUT,
    allergy_question,
    say_for_wish,
    say_understood,
)
from api.domain.menu.wishes import classify_wish
from api.schemas.menu import MenuHit, OptionGroup, Wish

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
# Vor dem fuehrenden Zahlwort: Artikel und Marker zaehlen nicht.
_LEAD_SKIP = frozenset({"die", "der", "das", "den", "nummer", "nr"})


@dataclass
class CartItem:
    menu_item_id: str
    number: str
    name: str
    quantity: int
    options: list[dict[str, str]] = field(default_factory=list)
    # Pflichtgruppen ohne Wahl: [{"group": ..., "options": [namen]}]
    pending: list[dict[str, Any]] = field(default_factory=list)
    # Hinweis fuer die Kueche ("ohne Karotten", eine Allergie), T-4.10.
    note: str | None = None


class PickupScript:
    """Ein Objekt je Anruf, gehalten vom `ScriptedLLM`."""

    def __init__(self) -> None:
        self.cart: list[CartItem] = []
        self.phase = "dishes"  # dishes · choose · option · more · customer
        self._suggestions: list[dict[str, Any]] = []
        self._suggestion_query = ""
        self._suggestion_wish: dict[str, Any] | None = None
        # Die order_id, deren readback gerade offen ist. Nach einem Nein ist sie
        # weg: der Gespraechszustand bleibt readback_pending, bis ein neuer
        # Entwurf kommt, und ein spaeteres Ja bestaetigte sonst die verworfene
        # Bestellung (CLAUDE.md §2 Regel 3).
        self.readback_for: str | None = None
        # Weitere unklare Teile eines Satzes: eine Rueckfrage nach der anderen,
        # keiner faellt still weg. Nach der ersten Antwort wird der naechste gesucht.
        self._later: list[str] = []
        # Was vor einer nachgeholten Suche gesagt werden soll ("Gern, Nummer 23."):
        # ein Zug ist Satz oder Tool-Aufruf, nie beides.
        self._carry: str | None = None
        # Die Position, zu der "Wogegen sind Sie allergisch?" offen ist: die Antwort
        # ist die Zutat, kein Gericht (Codex PR #139, P1).
        # Mehrere Positionen: jede einzeln, in Reihenfolge (Codex PR #139, P1).
        self._allergy_for: list[CartItem] = []
        # Stehen mehrere offen, nennt jede Frage ihr Gericht.
        self._allergy_named = False
        # Was nach der Allergie gefragt wird: nie zwei Fragen zugleich, sonst
        # waere "Nummer 12" die Zutat (Codex PR #139, P1).
        self._after_allergy: str | None = None
        # Die offene Rueckfrage, waehrend eine Antwort neu gesucht wird.
        self._reopen: tuple[list[dict[str, Any]], str] | None = None

    # -- Kundenzug ---------------------------------------------------------

    def start(
        self, prefix: str | None = None, patch: dict[str, Any] | None = None
    ) -> LLMTurn:
        return LLMTurn(say=_join(prefix, SAY_WHAT), state_patch=patch or None)

    def on_customer(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        if self._allergy_for:
            return self._answer_allergy(text, slots, patch)
        if self.phase == "choose":
            hit = self._pick_suggestion(text)
            if hit is not None and hit.get("sold_out"):
                # Heute aus: nicht aufnehmen, sagen und den Rest anbieten. Sonst
                # lehnte draft_order den Warenkorb ab, und die Bestellung kaeme
                # nicht mehr zum Abschluss (Codex PR #133).
                sold = SAY_SOLD_OUT.format(name=hit["name"])
                self._suggestions = [h for h in self._suggestions if h is not hit]
                if self._suggestions:
                    return LLMTurn(
                        say=_join(sold, _offer(self._suggestions)),
                        state_patch=patch or None,
                    )
                self.phase = "dishes"
                return self._next(slots, patch, lead=sold)
            if hit is not None:
                # Menge aus der Antwort ("einmal die dreizehn", auch ohne Marker:
                # "zwei Pho Bo", "zwei Nummer dreizehn"), sonst aus der Frage, zu
                # der die Vorschlaege gehoeren ("zwei Suppen") (Codex PR #133).
                quantity = _stated_quantity(text, hit) or _quantity(
                    self._suggestion_query, hit
                )
                wish = self._add(
                    hit, self._suggestion_query, quantity, self._suggestion_wish
                )
                lead = _wish_sentence(hit, wish, text)
                # Erst hier ist die Rueckfrage beantwortet. In _add geloescht, verlor
                # ein eindeutiger Teil im selben Satz die offene Frage (Codex PR #130).
                self._suggestions = []
                self.phase = "dishes"
                return self._next(slots, patch, lead=lead)
            # "Nein, das wars" oder "keine davon": die Vorschlaege sind verworfen.
            # Neu gesucht fanden die Worte kein Gericht, und dieselbe Frage kaeme
            # endlos zurueck (Codex PR #133).
            if _finishes(text) or _rejects(text):
                self._suggestions = []
                self.phase = "customer" if _finishes(text) and self.cart else "dishes"
                return self._next(slots, patch)
            # Keine der angebotenen: ein anderes Gericht, neu suchen mit eigener
            # Menge - die aus der Frage darauf zu legen waere geraten (Review PR
            # #133). Die Rueckfrage ist damit erledigt (Codex PR #130, P1) -
            # ausser, die Suche findet nichts: dann wird sie wieder gestellt,
            # statt dass die Position still verschwindet (on_search_failed).
            self._reopen = (self._suggestions, self._suggestion_query)
            self.phase = "dishes"
            self._suggestions = []
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
        reopen, self._reopen = self._reopen, None
        if data.get("match_type") == "positions":
            unclear: str | None = None
            # "heute aus" ist eine Aussage, keine Frage: sie kommt direkt mit,
            # ohne zweite Suche (Review PR #133).
            sold_out: list[str] = []
            for part in data["positions"]:
                if not part["ok"]:
                    # Jeder unklare Teil wird nachgefragt, einer nach dem anderen
                    # (Codex PR #130, P1).
                    if unclear:
                        self._later.append(part["query"])
                    else:
                        unclear = part.get("say")
                    continue
                if unclear and part.get("match_type") not in CLEAR_MATCHES:
                    self._later.append(part["query"])
                    continue
                # Jeder eindeutige Teil kommt in den Warenkorb, auch nach einer
                # Rueckfrage; gesprochen wird nur die erste (Codex PR #130, P1).
                said = self._take(part["query"], part)
                if said and _is_sold_out(part):
                    sold_out.append(said)
                else:
                    unclear = unclear or said
            if self._allergy_for and unclear:
                self._after_allergy, unclear = unclear, None
            lead = _join(self.take_carry(), data.get("say"), *sold_out, unclear)
            return self._next(slots, {}, lead=lead)
        carried = self._carried_wish(reopen, data)
        if carried is not None:
            # Die neue Suche traf eines der angebotenen Gerichte: der Wunsch aus
            # der Frage gilt fuer es, sonst fiele eine Allergie still weg (Review
            # PR #139).
            hit = data["results"][0]
            wish = self._add(hit, query, wish=carried)
            tail = (
                say_for_wish(MenuHit.model_validate(hit), Wish.model_validate(wish))
                if wish
                else None
            )
            return self._next(slots, {}, lead=_join(self.take_carry(), say, tail))
        taken = self._take(query, data)
        lead = _join(self.take_carry(), taken if taken is not None else say)
        return self._next(slots, {}, lead=lead)

    def _carried_wish(
        self,
        reopen: tuple[list[dict[str, Any]], str] | None,
        found: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Der Wunsch der offenen Frage, wenn die neue Suche eindeutig eines der
        angebotenen Gerichte fand und selbst keinen Wunsch traegt."""
        hits = found.get("results") or []
        if (
            reopen is None
            or self._suggestion_wish is None
            or found.get("wish")
            or found.get("match_type") not in CLEAR_MATCHES
            or not hits
            or hits[0].get("sold_out")
        ):
            return None
        offered = {h["menu_item_id"] for h in reopen[0]}
        return self._suggestion_wish if hits[0]["menu_item_id"] in offered else None

    def on_search_failed(self, say: str | None) -> LLMTurn | None:
        """Die Antwort auf eine Rueckfrage fand nichts: die Frage gilt weiter.
        Die allgemeine Meldung ("nicht gefunden, Nummer?") wuerde eine zweite
        Frage daneben stellen, eine genaue ("Nummer 99 habe ich nicht") kommt
        mit. None, wenn keine Rueckfrage offen war."""
        if self._reopen is None:
            return None
        self._suggestions, self._suggestion_query = self._reopen
        self._reopen = None
        self.phase = "choose"
        question = _offer(self._suggestions)
        lead = question if say in (None, SAY_NOT_FOUND) else _join(say, question)
        return LLMTurn(say=_join(self.take_carry(), lead))

    def take_carry(self) -> str | None:
        carry, self._carry = self._carry, None
        return carry

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
            self._add(hit, query, wish=found.get("wish"))
            return None
        if hits:
            self.phase = "choose"
            self._suggestions = hits
            self._suggestion_query = query
            # Ein Wunsch bei mehreren Treffern wird nach der Wahl eingeordnet.
            self._suggestion_wish = found.get("wish")
            return _offer(hits)
        return found.get("say")

    def _add(
        self,
        hit: dict[str, Any],
        query: str,
        quantity: int | None = None,
        wish: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
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
                quantity=quantity or _quantity(query, hit),
                pending=pending,
            )
        )
        return self._apply_wish(self.cart[-1], hit, wish)

    def _apply_wish(
        self, item: CartItem, hit: dict[str, Any], wish: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Der Wunsch aus search_menu am Warenkorb (T-4.10): eine Option der Karte
        wird gewaehlt (die Pflichtfrage dieser Gruppe entfaellt), ein Hinweis und
        eine Allergie gehen in `note`. Was die Karte nicht kennt, bleibt weg - der
        Satz dazu kam schon aus dem Code (D8)."""
        if wish is None:
            return None
        if wish["kind"] == "open":
            groups = [
                OptionGroup.model_validate(g) for g in hit.get("option_groups", [])
            ]
            wish = classify_wish(wish["text"], groups).model_dump()
        if wish.get("note"):
            # Weglassen zu einer Zugabe ("ohne Zwiebeln, dafür mit Huhn").
            item.note = wish["note"]
        if wish["kind"] == "option":
            item.options.append({"group": wish["group"], "name": wish["option"]})
            item.pending = [g for g in item.pending if g["group"] != wish["group"]]
        elif wish["kind"] == "allergy" and not wish.get("ingredient"):
            # Die Zutat fehlt: die Frage bleibt offen, bis die Antwort kommt.
            self._allergy_for.append(item)
            self._allergy_named = self._allergy_named or len(self._allergy_for) > 1
        elif wish["kind"] == "note" or (
            wish["kind"] == "allergy" and wish.get("ingredient")
        ):
            # Die Allergie im festen Wortlaut (E14); ohne Zutat fragt der Satz nach.
            item.note = wish["text"]
        return wish

    def _pick_suggestion(self, text: str) -> dict[str, Any] | None:
        """Nur eine eindeutige Nennung zaehlt: die Nummer oder ein Name, der genau
        auf eine der angebotenen passt."""
        lowered = text.lower()
        # Auch gesprochen ("die dreizehn") und mit fuehrender Null (Review PR #133).
        # Nur, wenn der Satz die Nummer selbst ist: in "zwei Pho Bo" ist die Zwei
        # eine Menge, keine Karte 2 - dieselbe Regel wie in der Suche (Codex PR
        # #133, P2).
        ref, _ = sole_item_number(text)
        if ref is not None:
            card = canonical_card(ref.text)
            by_number = [
                h for h in self._suggestions if canonical_card(h["number"]) == card
            ]
            if len(by_number) == 1:
                return by_number[0]
        by_name = [h for h in self._suggestions if h["name"].lower() in lowered]
        if len(by_name) == 1:
            return by_name[0]
        # "Meinen Sie Nummer 23?" - "Ja, genau": ein Ja nimmt den einen Vorschlag.
        # Neu gesucht fand "Ja, genau" kein Gericht, und dieselbe Frage kam
        # endlos zurueck (Eval-Suite T-5.2). Erst nach Nummer und Name, und nie,
        # wenn der Satz eine andere Nummer nennt: "Ja, aber lieber die 24" meint
        # die 24. Bei mehreren Vorschlaegen ist ein Ja keine Wahl.
        if len(self._suggestions) == 1 and ref is None and _agrees(text):
            return self._suggestions[0]
        return None

    def _answer_allergy(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        """Die Antwort auf "Wogegen?": die Zutat im festen Wortlaut (E14)."""
        # Eine ganze Antwort ("ich bin gegen Erdnuesse allergisch") traegt ihre
        # Zutat selbst; nur das blosse Wort ("Erdnuesse", "gegen Erdnuesse")
        # bekommt den Satzanfang (Codex PR #139, P1).
        wish = classify_wish(text, [])
        if wish.kind != "allergy" and _orders_something(text):
            # "Eine Cola bitte", "Nummer 12": keine Zutat - die Frage bleibt
            # offen, statt "Keine Eine Cola" zu notieren (Review PR #139).
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        if wish.kind != "allergy":
            # "Keine Erdnuesse" ergaebe sonst "Keine Keine Erdnuesse".
            bare = re.sub(
                r"^\s*(?:gegen|auf|keine[nm]?|kein)\s+", "", text, flags=re.IGNORECASE
            )
            wish = classify_wish(f"allergisch gegen {bare}", [])
        if not wish.ingredient:
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        item = self._allergy_for.pop(0)
        item.note = wish.text
        lead = SAY_ALLERGY_NOTE.format(name=item.name)
        if not self._allergy_for:
            self._allergy_named = False
            lead = _join(lead, self._after_allergy)
            self._after_allergy = None
        return self._next(slots, patch, lead=lead)

    def _allergy_question(self) -> str:
        names = [i.name for i in self._allergy_for]
        return allergy_question(names, self._allergy_named) or ""

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
        if self._allergy_for:
            # Nur die Frage nach der Allergie, keine zweite daneben. Steht sie
            # schon im Satz der Suche, nicht noch einmal.
            question = self._allergy_question()
            said = _join(lead) or ""
            return LLMTurn(
                say=said if question in said else _join(lead, question),
                state_patch=state_patch,
            )
        if self._later and self.phase != "choose":
            self._carry = _join(self._carry, lead)
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
                    **({"note": i.note} if i.note else {}),
                }
                for i in self.cart
            ],
        }


def _wish_sentence(
    hit: dict[str, Any], wish: dict[str, Any] | None, said: str
) -> str | None:
    """Nach der Wahl aus Vorschlaegen wird der Wunsch erst eingeordnet - und wie
    in der Suche gesagt: eine Option oder ein Hinweis mit wiederholt, das
    Unbekannte abgelehnt, die Allergie ohne Zusage (Codex PR #139)."""
    if wish is None:
        return None
    menu_hit, settled = MenuHit.model_validate(hit), Wish.model_validate(wish)
    echo = (
        None
        if settled.kind == "unknown"
        else say_understood([("alias", menu_hit, settled)], said)
    )
    return _join(echo, say_for_wish(menu_hit, settled)) or None


# Womit eine Bestellung beginnt, nie eine Zutat ("eine Cola", "Nummer 12").
_ORDER_LEADS = frozenset({"und", "ein", "eine", "einen", "einmal", "nummer", "noch"})


def _orders_something(text: str) -> bool:
    words = re.findall(r"[^\W_]+", text.lower())
    return (
        any(w.isdigit() for w in words)
        or sole_item_number(text)[0] is not None
        or (bool(words) and words[0] in _ORDER_LEADS)
    )


def _offer(hits: list[dict[str, Any]]) -> str:
    offer = " oder ".join(f"Nummer {h['number']} {h['name']}" for h in hits)
    return f"Meinen Sie {offer}?"


def _is_sold_out(found: dict[str, Any]) -> bool:
    hits = found.get("results") or []
    return (
        found.get("match_type") in CLEAR_MATCHES
        and bool(hits)
        and bool(hits[0].get("sold_out"))
    )


def _search(text: str, patch: dict[str, Any]) -> LLMTurn:
    return LLMTurn(
        tool_call=ToolCall("search_menu", {"query": text}), state_patch=patch or None
    )


def _quantity(query: str, hit: dict[str, Any] | None = None) -> int:
    """Die Menge, eins, wenn keine genannt ist - sie wird beim Vorlesen bestaetigt."""
    return _stated_quantity(query, hit) or 1


def _stated_quantity(query: str, hit: dict[str, Any] | None = None) -> int | None:
    """Die genannte Menge oder None. Nach der Regel der Domain (numberwords): mit
    Marker ("zweimal", "2 x") gilt sie immer. Ist der Satz die Kartennummer
    selbst ("die 7", "die sieben", "die 23 a"), ist ein fuehrendes Zahlwort nur
    eine Menge, wenn es nicht die Nummer ist ("zwei Nummer 23"). Neben einem
    Namen ist es eine Menge ("zwei Frühlingsrollen") - ausser, das gefundene
    Gericht erklaert es: seine eigene Nummer mit Artikel davor ("die 23,
    Frühlingsrollen") oder ein Name, der selbst mit dem Zahlwort beginnt ("Acht
    Schätze") (Review PR #133)."""
    marked = find_quantity(query)
    if marked:
        return marked
    words = _words(query)
    at = next((i for i, w in enumerate(words) if w not in _LEAD_SKIP), None)
    if at is None:
        return None
    lead = parse_cardinal(words[at])
    if not lead:
        return None
    ref, _ = sole_item_number(query)
    if ref is not None:
        return lead if lead != ref.value else None
    if hit is not None:
        name = _words(hit["name"])
        if name and name[0] == words[at]:
            return None
        if at > 0 and canonical_card(str(lead)) == canonical_card(hit["number"]):
            return None
    return lead


def _words(text: str) -> list[str]:
    # Gefaltet wie im Zahlwort-Parser: "fuenf" und "fünf" sind ein Wort (Codex PR
    # #133, P2).
    return re.findall(r"[^\W_]+", fold(text))


def _option_question(item: CartItem, group: dict[str, Any]) -> str:
    offer = " oder ".join(group["options"])
    return f"Welche Auswahl bei {group['group']} möchten Sie zu {item.name}: {offer}?"


# Auf eine Rueckfrage: "keine davon", "nein" verwirft die Vorschlaege.
REJECT_WORDS = ("nein", "keine", "keins", "keinen", "weder")


def _finishes(text: str) -> bool:
    """Fertig mit den Gerichten, ausser dem blossen "nein": das verwirft auf eine
    Rueckfrage nur die Vorschlaege."""
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(p)}\b", lowered) for p in DONE_PHRASES if p != "nein"
    )


# Kein "gern": "Ich haette gern die 24" ist eine Bestellung, kein Ja.
_AGREE = re.compile(r"\b(ja|jawohl|genau|richtig|stimmt|korrekt)\b")
# "stimmt nicht", "nicht richtig", "ja, aber ...": kein Ja zum Vorschlag.
_DOUBT = re.compile(r"\b(nein|nicht|aber|lieber|sondern|anders)\b")


def _agrees(text: str) -> bool:
    """Ein Ja ohne Zweifel: "Ja, genau" nimmt den Vorschlag, "Das stimmt nicht" nicht."""
    lowered = text.lower()
    return (
        bool(_AGREE.search(lowered))
        and not _DOUBT.search(lowered)
        and not _rejects(text)
    )


def _rejects(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{w}\b", lowered) for w in REJECT_WORDS)


def _is_done(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in DONE_PHRASES)


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)
