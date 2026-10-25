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

from api.agent.llm import LLMError, LLMTurn, ToolCall
from api.domain.menu.normalize import normalize_query
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
from api.domain.menu.wishes import (
    ALLERGY_NOTE,
    classify_wish,
    denies_allergy,
    kitchen_note,
    removal_with,
)
from api.schemas.menu import MenuHit, OptionGroup, Wish
from api.schemas.orders import NOTE_MAX

SAY_WHAT = "Was möchten Sie bestellen?"
# Where a part of an answer to "Wogegen?" begins: "gegen" or "auf" at the
# start or after "und", "aber", "sondern" ("Ohne Zwiebeln und auf
# Erdnuesse", "Ohne Zwiebeln, aber gegen Erdnuesse", Codex PR #330),
# "gegen" after a comma. An "auf" after a comma still ends the list
# ("Erdnuesse, auf jeden Fall"), and "auf jeden/keinen Fall" is no "auf".
_AGAINST = re.compile(
    r"^\s*(?:gegen|auf(?!\s+(?:jeden|keinen)\s+fall\b))\b|,\s*gegen\b"
    r"|\b(?:und|aber|sondern)\s+(?:gegen|auf(?!\s+(?:jeden|keinen)\s+fall\b))\b",
    re.IGNORECASE,
)
_BUT = re.compile(r"\baber\b", re.IGNORECASE)
# Before a removal in an answer: "Bitte ohne Zwiebeln", "Und ohne Zwiebeln".
_ANSWER_FILLERS = frozenset({"bitte", "aber", "und", "dann", "dafuer"})
# An answer to "Wogegen?" that names nothing: "Keine.", "Gar keine.", "Gegen
# nichts." ("Nein." stays a question: no to what is unclear).
_NOTHING = frozenset(
    {"kein", "keine", "keinen", "keinem", "keiner", "keinerlei", "nichts"}
)
_NOTHING_FILL = frozenset(
    {"gar", "gegen", "auf", "doch", "ueberhaupt", "nein", "danke"}
)
SAY_MORE = "Darf es noch etwas sein?"
SAY_NAME = "Auf welchen Namen darf ich die Bestellung notieren?"
SAY_PHONE = "Unter welcher Telefonnummer sind Sie erreichbar?"
SAY_RESTART = "Dann noch einmal von vorn: was möchten Sie bestellen?"

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
        # The position, its hit, the wish and the question for which "Meinen
        # Sie Reis bei Beilage oder bei Extra?" is open: the answer is the
        # group, not a dish (Codex PR #139).
        self._group_for: list[tuple[CartItem, dict[str, Any], dict[str, Any], str]] = []
        # The open follow-up question while an answer is being searched again.
        self._reopen: tuple[list[dict[str, Any]], str] | None = None
        # A correction at the read-back that is being looked up: the cart line
        # it replaces and the quantity, if the guest said one.
        self._replace: tuple[int, int | None] | None = None
        # A corrected order waits for the mandatory choice of its new dish:
        # with the answer it is complete again and goes back to the read-back.
        self._complete = False

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
        if self._group_for:
            return self._answer_group(text, slots, patch)
        if self.phase == "choose":
            # "Nein, die 13": what follows the no can still name one of the
            # offered dishes - a choice with the quantity and the wish of the
            # question, not a new search (review PR #178).
            other = None if _finishes(text) else _replacement(text)
            hit = self._pick_suggestion(text) or (
                self._pick_suggestion(other, agree=False) if other else None
            )
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
                # "keine davon, lieber Frühlingsrollen": only the rejection is
                # settled, the dish named with it is searched (Codex PR #133).
                if other:
                    self.phase = "dishes"
                    return _search(other, patch)
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

    def on_readback(self, yes: bool, text: str = "") -> LLMTurn:
        order_id = self.readback_for
        if yes and order_id:
            return LLMTurn(
                tool_call=ToolCall(
                    "confirm", {"entity": "order", "entity_id": order_id}
                )
            )
        # After a no the draft that was read back stays a draft and is never
        # confirmed (rule 3), whatever follows.
        self.readback_for = None
        fix = self._correction(text)
        if fix is None:
            # A correction that is not one clear number is not guessed but taken
            # anew.
            return self._restart()
        # "Falsch, ich meinte die Nummer 25c": that one dish is looked up and
        # replaced, the rest of the order stays, and the new order is read back
        # before any yes (Maxi, 12.10.2026).
        at, card, quantity = fix
        self._replace = (at, quantity)
        return _search(f"Nummer {card}", {})

    def _numbers(self) -> MenuNumbers:
        """The card numbers of the active menu, read while a sentence of the
        guest is parsed, outside any tool call. If they cannot be read, that is
        an outage like a model that does not answer: the loop hands the call
        to the team on `LLMError` (rule 5, Codex PR #271), where any other
        exception would end the call."""
        try:
            return self._menu()
        except Exception as exc:
            raise LLMError("the card numbers of the menu could not be read") from exc

    def _restart(self) -> LLMTurn:
        self.cart = []
        self.phase = "dishes"
        return LLMTurn(say=SAY_RESTART)

    def _correction(self, text: str) -> tuple[int, str, int | None] | None:
        """The cart line a no at the read-back replaces, the card number named
        instead, and the quantity if one was said. Only where it is no guess:
        behind the no the sentence is one card number and nothing else (rule A,
        `sole_item_number`), so "um 13 Uhr" or "in 24 Minuten" name no dish; no
        word adds a dish or takes one away ("und noch die 13", "und die 13",
        "die 24 nicht");
        and there is exactly one line the number can stand for - the one with
        the same digits ("25c" for a 25d that was read back, "S12" for the 12),
        else the only line of the order. A number that went through a heard
        letter ("ist zwölf") names nothing."""
        said = _CORRECTION_LEAD.sub("", text)
        words = _words(said)
        # "und die 13" continues the order; inside a number ("drei und
        # zwanzig") the word adds nothing (Codex PR #271).
        if _CORRECTION_STOP & set(words) or words[:1] == ["und"]:
            return None
        ref, unclear = sole_item_number(said, self._numbers().card)
        if unclear or ref is None or not ref.valid or len(ref.cards) != 1 or ref.heard:
            return None
        named = canonical_card(ref.text)
        if any(canonical_card(item.number) == named for item in self.cart):
            return None
        lines = [
            i
            for i, item in enumerate(self.cart)
            if _digits(item.number) == _digits(named)
        ]
        if len(lines) != 1:
            lines = [0] if len(self.cart) == 1 else []
        return (lines[0], ref.text, _correction_quantity(said)) if lines else None

    def _replaced(
        self,
        replace: tuple[int, int | None],
        data: dict[str, Any],
        slots: dict[str, Any],
        say: str | None = None,
    ) -> LLMTurn:
        """The search for a correction came back: the one dish takes the place
        of the line, with its quantity and its note for the kitchen. Options
        belong to the old dish and go; a mandatory choice of the new one is
        asked, and its answer leads back to the read-back (`_next`). Anything
        but one clear dish starts the order again; a dish that is out today
        with the sentence the search wrote for it."""
        at, quantity = replace
        hits = data.get("results") or []
        if data.get("match_type") not in CLEAR_MATCHES or len(hits) != 1:
            return self._restart()
        if hits[0].get("sold_out"):
            turn = self._restart()
            return LLMTurn(say=_join(say, turn.say))
        old = self.cart[at]
        self._add(hits[0], "", quantity or old.quantity)
        new = self.cart.pop()
        new.note = new.note or old.note
        self.cart[at] = new
        # The order was complete before the correction: once the new dish has
        # its mandatory choice, it is drafted again instead of "Darf es noch
        # etwas sein?" (Codex PR #271).
        self._complete = True
        return self._next(slots, {})

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
        if self._replace is not None:
            replace, self._replace = self._replace, None
            return self._replaced(replace, data, slots, say)
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
            if (self._allergy_for or self._group_for) and unclear:
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
        goes along. None if no follow-up question was open.

        A correction at the read-back that names a number the menu does not
        have starts the order again; a lookup that failed without a sentence
        for the guest is answered with None as well."""
        if self._replace is not None:
            self._replace = None
            # Without a sentence for the guest the search did not answer at all:
            # that is an outage, and the caller hands the call to the team
            # (rule 5, Codex PR #271). The order stays as it was read back.
            if not say:
                return None
            # "Die Nummer 99 habe ich nicht auf der Karte": the guest hears why
            # the correction was not taken, as with a dish that is out.
            turn = self._restart()
            return LLMTurn(say=_join(_statement(say), turn.say))
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
            classified = classify_wish(wish["text"], groups)
            if classified is None:
                return None
            wish = classified.model_dump()
        if wish.get("note"):
            # Leaving out in addition to an extra ("ohne Zwiebeln, dafür mit Huhn").
            item.note = wish["note"]
        if wish["kind"] == "option":
            item.options.append({"group": wish["group"], "name": wish["option"]})
            item.pending = [g for g in item.pending if g["group"] != wish["group"]]
        elif wish["kind"] == "open" and wish.get("groups"):
            # The option stands in several groups: the question stays open
            # until the answer names one, none is chosen here.
            question = say_for_wish(
                MenuHit.model_validate(hit), Wish.model_validate(wish)
            )
            self._group_for.append((item, hit, wish, question or ""))
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

    def _pick_suggestion(
        self, text: str, *, agree: bool = True
    ) -> dict[str, Any] | None:
        """Only an unambiguous mention counts: the number, or a name that
        matches exactly one of the offered ones. `agree=False`: a yes does not
        count, for what follows a rejection."""
        lowered = text.lower()
        # Also spoken ("die dreizehn") and with a leading zero (review PR #133).
        # Only if the sentence is the number itself: in "zwei Pho Bo" the two is
        # a quantity, not card 2 - the same rule as in the search (Codex PR
        # #133, P2).
        # Prefixes of the offered numbers ("S1 oder SM1?" - "SM eins", T-4.12).
        offered = CardFormat.from_items((h["number"], "") for h in self._suggestions)
        ref, _ = sole_item_number(text, offered)
        # "Ist eins" again after "Meinen Sie Nummer S1 ...?" is the same open
        # question: 1 or S1 by the letters of the menu, whatever was offered. A
        # number that went through a heard letter picks nothing; the name or a
        # yes does (Codex PR #257).
        on_menu, _ = sole_item_number(text, self._numbers().card)
        if ref is not None and not (on_menu is not None and on_menu.heard):
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
        if agree and len(self._suggestions) == 1 and ref is None and _agrees(text):
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
        if (wish is None or wish.kind != "allergy") and _orders_something(
            text, self._numbers()
        ):
            # "Eine Cola bitte", "Nummer 12", also "Die 23 noch, ich habe keine
            # Allergie": not an ingredient - the question stays open instead
            # of noting "Keine Eine Cola" (review PR #139).
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        # "Keine, aber ohne Zwiebeln": nothing, and a removal (Codex PR #330).
        but = _BUT.search(text)
        nothing = _withdraws(text[: but.start()] if but else text)
        behind = classify_wish(text[but.end() :], []) if but and nothing else None
        if behind is not None and behind.kind == "allergy":
            # "Keine, aber ich bin gegen Sesam allergisch", "Keine, aber meine
            # Frau ...": whose allergy and for which dish is not clear, so
            # the question is asked again (#207, Codex PR #361).
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        if wish is None or nothing or (wish.kind != "allergy" and denies_allergy(text)):
            # "Ich habe doch keine Allergie", "Keine.", "Gegen nichts.": the
            # question is closed, nothing goes to the kitchen; a removal said
            # with it is noted (issue #252).
            item = self._allergy_for.pop(0)
            if behind is not None:
                item.note = _with_removal(item.note, removal_with(behind))
            if wish is not None and not nothing:
                # A removal said with it is noted. An addition is not taken:
                # the answer is read without the dish's options (stand-in
                # only, docs/01_STATUS.md).
                item.note = _with_removal(item.note, removal_with(wish))
            if not self._allergy_for:
                self._allergy_named = False
            return self._next(slots, patch, lead=self._take_deferred())
        if wish.kind != "allergy":
            # "Keine Erdnuesse" would otherwise give "Keine Keine Erdnuesse".
            # A removal said with it is no ingredient ("Ohne Zwiebeln und
            # gegen Erdnuesse"), nor is the "gegen" in front of one (#252).
            bare, removal = _ingredient_answer(text)
            item = self._allergy_for[0]
            # Also a removal behind the ingredient ("Erdnuesse, aber ohne
            # Zwiebeln"), which the answer read as a wish already has.
            for said in (removal, wish.note):
                item.note = _with_removal(item.note, said)
            wish = classify_wish(f"allergisch gegen {bare}", [])
        if wish is None or not wish.ingredient:
            # A removal said with an answer that names no ingredient yet stays.
            if wish is not None:
                item = self._allergy_for[0]
                item.note = _with_removal(item.note, removal_with(wish))
            return LLMTurn(
                say=self._allergy_question(),
                state_patch=patch or None,
                understanding_failure="allergy",
            )
        item = self._allergy_for.pop(0)
        # A removal said together with the allergy is in the note already and
        # follows it (issue #252).
        # Rebuilt from the allergy alone, so a removal said again is noted
        # once (#252).
        item.note = kitchen_note(
            _with_removal(item.note, removal_with(wish)),
            ALLERGY_NOTE.format(ingredient=wish.ingredient),
        )
        lead = SAY_ALLERGY_NOTE.format(name=item.name)
        if not self._allergy_for:
            self._allergy_named = False
        return self._next(slots, patch, lead=_join(lead, self._take_deferred()))

    def _allergy_question(self) -> str:
        names = [i.name for i in self._allergy_for]
        return allergy_question(names, self._allergy_named) or ""

    def _take_deferred(self) -> str | None:
        """The question that was held back, once no other one is open."""
        if self._allergy_for or self._group_for:
            return None
        deferred, self._after_allergy = self._after_allergy, None
        return deferred

    def _answer_group(
        self, text: str, slots: dict[str, Any], patch: dict[str, Any]
    ) -> LLMTurn:
        """The answer to "Meinen Sie Reis bei Beilage oder bei Extra?": the one
        group it names. A bare no drops the option, the dish stays as it is on
        the menu. Anything else leaves the question open - no group is chosen
        for the guest (CLAUDE.md §2 rule 2)."""
        item, hit, wish, question = self._group_for[0]
        said = set(_words(text))
        named = [g for g in wish["groups"] if set(_words(g)) <= said]
        rejected = _rejects(text) or "nicht" in said
        chosen = len(named) == 1 and not rejected
        dropped = rejected and not named
        if not (chosen or dropped):
            return LLMTurn(
                say=question,
                state_patch=patch or None,
                understanding_failure="option",
            )
        self._group_for.pop(0)
        lead = None
        if chosen:
            # Classified again against the one group: the option and its
            # surcharge come from the menu and are said like any other option
            # (Codex PR #178).
            groups = [g for g in hit["option_groups"] if g["group"] == named[0]]
            settled = self._apply_wish(
                item,
                {**hit, "option_groups": groups},
                {"kind": "open", "text": wish["text"]},
            )
            lead = _wish_sentence(hit, settled, text)
        return self._next(slots, patch, lead=_join(lead, self._take_deferred()))

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
        if self._allergy_for or self._group_for:
            # Only the question about the allergy or the group, no second one
            # next to it. If it is already in the search's sentence, not again.
            question = (
                self._allergy_question() if self._allergy_for else self._group_for[0][3]
            )
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
        if self._complete and not any(i.pending for i in self.cart):
            self._complete = False
            self.phase = "customer"
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


# How a correction at the read-back opens ("Nein, ...", "Falsch, ..."); what
# follows has to be the card number alone.
_CORRECTION_LEAD = re.compile(r"^\W*(?:(?:nein|nee|falsch)\W+)+", re.IGNORECASE)
# One more dish, or one less, is no replacement of the dish that was read back.
_CORRECTION_STOP = frozenset(
    {
        "noch",
        "auch",
        "dazu",
        "zusaetzlich",
        "ausserdem",
        "nicht",
        "ohne",
        "kein",
        "keine",
    }
)


def _statement(say: str) -> str:
    """What a search says without the question it ends on ("Können Sie das noch
    einmal sagen?"): the restart asks its own, and two questions in a row leave
    the guest with nothing to answer."""
    sentences = re.split(r"(?<=[.!?])\s+", say.strip())
    kept = [s for s in sentences if not s.endswith("?")]
    return " ".join(kept) or say


def _correction_quantity(said: str) -> int | None:
    """The quantity said with a correction: marked ("zweimal", "2 x"), or bare
    directly before the marker ("zwei Nummer 24", Codex PR #271). Not
    `_stated_quantity`: that takes the "drei" of "die drei und zwanzig" for a
    quantity, which is the number itself."""
    marked = find_quantity(said)
    if marked:
        return marked
    words = _words(said)
    at = next((i for i, w in enumerate(words) if w in ("nummer", "nr")), 0)
    return parse_cardinal(words[at - 1]) if at else None


def _digits(card: str) -> str:
    """The number of a card number without its letters: "25c" and "25d", "s12"
    and "12" are one family for a correction at the read-back."""
    return re.sub(r"\D", "", canonical_card(card))


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
    Frühlingsrollen") or a name that itself starts with the number ("Acht
    Schätze", also "8 Schätze" found as "acht schaetze": the values are
    compared, not the spelling) (review and Codex PR #133)."""
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
        if name and parse_cardinal(name[0]) == lead:
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


# What stands between a rejection and the dish named instead; none of it names
# a dish ("keine davon, lieber ...", "weder noch, dann ...", "nein, vielen
# Dank": courtesy alone is no replacement, Codex PR #178).
_REJECTION_FILL = frozenset(
    {"davon", "noch", "lieber", "sondern", "dann", "aber", "stattdessen"}
    | {"danke", "dank", "vielen", "schön", "schoen", "sehr", "bitte"}
)


def _replacement(text: str) -> str | None:
    """The dish named after a leading rejection ("keine davon, lieber
    Frühlingsrollen" -> "Frühlingsrollen"), or None if the answer only rejects.
    "Keine Suppe" and "weder die 12 noch die 13" negate the dish itself, and a
    second no ("nein, nicht die", "nein, lieber nichts") leaves nothing to
    search: they stay a rejection."""
    words = list(re.finditer(r"[^\W_]+", text))
    said = [w.group().lower() for w in words]
    if not said or said[0] not in REJECT_WORDS:
        return None
    at = next(
        (i for i, w in enumerate(said) if w not in (*REJECT_WORDS, *_REJECTION_FILL)),
        None,
    )
    if at is None or said[at - 1].startswith(("kein", "weder")):
        return None
    if any(w in (*REJECT_WORDS, "nicht", "nichts") for w in said[at:]):
        return None
    rest = text[words[at].start() :]
    return rest if normalize_query(rest) or re.search(r"\d", rest) else None


def _is_done(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in DONE_PHRASES)


def _join(*parts: str | None) -> str:
    return " ".join(p for p in parts if p)


def _withdraws(text: str) -> bool:
    """ "Keine.", "Gar keine.", "Gegen nichts.": no allergy after all."""
    words = set(_words(text))
    return bool(words & _NOTHING) and words <= _NOTHING | _NOTHING_FILL


def _ingredient_answer(text: str) -> tuple[str, str | None]:
    """An answer to "Wogegen?" in its parts: the ingredient, and a removal
    said with it. "Ohne Zwiebeln und gegen Erdnuesse" -> ("Erdnuesse", "Ohne
    Zwiebeln"); "Erdnuesse und gegen Sesam" -> ("Erdnuesse und Sesam", None);
    "Keine Erdnuesse" -> ("Erdnuesse", None)."""
    parts = [part.strip(" ,.") for part in _AGAINST.split(text)]
    parts = [part for part in parts if part]
    # "Bitte ohne Zwiebeln": the removal behind a polite filler (Codex PR #330).
    removals = [
        p
        for p in parts
        if len(parts) > 1
        and [w for w in _words(p) if w not in _ANSWER_FILLERS][:1] == ["ohne"]
    ]
    bare = " und ".join(p for p in parts if p not in removals)
    # "Keine Erdnuesse", also "Ohne Zwiebeln." alone: the ingredient.
    bare = re.sub(r"^\s*(?:keine[nm]?|kein|ohne)\s+", "", bare, flags=re.IGNORECASE)
    return bare, ", ".join(removals) or None


def _with_removal(note: str | None, removal: str | None) -> str | None:
    """The note with a removal added, each removal once, within an order line."""
    said = [n for n in (note or "").split(", ") if n]
    known = {n.lower() for n in said}
    new = [r for r in (removal or "").split(", ") if r and r.lower() not in known]
    if not new:
        return note
    joined = ", ".join([*said, *new])
    return joined if len(joined) <= NOTE_MAX else note
