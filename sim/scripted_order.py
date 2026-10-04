"""Pickup order in the rule-based stand-in model (`sim/scripted_llm.py`).

The flow from `prompts/system_v2.md` §Ablauf 4: dishes via `search_menu`, ask
for mandatory options, name and phone number, `draft_order`, read back,
`confirm` on a yes. Like the rest of the script this only recognises patterns
and guesses nothing: a position enters the cart only with a `menu_item_id`
from `search_menu`; with several hits they are offered, never one chosen
(CLAUDE.md §2 rule 2). Names of dishes and options come from the tool results,
never from this module.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from api.agent.llm import LLMTurn, ToolCall
from api.domain.menu.numberwords import (
    NO_PREFIXES,
    CardFormat,
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

# Whoever says this is done with the dishes.
DONE_PHRASES = ("nein", "das war", "das wars", "nichts mehr", "alles", "sonst nichts")
CLEAR_MATCHES = ("exact_number", "alias", "fuzzy_single")
# Before the leading number word: articles and markers do not count.
_LEAD_SKIP = frozenset({"die", "der", "das", "den", "nummer", "nr"})


@dataclass
class CartItem:
    menu_item_id: str
    number: str
    name: str
    quantity: int
    options: list[dict[str, str]] = field(default_factory=list)
    # Mandatory groups without a choice: [{"group": ..., "options": [names]}]
    pending: list[dict[str, Any]] = field(default_factory=list)
    # Note for the kitchen ("ohne Karotten", an allergy), T-4.10.
    note: str | None = None


@dataclass(frozen=True)
class MenuNumbers:
    """What the script knows about the card numbers of the active menu: the
    format for spoken forms ("S zwölf", "Sushi zwölf") and the numbers
    themselves, as search_menu compares them (`canonical_card`). Read from the
    database by the caller (`sim/session.py`), never from this module."""

    card: CardFormat = NO_PREFIXES
    numbers: frozenset[str] = frozenset()

    @classmethod
    def from_items(cls, items: Iterable[tuple[str, str]]) -> "MenuNumbers":
        """From (number, category) of the active dishes."""
        rows = list(items)
        return cls(
            CardFormat.from_items(rows),
            frozenset(canonical_card(number) for number, _ in rows),
        )


class PickupScript:
    """Ein Objekt je Anruf, gehalten vom `ScriptedLLM`.

    `menu` reads the card numbers of the active menu when an answer has to be
    told apart from an order. Without it the script knows no menu: only plain
    numbers by rule A count ("Nummer 12", "die 25g")."""

    def __init__(self, menu: Callable[[], MenuNumbers] | None = None) -> None:
        self._menu = menu or MenuNumbers
        self.cart: list[CartItem] = []
        self.phase = "dishes"  # dishes · choose · option · more · customer
        self._suggestions: list[dict[str, Any]] = []
        self._suggestion_query = ""
        self._suggestion_wish: dict[str, Any] | None = None
        # The order_id whose readback is currently open. After a no it is gone:
        # the conversation state stays readback_pending until a new draft
        # comes, and a later yes would otherwise confirm the discarded order
        # (CLAUDE.md §2 rule 3).
        self.readback_for: str | None = None
        # Further unclear parts of a sentence: one follow-up question after the
        # other, none drops out silently. After the first answer the next one
        # is searched.
        self._later: list[str] = []
        # What should be said before a deferred search ("Gern, Nummer 23."): a
        # turn is a sentence or a tool call, never both.
        self._carry: str | None = None
        # The position for which "Wogegen sind Sie allergisch?" is open: the
        # answer is the ingredient, not a dish (Codex PR #139, P1).
        # Several positions: each one separately, in order (Codex PR #139, P1).
        self._allergy_for: list[CartItem] = []
        # If several are open, each question names its dish.
        self._allergy_named = False
        # What is asked after the allergy: never two questions at once,
        # otherwise "Nummer 12" would be the ingredient (Codex PR #139, P1).
        self._after_allergy: str | None = None
        # The open follow-up question while an answer is being searched again.
        self._reopen: tuple[list[dict[str, Any]], str] | None = None

    # -- Customer turn ------------------------------------------------------

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
                # Out today: do not add it, say so and offer the rest. Otherwise
                # draft_order would reject the cart and the order would never
                # be completed (Codex PR #133).
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
                # Quantity from the answer ("einmal die dreizehn", also without
                # a marker: "zwei Pho Bo", "zwei Nummer dreizehn"), else from
                # the question the suggestions belong to ("zwei Suppen") (Codex
                # PR #133).
                quantity = _stated_quantity(text, hit) or _quantity(
                    self._suggestion_query, hit
                )
                wish = self._add(
                    hit, self._suggestion_query, quantity, self._suggestion_wish
                )
                lead = _wish_sentence(hit, wish, text)
                # Only here is the follow-up question answered. Cleared in _add,
                # a clear part in the same sentence lost the open question
                # (Codex PR #130).
                self._suggestions = []
                self.phase = "dishes"
                return self._next(slots, patch, lead=lead)
            # "Nein, das wars" or "keine davon": the suggestions are discarded.
            # Searched again, the words found no dish, and the same question
            # would come back forever (Codex PR #133).
            if _finishes(text) or _rejects(text):
                self._suggestions = []
                self.phase = "customer" if _finishes(text) and self.cart else "dishes"
                return self._next(slots, patch)
            # None of the offered ones: another dish, search again with its own
            # quantity - putting the one from the question on it would be a
            # guess (review PR #133). That settles the follow-up question
            # (Codex PR #130, P1) - unless the search finds nothing: then it is
            # asked again instead of the position vanishing silently
            # (on_search_failed).
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
        # A correction to the cart that was read back is not guessed but taken
        # anew: the old draft stays a draft and is never confirmed.
        self.readback_for = None
        self.cart = []
        self.phase = "dishes"
        return LLMTurn(say=SAY_RESTART)

    # -- Tool result -------------------------------------------------------

    def on_search(
        self,
        query: str,
        data: dict[str, Any],
        slots: dict[str, Any],
        say: str | None = None,
    ) -> LLMTurn:
        """`say` repeats what was clearly understood (from the code, Maxi PR #127);
        the script speaks it like a model that follows the rule in the prompt."""
        reopen, self._reopen = self._reopen, None
        if data.get("match_type") == "positions":
            unclear: str | None = None
            # "heute aus" is a statement, not a question: it goes along
            # directly, without a second search (review PR #133).
            sold_out: list[str] = []
            for part in data["positions"]:
                if not part["ok"]:
                    # Every unclear part is asked about, one after the other
                    # (Codex PR #130, P1).
                    if unclear:
                        self._later.append(part["query"])
                    else:
                        unclear = part.get("say")
                    continue
                if unclear and part.get("match_type") not in CLEAR_MATCHES:
                    self._later.append(part["query"])
                    continue
                # Every clear part goes into the cart, also after a follow-up
                # question; only the first one is spoken (Codex PR #130, P1).
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
            # The new search hit one of the offered dishes: the wish from the
            # question applies to it, otherwise an allergy would silently drop
            # out (review PR #139).
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
        """The wish of the open question, if the new search clearly found one of
        the offered dishes and carries no wish itself."""
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
        """The answer to a follow-up question found nothing: the question still
        stands. The general message ("nicht gefunden, Nummer?") would put a
        second question next to it; a precise one ("Nummer 99 habe ich nicht")
        goes along. None if no follow-up question was open."""
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

    # -- internal ----------------------------------------------------------

    def _take(self, query: str, found: dict[str, Any]) -> str | None:
        """Takes a clear hit into the cart. Otherwise the sentence that must be
        said: sold out (from the code) or the follow-up question for several."""
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
            # A wish with several hits is classified after the choice.
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
        """The wish from search_menu on the cart (T-4.10): an option of the menu
        is chosen (the mandatory question of that group is dropped), a note and
        an allergy go into `note`. What the menu does not know stays out - the
        sentence for it already came from the code (D8)."""
        if wish is None:
            return None
        if wish["kind"] == "open":
            groups = [
                OptionGroup.model_validate(g) for g in hit.get("option_groups", [])
            ]
            wish = classify_wish(wish["text"], groups).model_dump()
        if wish.get("note"):
            # Leaving out in addition to an extra ("ohne Zwiebeln, dafür mit Huhn").
            item.note = wish["note"]
        if wish["kind"] == "option":
            item.options.append({"group": wish["group"], "name": wish["option"]})
            item.pending = [g for g in item.pending if g["group"] != wish["group"]]
        elif wish["kind"] == "allergy" and not wish.get("ingredient"):
            # The ingredient is missing: the question stays open until the answer comes.
            self._allergy_for.append(item)
            self._allergy_named = self._allergy_named or len(self._allergy_for) > 1
        elif wish["kind"] == "note" or (
            wish["kind"] == "allergy" and wish.get("ingredient")
        ):
            # The allergy in the fixed wording (E14); without an ingredient the sentence asks back.
            item.note = wish["text"]
        return wish

    def _pick_suggestion(self, text: str) -> dict[str, Any] | None:
        """Only an unambiguous mention counts: the number, or a name that
        matches exactly one of the offered ones."""
        lowered = text.lower()
        # Also spoken ("die dreizehn") and with a leading zero (review PR #133).
        # Only if the sentence is the number itself: in "zwei Pho Bo" the two is
        # a quantity, not card 2 - the same rule as in the search (Codex PR
        # #133, P2).
        # Prefixes of the offered numbers ("S1 oder SM1?" - "SM eins", T-4.12).
        offered = CardFormat.from_items((h["number"], "") for h in self._suggestions)
        ref, _ = sole_item_number(text, offered)
        if ref is not None:
            cards = {canonical_card(c) for c in ref.cards}
            by_number = [
                h for h in self._suggestions if canonical_card(h["number"]) in cards
            ]
            if len(by_number) == 1:
                return by_number[0]
        by_name = [h for h in self._suggestions if h["name"].lower() in lowered]
        if len(by_name) == 1:
            return by_name[0]
        # "Meinen Sie Nummer 23?" - "Ja, genau": a yes takes the one suggestion.
        # Searched again, "Ja, genau" found no dish, and the same question came
        # back forever (eval suite T-5.2). Only after number and name, and never
        # if the sentence names another number: "Ja, aber lieber die 24" means
        # the 24. With several suggestions a yes is not a choice.
        if len(self._suggestions) == 1 and ref is None and _agrees(text):
            return self._suggestions[0]
        return None

    def _answer_allergy(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        """Die Antwort auf "Wogegen?": die Zutat im festen Wortlaut (E14)."""
        # A whole answer ("ich bin gegen Erdnuesse allergisch") carries its
        # ingredient itself; only the bare word ("Erdnuesse", "gegen
        # Erdnuesse") gets the sentence opening (Codex PR #139, P1).
        wish = classify_wish(text, [])
        if wish.kind != "allergy" and _orders_something(text, self._menu()):
            # "Eine Cola bitte", "Nummer 12": not an ingredient - the question
            # stays open instead of noting "Keine Eine Cola" (review PR #139).
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        if wish.kind != "allergy":
            # "Keine Erdnuesse" would otherwise give "Keine Keine Erdnuesse".
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
            # Only the question about the allergy, no second one next to it. If
            # it is already in the search's sentence, not again.
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
            # This is always the follow-up question with the suggestions (_take).
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
    """After the choice from suggestions the wish is classified first - and said
    as in the search: an option or a note repeated as well, the unknown
    rejected, the allergy without a promise (Codex PR #139)."""
    if wish is None:
        return None
    menu_hit, settled = MenuHit.model_validate(hit), Wish.model_validate(wish)
    echo = (
        None
        if settled.kind == "unknown"
        else say_understood([("alias", menu_hit, settled)], said)
    )
    return _join(echo, say_for_wish(menu_hit, settled)) or None


# What an order starts with, never an ingredient ("eine Cola", "Nummer 12").
_ORDER_LEADS = frozenset({"und", "ein", "eine", "einen", "einmal", "nummer", "noch"})


def _orders_something(text: str, menu: MenuNumbers) -> bool:
    """Is the answer to "Wogegen?" an order instead of an ingredient?

    The active menu decides what a card number is, not the import grammar
    (Codex PR #155, P2): "S12" and "Sushi zwölf" are numbers only where the
    menu has the prefix S, so "B12", "Vitamin B12" and the additives "E621"
    and "E 621" stay the ingredient. An order is

    - a sentence that names a number by rule A in the menu's card format
      ("S12", "S zwölf", "Nummer 12", "die 25g", also one the menu lacks or a
      number next to more: "S13", "12 oder 13", "S12 Lachs"),
    - any word that is a number on the menu ("die 12 mit Reis", "Milch und
      S12"), or
    - a sentence that opens like an order ("Eine Cola bitte").

    A number word or a number the menu does not have, next to another word,
    belongs to the ingredient ("Fünf-Gewürze-Pulver", "Zwei Sachen: Milch",
    "Gegen E 621").
    """
    words = re.findall(r"[^\W_]+", text.lower())
    ref, unclear = sole_item_number(text, menu.card)
    return (
        ref is not None
        or unclear
        or any(canonical_card(w) in menu.numbers for w in words)
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
    """The quantity that was said, or None. By the domain's rule (numberwords):
    with a marker ("zweimal", "2 x") it always applies. If the sentence is the
    card number itself ("die 7", "die sieben", "die 23 a"), a leading number
    word is a quantity only if it is not the number ("zwei Nummer 23"). Next to
    a name it is a quantity ("zwei Frühlingsrollen") - unless the dish that
    was found explains it: its own number with an article in front ("die 23,
    Frühlingsrollen") or a name that itself starts with the number word ("Acht
    Schätze") (review PR #133)."""
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
    # Folded as in the number word parser: "fuenf" and "fünf" are one word
    # (Codex PR #133, P2).
    return re.findall(r"[^\W_]+", fold(text))


def _option_question(item: CartItem, group: dict[str, Any]) -> str:
    offer = " oder ".join(group["options"])
    return f"Welche Auswahl bei {group['group']} möchten Sie zu {item.name}: {offer}?"


# As an answer to a follow-up question: "keine davon", "nein" discards the suggestions.
REJECT_WORDS = ("nein", "keine", "keins", "keinen", "weder")


def _finishes(text: str) -> bool:
    """Done with the dishes, except for the bare "nein": as an answer to a
    follow-up question that only discards the suggestions."""
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(p)}\b", lowered) for p in DONE_PHRASES if p != "nein"
    )


# No "gern": "Ich haette gern die 24" is an order, not a yes.
_AGREE = re.compile(r"\b(ja|jawohl|genau|richtig|stimmt|korrekt)\b")
# "stimmt nicht", "nicht richtig", "ja, aber ...": not a yes to the suggestion.
_DOUBT = re.compile(r"\b(nein|nicht|aber|lieber|sondern|anders)\b")


def _agrees(text: str) -> bool:
    """A yes without doubt: "Ja, genau" takes the suggestion, "Das stimmt nicht" does not."""
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
