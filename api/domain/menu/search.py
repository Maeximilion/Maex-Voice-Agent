"""search_menu: from what was said to the menu_item_id (docs/04 §search_menu, T-4.3).

The resolution order from docs/04, strictly in this order:

1. Number -> exact hit on the card number (`exact_number`), but only if the
   whole sentence is exactly one number (rule A, numberwords.
   sole_item_number). If there is more next to it - a second number, a name,
   "oder" - that is `ambiguous`, asking for the one number. If the guest names
   a number that does not exist, that is `not_found`; the search then does not
   fall back to similar names (CLAUDE.md §2 rule 2).
2. Exact alias (`alias`). If the same alias is attached to several dishes,
   that is `ambiguous` - the importer warned about it, the search asks back.
3. Fuzzy over name and aliases (pg_trgm): exactly one hit above the high
   threshold -> `fuzzy_single`; otherwise all above the low one -> `ambiguous`
   with at most three suggestions; none -> `not_found`.

Only active dishes are searched. Sold-out ones come back with `sold_out: true`
and a sentence: the agent should say it is out today instead of acting as if
the dish did not exist.

The fuzzy search runs in two steps: first a prefilter with the operators `%`
and `<%`, which uses the GIN indexes on `menu_items.name` and
`item_aliases.alias`, then the exact scores only on the hits. The prefilter
compares against the session thresholds, which are set to the same low
threshold for this - so it matches the condition and cuts nothing off. With a
few hundred rows the difference is small; as the menu grows, the index carries
the search.
"""

import uuid
import zlib
from collections.abc import Callable
from datetime import datetime
from functools import partial

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Ambiguous, NotFound
from api.core.time import utcnow
from api.domain.menu.items import card_format, option_groups
from api.domain.menu.items import is_sold_out as _sold_out
from api.domain.menu.normalize import normalize_alias, normalize_query
from api.domain.menu.numberwords import (
    CardFormat,
    canonical_card,
    sole_item_number,
)
from api.domain.menu.sold_out import alternatives
from api.domain.menu.split import raw_pieces, separator_pieces, split_positions
from api.domain.menu.wishes import (
    classify_wish,
    has_number,
    names_it,
    open_wish,
    opens_with_allergy,
    wish_candidates,
)
from api.models import ItemAlias, MenuItem
from api.schemas.menu import MenuHit, SearchResult, Wish

SAY_NOT_FOUND = (
    "Das habe ich auf der Karte nicht gefunden. Können Sie mir die Nummer sagen?"
)
SAY_NO_SUCH_NUMBER = (
    "Die Nummer {number} habe ich nicht auf der Karte. Können Sie das noch "
    "einmal sagen?"
)
SAY_WHICH_NUMBER = "Welche Nummer meinen Sie? Bitte sagen Sie mir nur die eine Nummer."
SAY_SOLD_OUT = "{name} ist heute leider aus."
# Alternative from the same category, only what is available today (T-4.8, D8).
SAY_ALTERNATIVES = "Stattdessen hätte ich {items}."
# Several positions in one sentence: the guest may do that and does not have to
# repeat anything (Maxi, PR #127). Over HTTP the caller then asks per part.
SAY_IN_TURN = "Einen Moment, ich nehme das der Reihe nach auf."
# Wishes (T-4.10, D8). What the menu does not know is not offered; the allergy
# goes to the kitchen without a promise. The wording about the allergy is a
# draft and is agreed with the legal check before live operation (docs/09).
SAY_WISH_UNKNOWN = (
    "Den Wunsch „{wish}“ kann ich leider nicht anbieten. {name} nehme ich so auf, "
    "wie es auf der Karte steht."
)
SAY_WISH_WHICH_GROUP = "Meinen Sie {option} bei {groups}?"
SAY_ALLERGY_WHICH = "Wogegen sind Sie allergisch? Das gebe ich an die Küche weiter."
# Several dishes with an allergy but no ingredient: one question after the
# other, each with its dish (Codex PR #139, P1).
SAY_ALLERGY_WHICH_FOR = (
    "Wogegen sind Sie bei {name} allergisch? Das gebe ich an die Küche weiter."
)
SAY_ALLERGY_NOTE = (
    "Ihren Hinweis zur Allergie gebe ich an die Küche weiter. Ob {name} frei davon "
    "ist, kann ich Ihnen nur sagen, wenn es bei uns hinterlegt ist."
)
SAY_UNDERSTOOD = (
    "Gern, {items}.",
    "Alles klar, {items}.",
    "Notiert: {items}.",
    "{items}, sehr gern.",
)
AMBIGUOUS_LIMIT = 3
# Distance of the session threshold from the actual threshold, so the prefilter
# safely stays a superset. Small enough not to fetch a real extra row, large
# enough for the comparison in float4.
PREFILTER_EPSILON = 1e-4


def _active(tenant_id: uuid.UUID) -> tuple:
    return (MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True))


def _by_number(
    session: Session, tenant_id: uuid.UUID, spoken: tuple[str, ...] | list[str]
) -> list[MenuItem]:
    """Active dishes for the card numbers that were said.

    The text is compared without leading zeros of the number: "07" finds 7,
    "acht" finds 08, "S7" finds S07 (numberwords.canonical_card, here in SQL).
    The card number is text (docs/14); converting via int would lose "23a"
    (Codex PR #116). Number, letter and marker come from the same place in the
    sentence (Codex PR #117). Several numbers come from a category word
    ("Sushi zwölf" is S12 or SM12, T-4.12).
    """
    if isinstance(spoken, str):
        # A str would be looked up character by character (review PR #155).
        raise TypeError("pass card numbers as a sequence, not as a str")
    stored = func.regexp_replace(
        func.lower(func.btrim(MenuItem.number)), "^([a-z]*)0*([0-9])", "\\1\\2"
    )
    return list(
        session.scalars(
            select(MenuItem)
            .where(
                *_active(tenant_id),
                stored.in_(sorted({canonical_card(n) for n in spoken})),
            )
            .order_by(MenuItem.number)
        )
    )


def _hits(session: Session, items: list[MenuItem], now: datetime) -> list[MenuHit]:
    groups = option_groups(session, [i.id for i in items])
    return [
        MenuHit(
            menu_item_id=item.id,
            number=item.number,
            name=item.name,
            price_cents=item.price_cents,
            sold_out=_sold_out(item, now),
            option_groups=groups.get(item.id, []),
        )
        for item in items
    ]


def _single(
    session: Session, match_type: str, item: MenuItem, now: datetime
) -> SearchResult:
    say = _sold_out_say(session, item, now) if _sold_out(item, now) else None
    return SearchResult(
        match_type=match_type, results=_hits(session, [item], now), say=say
    )


def _sold_out_say(session: Session, item: MenuItem, now: datetime) -> str:
    """ "Heute aus" plus up to two dishes of the same category (docs/06 §3: the
    agent names an alternative). Without an alternative only the first sentence."""
    others = alternatives(session, item, now)
    if not others:
        return SAY_SOLD_OUT.format(name=item.name)
    items = " oder ".join(f"Nummer {o.number} {o.name}" for o in others)
    return (
        f"{SAY_SOLD_OUT.format(name=item.name)} {SAY_ALTERNATIVES.format(items=items)}"
    )


def _ambiguous(session: Session, items: list[MenuItem], now: datetime) -> SearchResult:
    names = [f"Nummer {i.number}, {i.name}" for i in items]
    if len(names) == 1:
        question = f"Meinen Sie {names[0]}?"
    else:
        question = f"Meinen Sie {', '.join(names[:-1])} oder {names[-1]}?"
    return SearchResult(
        match_type="ambiguous", results=_hits(session, items, now), say=question
    )


CLEAR_MATCHES = ("exact_number", "alias", "fuzzy_single")


def say_understood(
    understood: list[tuple[str, MenuHit, Wish | None]], said: str
) -> str | None:
    """Repeats at once what was clearly understood - the way a person on the
    phone does (Maxi, PR #127).

    If the guest named a number, only the number comes back ("Nummer 9"). If
    they described the dish ("Süß Sauer mit Ente"), the menu's name comes back
    with the number ("Nummer 25a Ente süß-sauer"): not what was said, but what
    the system made of it - a wrong hit is noticed at once that way. Without
    the quantity; that comes with the readback from draft_order.

    The opening varies so it does not sound like an announcement. It is chosen
    from what was said, not at random: a replay says the same (docs/08).
    `understood` holds match_type, hit and wish; empty means no sentence.

    A wish is repeated as well, so the guest hears that it was noted: "Nummer
    23, ohne Karotten", "Nummer 47 Ente knusprig mit Nudeln, 3 Euro Aufpreis" -
    the surcharge from the menu, never from the model (T-4.10).
    """
    names = [
        _with_wish(
            f"Nummer {hit.number}"
            if match_type == "exact_number"
            else f"Nummer {hit.number} {hit.name}",
            wish,
        )
        for match_type, hit, wish in understood
    ]
    if not names:
        return None
    items = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} und {names[-1]}"
    lead = SAY_UNDERSTOOD[zlib.crc32(said.casefold().encode()) % len(SAY_UNDERSTOOD)]
    return lead.format(items=items)


def _with_wish(item: str, wish: Wish | None) -> str:
    # Unknown has its own sentence, open is not decided yet, and the allergy is
    # in the sentence after it (SAY_ALLERGY_NOTE) - here only the dish.
    if wish is None or wish.kind in ("unknown", "open", "allergy"):
        return item
    if wish.kind != "option":
        return f"{item}, {wish.text}"
    # Import here: ordering imports search (validation); at the top it would be circular.
    from api.domain.ordering.readback import spoken_euro

    delta = wish.price_delta_cents or 0
    text = (
        f"{item}, {wish.note}, mit {wish.option}"
        if wish.note
        else f"{item} mit {wish.option}"
    )
    if delta > 0:
        return f"{text}, {spoken_euro(delta)} Aufpreis"
    if delta < 0:
        return f"{text}, {spoken_euro(-delta)} günstiger"
    return text


def position_parts(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
    card: CardFormat | None = None,
) -> list[str]:
    """The positions of a sentence (`_position_parts`). A part that starts with
    an allergy of its own ("und einer Sesamallergie") is not a new position; it
    belongs to the one before (Codex PR #139, P1)."""
    parts = _position_parts(session, tenant_id, query, now, high, low, card)
    return _keep_allergy_clauses(query, parts)


def _keep_allergy_clauses(query: str, parts: list[str]) -> list[str]:
    if len(parts) <= 1 or not any(opens_with_allergy(p) for p in parts):
        return parts
    if opens_with_allergy(parts[0]):
        # At the start of the sentence: it belongs to the first dish after it,
        # as a wish behind the dish (review PR #139).
        rest = _keep_allergy_clauses(query, parts[1:])
        if opens_with_allergy(rest[0]):
            return parts
        return [f"{rest[0]}, {parts[0]}", *rest[1:]]
    spans: list[list[int]] = []
    at = 0
    for part in parts:
        start = query.find(part, at)
        if start < 0:
            return parts
        spans.append([start, start + len(part)])
        at = start + len(part)
    merged = [spans[0]]
    for span, part in zip(spans[1:], parts[1:], strict=True):
        if opens_with_allergy(part):
            merged[-1][1] = span[1]
        else:
            merged.append(span)
    return [query[start:end] for start, end in merged]


def _position_parts(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    now: datetime | None,
    high: float | None,
    low: float | None,
    card: CardFormat | None = None,
) -> list[str]:
    """The positions of a sentence: first by the sentence (`split_positions`),
    then with the menu.

    "die 23 und Pho Bo": without a quantity "Pho Bo" does not open a position,
    the sentence alone would stay whole, and the name search over the whole
    sentence would find only Pho Bo - the 23 would silently drop out (Codex PR
    #127, P1). If every piece at the separators clearly hits a **different**
    dish on its own, these are several positions. If one hits nothing or the
    same dish, the "und" was part of a name ("Ente süß und sauer"), and the
    sentence stays whole.

    Connected pieces come first: if the whole sentence or a part of it is on
    the menu as such ("Fisch und Chips" as an alias or as a name), it is one
    dish, even if "Fisch" and "Chips" are dishes on their own too - also in
    the middle of a list ("Fisch und Chips und Pho Bo", Codex PR #127, P1).
    The search goes from the left, longest piece first.
    """
    parts = split_positions(query)
    if len(parts) > 1:
        return parts
    pieces = raw_pieces(query)
    if len(pieces) <= 1:
        return [query]
    # Read the menu once, not per piece and span (review PR #155).
    search = partial(
        search_menu,
        session,
        tenant_id,
        now=now,
        high=high,
        low=low,
        split_check=False,
        card=card if card is not None else card_format(session, tenant_id),
    )
    spans = _spans(query, pieces)
    # No span can be a dish if it is longer than the longest name or alias on
    # the menu. Without a limit, a list of 20 dishes checked about 190 spans
    # (Codex PR #127, P2); this way it is at most one per piece if the menu has
    # names with an "und", and none if it does not.
    longest = _longest_dish(session, tenant_id)
    positions: list[str] = []
    # Per dish, the words it was named with. "Pho Bo und Pho Bo" are two
    # portions (Codex PR #127, P1); "Pho und Pho Bo" hits the same dish with
    # other words - that can be a clarification, and the sentence stays whole.
    # The same holds for compound dishes: "Fisch und Chips und Backfisch mit
    # Pommes" names the same dish with other words (Codex PR #127, P1).
    said: dict[uuid.UUID, set[str]] = {}
    i = 0
    while i < len(pieces):
        for j in range(min(len(pieces), i + longest) - 1, i, -1):
            text = query[spans[i][0] : spans[j][1]]
            ids = _whole_dish(session, tenant_id, search, text, pieces[i : j + 1])
            if ids:
                if len(ids) == 1:
                    said.setdefault(next(iter(ids)), set()).add(normalize_query(text))
                positions.append(text)
                i = j + 1
                break
        else:
            piece = pieces[i]
            try:
                found = search(piece)
            except (Ambiguous, NotFound):
                return [query]
            if found.match_type not in CLEAR_MATCHES or not found.results:
                return [query]
            said.setdefault(found.results[0].menu_item_id, set()).add(
                normalize_query(piece)
            )
            positions.append(piece)
            i += 1
    if len(positions) == 1 or any(len(words) > 1 for words in said.values()):
        return [query]
    return positions


def _longest_dish(session: Session, tenant_id: uuid.UUID) -> int:
    """Die meisten Stuecke an den Trennern, die ein aktiver Name oder Alias hat."""
    names = session.scalars(select(MenuItem.name).where(*_active(tenant_id)))
    aliases = session.scalars(
        select(ItemAlias.alias)
        .join(MenuItem, ItemAlias.menu_item_id == MenuItem.id)
        .where(*_active(tenant_id))
    )
    return max(
        (separator_pieces(text) for text in [*names, *aliases]),
        default=1,
    )


def _spans(query: str, pieces: list[str]) -> list[tuple[int, int]]:
    """Where each piece is in the sentence, so neighbouring pieces can be put
    back together with their separator, the way the guest said them."""
    spans = []
    start = 0
    for piece in pieces:
        begin = query.index(piece, start)
        start = begin + len(piece)
        spans.append((begin, start))
    return spans


def _whole_dish(
    session: Session,
    tenant_id: uuid.UUID,
    search: Callable[[str], SearchResult],
    query: str,
    pieces: list[str],
) -> set[uuid.UUID]:
    """Is the whole sentence exactly one dish on the menu: an alias or the same
    name? Returns the dishes it hits that way - several for a duplicate alias,
    none if it is not a whole dish.
    Fuzzy does not count - "die 23 und Pho Bo" would fuzzily hit Pho Bo.

    The comparison uses the form of the search (normalize_query): "einmal Fisch
    und Chips, bitte" is the name with a quantity and filler word (Codex PR
    #127, P1). That form also drops numbers; "Pho Bo und die 23" would become
    "pho bo". So every piece must keep content: "die 23" alone is a dish of its
    own, not part of the name. The same holds for the alias: "die 23 und Pho"
    would otherwise hit the alias "Pho" (Codex PR #127, P1).

    An alias also counts if it is attached to several dishes: then the search
    asks over the whole sentence which one is meant, instead of taking the
    pieces as positions (Codex PR #127, P2)."""
    if not all(normalize_query(p) for p in pieces):
        return set()
    by_alias = _alias_items(session, tenant_id, query)
    if by_alias:
        return {item.id for item in by_alias}
    try:
        found = search(query)
    except (Ambiguous, NotFound):
        return set()
    said = normalize_query(query)
    return {
        hit.menu_item_id for hit in found.results if normalize_query(hit.name) == said
    }


def _alias_items(session: Session, tenant_id: uuid.UUID, query: str) -> list[MenuItem]:
    """Aktive Gerichte, deren Alias genau das Gesagte ist (Schritt 2 der Suche)."""
    text = normalize_query(query)
    said = {normalize_alias(query), text}
    matched_ids = {
        item_id
        for item_id, alias in session.execute(
            select(ItemAlias.menu_item_id, ItemAlias.alias)
            .join(MenuItem, ItemAlias.menu_item_id == MenuItem.id)
            .where(*_active(tenant_id))
        )
        if alias in said or normalize_query(alias) == text
    }
    if not matched_ids:
        return []
    return list(
        session.scalars(
            select(MenuItem)
            .where(MenuItem.id.in_(matched_ids))
            .order_by(MenuItem.number)
        )
    )


def say_for_wish(hit: MenuHit, wish: Wish) -> str | None:
    """The sentence a wish needs: not offered (D8), the question for the group
    of an option that is in two of them, or the note about the allergy without
    a promise (E14). Leaving out and options need none; they are repeated as
    well (`say_understood`)."""
    if wish.kind == "unknown":
        sentence = SAY_WISH_UNKNOWN.format(wish=wish.text, name=hit.name)
        # The leaving-out that comes with it still applies ("ohne Zwiebeln, dafür mit Pommes").
        return f"{sentence} Notiert: {wish.note}." if wish.note else sentence
    if wish.kind == "open" and wish.groups:
        return SAY_WISH_WHICH_GROUP.format(
            option=wish.option, groups=" oder bei ".join(wish.groups)
        )
    if wish.kind == "allergy":
        if wish.ingredient:
            return SAY_ALLERGY_NOTE.format(name=hit.name)
        return SAY_ALLERGY_WHICH
    return None


def allergy_question(names: list[str], named: bool | None = None) -> str | None:
    """The question for the first open allergy. With several, it names the
    dish so the answer belongs to it - also for the last one in the row
    (`named`)."""
    if not names:
        return None
    if named is None:
        named = len(names) > 1
    return SAY_ALLERGY_WHICH_FOR.format(name=names[0]) if named else SAY_ALLERGY_WHICH


def _named_dish(session: Session, tenant_id: uuid.UUID, text: str) -> MenuItem | None:
    """Das eine aktive Gericht, das genau so heisst oder diesen Alias hat."""
    by_alias = _alias_items(session, tenant_id, text)
    if len(by_alias) == 1:
        return by_alias[0]
    said = normalize_query(text)
    # The menu's names once per session, not again for every search with a wish
    # (review PR #139); active is checked on the hit.
    names = session.info.setdefault("menu_names", {})
    if tenant_id not in names:
        names[tenant_id] = [
            (normalize_query(name), item_id)
            for item_id, name in session.execute(
                select(MenuItem.id, MenuItem.name).where(
                    MenuItem.tenant_id == tenant_id
                )
            )
        ]
    items = [session.get(MenuItem, i) for n, i in names[tenant_id] if n == said]
    named = [item for item in items if item is not None and item.active]
    return named[0] if len(named) == 1 else None


def _search_with_wish(
    session: Session,
    tenant_id: uuid.UUID,
    candidates: list[tuple[str, str, str]],
    max_results: int,
    now: datetime,
    high: float,
    low: float,
    card: CardFormat,
) -> SearchResult | None:
    """Search the dish without the wish, then classify the wish for it (T-4.10).

    None means: the normal search over the whole sentence decides, because the
    dish without the wish finds nothing. The search runs once, with the dish of
    the first split. If that part of the sentence belongs to the name
    ("Sommerrollen mit Garnelen"), the next wish applies to the same dish
    ("... ohne Koriander") - without a second search that could fail (Codex and
    review PR #139).
    """
    dish = candidates[0][0]
    try:
        found = search_menu(
            session,
            tenant_id,
            dish,
            max_results,
            now,
            high,
            low,
            split_check=False,
            card=card,
        )
    except (Ambiguous, NotFound):
        return None
    if not found.results:
        return None
    hit = found.results[0]
    first = 0
    # If "dish + first part of the sentence" is a dish itself ("Pizza mit
    # Salami" next to "Pizza" or "Pizza mit Pilzen"), that one applies, and only
    # the next part of the sentence is the wish (Codex PR #139).
    named = _named_dish(session, tenant_id, f"{dish} {candidates[0][2]}")
    clear = found.match_type in CLEAR_MATCHES
    if named is not None and (not clear or named.id != hit.menu_item_id):
        found = _single(session, "alias", named, now)
        hit, first = found.results[0], 1
    elif not clear:
        # If the part of the sentence belongs to the name of one of the hits,
        # the whole sentence decides (Codex PR #139).
        if any(names_it(h.name, candidates[0][2]) for h in found.results):
            return None
        return found.model_copy(update={"wish": open_wish(candidates[0][1])})
    for _, wish, segment in candidates[first:]:
        if names_it(hit.name, segment):
            continue
        classified = classify_wish(wish, hit.option_groups)
        say = found.say or say_for_wish(hit, classified)
        return found.model_copy(update={"wish": classified, "say": say})
    return found


def search_menu(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    max_results: int = AMBIGUOUS_LIMIT,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
    *,
    split_check: bool = True,
    card: CardFormat | None = None,
) -> SearchResult:
    """`split_check=False` only for `position_parts`: the search per piece and
    over the whole sentence must not run the check for several positions again.
    `card`: the card format, if the caller has already read it."""
    now = now or utcnow()
    # Which prefixes a number can carry ("S12") is decided by the menu
    # (T-4.12); read once per search, including the recursive calls.
    card = card if card is not None else card_format(session, tenant_id)
    high = settings.menu_fuzzy_threshold_high if high is None else high
    low = settings.menu_fuzzy_threshold_low if low is None else low

    # The positions of the sentence are determined at most once, even if there
    # is a wish in it (review PR #139).
    parts: list[str] | None = None
    candidates = wish_candidates(query)
    if candidates and not has_number(candidates[0][1]):
        if split_check:
            parts = position_parts(
                session, tenant_id, query, now=now, high=high, low=low, card=card
            )
        if parts is None or len(parts) <= 1:
            found = _search_with_wish(
                session, tenant_id, candidates, max_results, now, high, low, card
            )
            if found is not None:
                return found
    limit = min(max_results, AMBIGUOUS_LIMIT)

    text = normalize_query(query)

    # 1. Number - rule A: directly only if the whole sentence is exactly one
    # number ("Nummer 23", "die 23", "zweimal die 23"). If there is more next
    # to it (a second number, a name, "oder"), the search asks back instead of
    # choosing a number. Without "Nummer", a number next to a name is a
    # quantity ("zwei Frühlingsrollen") and the name search decides.
    ref, unclear = sole_item_number(query, card)
    if unclear:
        raise Ambiguous("Nummer nicht eindeutig", say=SAY_WHICH_NUMBER)
    if ref is not None:
        # "23h": a suffix that no menu has is not the 23.
        items = _by_number(session, tenant_id, ref.cards) if ref.valid else []
        if not items:
            raise NotFound(
                f"Nummer {ref.text} nicht auf der Karte",
                say=SAY_NO_SUCH_NUMBER.format(number=ref.text),
            )
        if len(items) > 1:
            # "7" and "07" on the same menu, "Sushi eins" with S1 and SM1:
            # ask back instead of choosing.
            return _ambiguous(session, items[:limit], now)
        return _single(session, "exact_number", items[0], now)

    if not text:
        raise NotFound("Anfrage ohne Inhalt", say=SAY_NOT_FOUND)

    # One sentence, one position: if the sentence names several, the search
    # asks back instead of running the name search over the whole sentence -
    # that would find one and silently swallow the other (Codex PR #124, P1).
    # Nothing is split here; the parts are in the message, the caller asks per
    # part.
    if parts is None:
        parts = (
            position_parts(
                session, tenant_id, query, now=now, high=high, low=low, card=card
            )
            if split_check
            else [query]
        )
    if len(parts) > 1:
        raise Ambiguous("mehrere Positionen: " + " | ".join(parts), say=SAY_IN_TURN)

    # 2. Exact alias. Aliases are stored as on the menu, often with an article
    # ("die knusprigen rollen"); the guest says "die knusprigen Rollen, bitte".
    # So both sides are compared without filler words. In Python instead of
    # SQL: normalize_query exists only here, and a menu has a few hundred
    # aliases - that is one index scan and a loop, not a load.
    by_alias = _alias_items(session, tenant_id, query)
    if len(by_alias) == 1:
        return _single(session, "alias", by_alias[0], now)
    if by_alias:
        return _ambiguous(session, list(by_alias)[:limit], now)

    # 3. Fuzzy: the better of name and best alias, per dish.
    #
    # Two steps, because only the first can use the GIN index: the operators %
    # and <% look up in the index, a greatest(similarity(...)) in the WHERE has
    # to touch every active row (Codex PR #117, P2).
    #
    # The prefilter is a superset on purpose, not the exact condition: the
    # session threshold is a tiny bit below `low`. Whether the operators check
    # with ">" or ">=" against their threshold depends on the version; a hit
    # exactly on the threshold would otherwise be gone here already, although
    # `total >= low` would keep it (Codex PR #117, P2). The decision is made
    # in the query below anyway; the prefilter only saves rows.
    #
    # `SET LOCAL` via set_config(..., true): the values apply only to this
    # transaction and do not stick to the connection from the pool.
    grenze = max(0.0, low - PREFILTER_EPSILON)
    session.execute(
        select(
            func.set_config("pg_trgm.similarity_threshold", str(grenze), True),
            func.set_config("pg_trgm.word_similarity_threshold", str(grenze), True),
        )
    )

    def score(column):
        return func.greatest(
            func.similarity(column, text), func.word_similarity(text, column)
        )

    def candidate(column):
        # Superset of score(column) >= low, backed by the index.
        return or_(column.op("%")(text), literal(text).op("<%")(column))

    # Without lower(): pg_trgm builds its trigrams in lower case itself, and a
    # lower(name) in the expression no longer matches the index on name.
    alias_score = (
        select(func.max(score(ItemAlias.alias)))
        .where(ItemAlias.menu_item_id == MenuItem.id)
        .correlate(MenuItem)
        .scalar_subquery()
    )
    alias_candidate = (
        select(1)
        .where(ItemAlias.menu_item_id == MenuItem.id, candidate(ItemAlias.alias))
        .correlate(MenuItem)
        .exists()
    )
    total = func.greatest(score(MenuItem.name), func.coalesce(alias_score, 0))
    rows = session.execute(
        select(MenuItem, total.label("score"))
        .where(
            *_active(tenant_id),
            # With `low <= 0` the prefilter is dropped: it could then only lose
            # rows with a score of exactly 0, which `total >= low` keeps.
            *((or_(candidate(MenuItem.name), alias_candidate),) if grenze > 0 else ()),
            # The threshold decides here, not the session variable.
            total >= low,
        )
        .order_by(total.desc(), MenuItem.number)
        .limit(AMBIGUOUS_LIMIT + 1)
    ).all()
    if not rows:
        raise NotFound(f"Kein Treffer für „{text}“", say=SAY_NOT_FOUND)
    strong = [item for item, value in rows if value >= high]
    if len(strong) == 1:
        return _single(session, "fuzzy_single", strong[0], now)
    return _ambiguous(session, [item for item, _ in rows][:limit], now)
