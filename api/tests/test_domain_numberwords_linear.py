"""`sole_item_number` and the number readers stay linear and read the same.

`sole_item_number` compared each token with every number, and helpers it
shares with `find_item_number_ref` sliced the sentence again at every position
(docs/24, chain round 4 item 29). The old versions of every function that
changed are kept below as the reference only. They run in a copy of the module,
so the old functions call each other and nothing new: every sentence of the
menu tests and the eval cases, generated sentences and the rests of them that
`search._ends_in_card_number` reads must give the same result with old and new
(a differential test).

A later change that means to change what these functions return deletes the
comparisons it outdates; the copies are never edited to match.
"""

import random
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest

from api.domain.menu import numberwords
from api.domain.menu.numberwords import NO_PREFIXES, CardFormat, sole_item_number
from api.tests.test_domain_menu_search_linear import CARDS, _corpus

# --- The old versions, verbatim from main before the change (reference only) ---

_OLD = r'''
def _bare_number_at(tokens: list[str], at: int) -> bool:
    """Is the rest of the sentence from `at` one number and filler only?"""
    span = _scan(tokens, at)
    return span is not None and all(
        t in PUNCTUATION or t in _SENTENCE_FILLER or t in _LEAD_FILLER or hesitation(t)
        for t in tokens[span.end :]
    )


def _clean_lead(tokens: list[str], at: int) -> bool:
    """Is there only punctuation, a hesitation, an article, a marker or a
    quantity before position `at`? Only then may a letter name be a prefix
    there."""
    return all(
        t in PUNCTUATION
        or t in _SPELLED_LEAD
        or t in _LEAD_FILLER
        or hesitation(t)
        or t in _QUANTITY_NOUNS
        or _word_value(t) is not None
        or (_QUANTITY_SUFFIX.match(t) is not None and _word_value(t[:-3]) is not None)
        for t in tokens[:at]
    )


def _number_spans(
    tokens: list[str],
    card: CardFormat = NO_PREFIXES,
    prefixed: set[int] | frozenset[int] = frozenset(),
) -> list[_Span]:
    """All numbers said, with their positions.

    A bare article does not count ("ein Tisch"), but a number that **starts**
    with an article does: "die ein und zwanzig" is 21 and used to be 20,
    because the "ein" was dropped before anyone checked whether it opens a
    number (Codex review PR #105, P1).

    A prefix of the menu directly in front ("S zwölf", "Sushi zwölf") belongs
    to the span (`_with_prefix`).
    """
    spans: list[_Span] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in PUNCTUATION:
            i += 1
            continue
        span = _scan(tokens, i)
        if span is None or (tokens[i] in ARTICLES and span.end == i + 1):
            i += 1
            continue
        floor = spans[-1].end if spans else 0
        spans.append(_with_prefix(tokens, span, card, floor, prefixed))
        i = max(span.end, i + 1)
    return spans


def _with_prefix(
    tokens: list[str],
    span: _Span,
    card: CardFormat,
    floor: int,
    prefixed: set[int] | frozenset[int] = frozenset(),
) -> _Span:
    """The span extended by a card prefix directly in front, longest first.

    Only tokens from `floor` on: what belongs to the previous number is not a
    prefix. Letter names only after a clean sentence start (`_clean_lead`) and
    never glued to the digits: "ES12" is the prefix ES, not "Es zwölf" (Codex
    PR #155).
    """
    for n in range(_MAX_PREFIX_TOKENS, 0, -1):
        start = span.start - n
        if start < floor:
            continue
        apart = span.start - 1 not in prefixed
        spelled = apart and _clean_lead(tokens, start)
        found = card.prefixes_of(tokens[start : span.start], spelled, heard=apart)
        if found:
            return _Span(start, span.end, span.value, n, found)
    return span


def _quantity_spans(tokens: list[str]) -> list[_Span]:
    """Numbers attached to a quantity marker: "zweimal", "2 x", "drei Portionen".
    Not the tens half of a number the ear may have cut: "drei und äh zwanzig
    Portionen" is no 20 portions (`_second_half`, #328)."""
    spans: list[_Span] = []
    # Where a number named as a dish ends ("Nummer zwei hundert"): a quantity
    # behind it starts there, never inside it (Codex PR #722).
    dish_ends = [
        span.end
        for k in range(1, len(tokens))
        if tokens[k - 1] in _ITEM_NUMBER_MARKERS | _LINK_ARTICLES
        and (span := _scan(tokens, k)) is not None
    ]
    for i, token in enumerate(tokens):
        suffix = _QUANTITY_SUFFIX.match(token)
        if suffix:
            value = _word_value(suffix.group(1))
            if value is not None:
                spans.append(_Span(i, i + 1, value))
                continue
        if token in _QUANTITY_NOUNS and i > 0:
            floor = max((e for e in dish_ends if e <= i - 1), default=0)
            span = _scan_ending_at(tokens, i - 1, floor)
            if span is not None:
                spans.append(span)
    return [s for s in spans if not _second_half(tokens, s.start)]


def _joined_without_und(tokens: list[str], k: int) -> bool:
    """ "drei zwanzig", "die 3 20": a unit before a tens half, no "und"
    between. Two numbers, or a 23 whose "und" the ear lost; `_scan` joins
    neither, and the sentence asks, also next to a name (#325). A hesitation
    between them changes nothing ("Nummer drei äh zwanzig Portionen"); a bare
    comma separates ("die 3, zwanzig Portionen" is 20 of the 3). "eine" counts
    only before a tens quantity: it cannot be the article of "zwanzig
    Portionen" (Codex PR #362)."""
    j = k + 1
    paused = False
    while j < len(tokens) and pause_before_und(tokens[j]):
        paused = paused or hesitation(tokens[j])
        j += 1
    if j >= len(tokens) or not tens_half(tokens[j]) or (j > k + 1 and not paused):
        return False
    after = j + 1
    while after < len(tokens) and pause_before_und(tokens[after]):
        after += 1  # "eine zwanzig äh Portionen"
    quantity = bool(_QUANTITY_SUFFIX.match(tokens[j])) or (
        after < len(tokens) and tokens[after] in _QUANTITY_NOUNS
    )
    return _first_half(tokens[k]) or (quantity and tokens[k] in ARTICLES)


def _prefixed_too_large(
    tokens: list[str], card: CardFormat, prefixed: set[int], taken: set[int]
) -> list[tuple[int, int, ItemNumber]]:
    """ "S1000", "S tausend", "Sushi 1000" without a marker: a prefix of the
    menu before a number above the card range. Named, not existing - no name
    search that would find a dish via "sushi" (Codex PR #155).

    Returns `(start, end, bad)` per occurrence. Tokens in `taken` already
    belong to a number after a marker.
    """
    found_all: list[tuple[int, int, ItemNumber]] = []
    i = 0
    while i < len(tokens):
        hit = None
        for n in range(_MAX_PREFIX_TOKENS, 0, -1):
            at = i + n
            if at >= len(tokens) or any(k in taken for k in range(i, at + 1)):
                continue
            spelled = _clean_lead(tokens, i) and at - 1 not in prefixed
            oversized = _oversized_after_prefix(tokens, i, n, card, spelled, False)
            if oversized is not None:
                hit = (i, *oversized)
                break
        if hit is None:
            i += 1
            continue
        found_all.append(hit)
        i = hit[1]
    return found_all


def sole_item_number(
    text: str, card: CardFormat = NO_PREFIXES
) -> tuple[ItemNumber | None, bool]:
    """Rule A: a card number only if the whole sentence is exactly one number.

    Allowed next to the number: "Nummer"/"Nr.", filler words, hesitations,
    punctuation and one quantity ("zweimal", "2x", "drei Portionen", "zwei
    Nummer 23"). Result:

    - `(ref, False)`: exactly one number and nothing else - the caller takes
      it (invalid ones such as "23h" or "Nummer 1000" as `valid=False`).
    - `(None, True)`: a number plus more (second number, text) - ask back.
    - `(None, False)`: not a number sentence. Without "Nummer", a number next
      to a name is a quantity ("zwei Frühlingsrollen") and the name search
      decides.

    One rule instead of many special cases: every sentence form that is not
    clearly one number becomes a follow-up question instead of a silent wrong
    pick (Codex reviews PR #117, 15 rounds).

    A number with a prefix of the menu ("S12", "Sushi zwölf") is never a
    quantity: directly next to a name ("S12 Lachs") the sentence asks back as
    with a marker. Separated from it by "und" or punctuation ("S12 und Pho
    Bo") it is two positions as with a plain number, and the name search and
    `position_parts` decide (T-4.12, code review PR #155). Only a number word
    without an article in front is the quantity ("zwei S zwölf").
    """
    tokens = _tokens(text)
    if any(
        (t == "und" and _split_by_pause(tokens, k)) or _joined_without_und(tokens, k)
        for k, t in enumerate(tokens)
    ):
        return None, True
    glued = _glued(text)
    prefixed = _prefixed(text)
    mengen = _quantity_spans(tokens)
    # "die dreizehn mal zwei", "13 x 2": with an article in front or as the
    # first number, either number may be the dish and the other the quantity.
    # Asked, never swapped (#677).
    for q in mengen:
        if (
            q.end + 1 < len(tokens)
            and tokens[q.end] in ("mal", "x")
            and _word_value(tokens[q.end + 1]) is not None
            # Only a bare second number: anything but filler behind it names
            # a dish ("2 x 7up", "2 x 30 a", "zwei mal Acht Schätze"), and the
            # search reads it (Codex PR #722).
            and _bare_number_at(tokens, q.end + 1)
            and (
                (q.start > 0 and tokens[q.start - 1] in _LINK_ARTICLES)
                or all(_word_value(t) is None for t in tokens[: q.start])
            )
        ):
            return None, True
    consumed: set[int] = set()
    marked: list[_Span] = []
    invalid: list[ItemNumber] = []
    # Tokens that already belong to an invalid number. The digits from "Nummer
    # A12" must not count again as a number of their own, otherwise the
    # sentence would be "unclear" instead of a number that does not exist.
    invalid_at: set[int] = set()
    marker_at: set[int] = set()
    for i, token in enumerate(tokens):
        if token not in _ITEM_NUMBER_MARKERS:
            continue
        start, end, span, bad = _marker_target(tokens, i, prefixed, card, glued)
        if span is not None:
            marked.append(span)
            marker_at.add(i)
        elif bad is not None:
            invalid.append(bad)
            marker_at.add(i)
            consumed.update(range(start, end))
            invalid_at.update(range(start, end))
    taken = invalid_at | {k for s in marked for k in range(s.start, s.end)}
    for start, end, bad in _prefixed_too_large(tokens, card, prefixed, taken):
        invalid.append(bad)
        consumed.update(range(start, end))
        invalid_at.update(range(start, end))
    numbers = list(marked)
    spans = _number_spans(tokens, card, prefixed)
    prefix_at = {s.start for s in [*spans, *marked] if s.prefixes}

    def _quantity_before_prefix(span: _Span) -> bool:
        """ "zwei S zwölf": a number word without an article before the prefix
        is the quantity. Digits or an article in front ("die 23 S12") are a
        number of their own, and the sentence asks back (review PR #155)."""
        return (
            span.end in prefix_at
            and not any(tokens[k].isdigit() for k in range(span.start, span.end))
            and not (span.start > 0 and tokens[span.start - 1] in _LINK_ARTICLES)
        )

    for span in spans:
        if any(span.overlaps(m) for m in marked) or any(
            span.overlaps(q) for q in mengen
        ):
            continue
        if any(k in invalid_at for k in range(span.start, span.end)):
            continue
        if span.end in marker_at or _quantity_before_prefix(span):
            # "zwei Nummer 23", "zwei S zwölf": quantity before marker or prefix.
            consumed.update(range(span.start, span.end))
            continue
        numbers.append(span)
    if not numbers and not invalid:
        return None, False

    for span in [*numbers, *mengen]:
        consumed.update(range(span.start, span.end))
        nxt = span.end
        if nxt >= len(tokens):
            continue
        after = tokens[nxt]
        # Belonging to the number: a quantity word ("2 x", "drei Portionen"), a
        # card suffix a to g ("23a", "23 a") and - only after "Nummer" - any
        # other suffix, which then makes the number invalid ("Nummer 23h").
        # Without a marker "7up" or "23h" stays text for the name search.
        if (
            after in _QUANTITY_NOUNS
            or after in _SUFFIXES
            or (span in marked and _long_suffix(after))
            or (
                span in marked
                and after.isalpha()
                and (span.digits_at in glued or len(after) == 1)
            )
        ):
            consumed.add(nxt)
        # "die 30, ah", "Die 30, A.": a letter behind a pause is the number's,
        # a candidate `_heard_suffixes` offers and the search asks (#677).
        at = nxt
        while at < len(tokens) and (tokens[at] in _PAUSES or hesitation(tokens[at])):
            at += 1
        # Only where the menu has that lettered dish: else the letter stays
        # unread, and the plain number is not taken unasked.
        if (
            span in numbers
            and at < len(tokens)
            and tokens[at] in _LETTER_WORDS
            and any(
                canonical_card(f"{prefix}{span.value}{_HEARD_SUFFIXES[tokens[at]]}")
                in card.lettered
                for prefix in span.prefixes or ("",)
            )
            and all(_trails(t) for t in tokens[at + 1 :] if t not in PUNCTUATION)
        ):
            consumed.add(at)
    consumed |= marker_at
    # Tokens that belong to a number - valid or not.
    number_at = {k for s in numbers for k in range(s.start, s.end)} | invalid_at

    def _connects(k: int) -> bool:
        """Steht links und rechts von Position k eine Zahl?"""
        return any(x < k for x in number_at) and any(x > k for x in number_at)

    # A connecting word between two numbers is transparent: "23 oder 24" is a
    # follow-up question about the number, not a sentence about a dish called
    # "oder". If it hangs loose instead ("Nummer 23, nein", "Nummer 23 oder"),
    # the guest took something back or did not finish. It is then not a dish
    # name - so it does not belong in the leftovers but makes the sentence
    # unclear by itself (Codex PR #117, P1).
    #
    # Exception: an "und" that opens the sentence. "Und noch die 24" continues
    # the order from the previous sentence and does not connect a second number
    # in this one (T-5.2). Only at the very start: if a quantity ("zweimal und
    # die 24") or a marker ("Nummer und 24") comes before it, it connects
    # something in this sentence that is missing. And never before a tens
    # word: "und zwanzig" is the second half of "drei und zwanzig", the
    # recognition only cut it off (review PR #147), also in digits ("und 20",
    # the ear wrote "Nummer 3 und 20" for a 23, docs/20) and with a pause
    # between the two: "und äh zwanzig", "und bitte zwanzig" (`inside_split`,
    # Codex PR #147). "und die zwanzig", "und S zwanzig", "und Nummer zwanzig"
    # and "und ein und zwanzig" stay continuations. Only "und": an "oder" or
    # "nein" up front refers to something this sentence does not name and
    # stays a follow-up question.
    def _opens(k: int) -> bool:
        if tokens[k] != "und":
            return False
        j = k + 1
        while j < len(tokens) and inside_split(tokens[j]):
            j += 1
        if j < len(tokens) and tens_half(tokens[j]):
            return False
        return all(
            t in PUNCTUATION or hesitation(t) or t in _OPENING for t in tokens[:k]
        )

    loose = any(
        t in _CONNECTORS and not _connects(k) and not _opens(k)
        for k, t in enumerate(tokens)
        if k not in consumed
    )
    # Tokens between a marker and the next number: _LEAD_FILLER does not apply
    # there, "Nummer dazu 23" stays unclear (review PR #147).
    after_marker = {
        k
        for i, tok in enumerate(tokens)
        if tok in _ITEM_NUMBER_MARKERS
        for k in range(i + 1, min((n.start for n in numbers if n.start > i), default=0))
    }
    # What is left over and could be a dish name. A word glued to the digits
    # ("JA12") is never a filler word (Codex PR #155).
    residue_at = [
        k
        for k, t in enumerate(tokens)
        if k not in consumed
        and (
            k in prefixed
            or (
                t not in PUNCTUATION
                and t not in _CONNECTORS
                and t not in _SENTENCE_FILLER
                and (t not in _LEAD_FILLER or k in after_marker)
                and not hesitation(t)
                and t not in _ITEM_NUMBER_MARKERS
                and not _QUANTITY_SUFFIX.match(t)
            )
        )
    ]
    residue = [tokens[k] for k in residue_at]
    has_marker = bool(marked or invalid)

    def _apart(span: _Span) -> bool:
        """Is there an "und" or punctuation between the prefixed number and
        every leftover word? Then the rest is a position of its own (code
        review PR #155)."""

        def cut(a: int, b: int) -> bool:
            return any(
                tokens[x] in PUNCTUATION or tokens[x] == "und" for x in range(a, b)
            )

        return all(
            cut(span.end, k) if k >= span.end else cut(k + 1, span.start)
            for k in residue_at
        )

    # A marker that caught no number itself still counts - but only for a
    # number that comes after it: "Nummer Ente 23" is a number next to a name,
    # so a follow-up question instead of a name search for "ente" (Codex PR
    # #117, P2). If the number comes first, it does not belong to the marker:
    # in "zwei Fruehlingsrollen, Nummer weiss ich nicht" the two is a quantity
    # and the guest is saying they do not know the number - then the name
    # search decides.
    # A number with a prefix is never a quantity ("S12 Lachs"): directly next
    # to a name the sentence asks back, as with a marker (review PR #155). A
    # prefix that was only heard (`_HEARD_LETTERS`) leaves the plain number
    # possible, so next to a name it is what it was without it: "das ist 5g
    # Zucker" stays a sentence for the name search.
    nummer_gemeint = (
        has_marker
        or any(s.prefixes and "" not in s.prefixes and not _apart(s) for s in numbers)
        or any(
            span.start > i
            for i, tok in enumerate(tokens)
            if tok in _ITEM_NUMBER_MARKERS
            for span in numbers
        )
    )
    # A loose connecting word is only a number taken back if a number was being
    # talked about at all: with a marker, or if there is no dish name next to
    # it. Otherwise the "oder" belongs to the name - "zwei Cola oder Fanta" is
    # a quantity next to a name, and the alias "cola oder fanta" must stay
    # reachable (Codex PR #117, P2).
    dangling = loose and (has_marker or not residue)
    if len(numbers) + len(invalid) == 1 and not residue and not dangling:
        if invalid:
            return invalid[0], False
        return _ref(tokens, numbers[0], has_marker, glued, card), False
    if nummer_gemeint or dangling or not residue:
        return None, True
    return None, False
'''


@cache
def _old() -> dict[str, object]:
    """The module as it is, with the old functions put back: a copy whose
    functions all look each other up in this namespace."""
    namespace: dict[str, object] = {"__name__": "numberwords_before_linear"}
    source = Path(numberwords.__file__).read_text(encoding="utf-8")
    exec(compile(source, "numberwords", "exec"), namespace)
    exec(compile(_OLD, "numberwords_before_linear", "exec"), namespace)
    return namespace


# The readers that use a changed function, and how they are called.
_READERS: list[tuple[str, bool]] = [
    ("sole_item_number", True),
    ("find_item_number_ref", True),
    ("find_item_number", True),
    ("find_marked_item_numbers", True),
    ("find_numbers", False),
    ("find_quantity", False),
]


def _read(module: dict[str, object] | None, name: str, text: str, card: CardFormat):
    reader = getattr(numberwords, name) if module is None else module[name]
    with_card = dict(_READERS)[name]
    # repr: the copy has its own classes, which never compare equal.
    return repr(reader(text, card) if with_card else reader(text))  # type: ignore[operator]


# --- The corpus ---

# Pieces of number sentences, many of them the shapes that cost the old
# version quadratic time when repeated.
_FRAGMENTS = [
    "die 5", "Nummer 5", "und die 24", "5 oder", "zweimal die 13", "die 30 a",
    "die 30, ah", "S12 Lachs und", "S12", "äh", "und äh", "nummer", "die",
    "die dreizehn mal zwei", "Nummer A12", "S1000", "Sushi zwölf",
    "drei und äh zwanzig", "drei zwanzig", "Ente", "und", "oder", "Nummer Ente",
    "2 x", "zwei Portionen", "Nummer 1000", "eine", "Nummer 23h", "Nr. 12", "ein",
    ",", "und ,", "bitte und", "S 12", "es zwei", "ist zwölf", "die 25g",
    "Nummer 3 und 20", "zwei S zwölf", "dazu", "Nummer dazu", "13 x 2",
    "äh und", "3 20", "die 26 der", "Nummer 35 ja genau", "S tausend", "Z 12 h",
    "Nummer JA12", "die 23a", "23 ab", "noch", "nein", "doch die 24", "Und noch",
    "ein und zwanzig", "vierzig sieben", "zwei hundert", "Nummer Z 1000",
    "Nummer 23 mit Reis", "für 8,50", "um 19 Uhr", "tausend", "Pho Bo",
]  # fmt: skip
_SEPARATORS = [" ", " ", ", ", " und ", ". ", " oder ", " äh "]


def _generated(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    sentences = []
    for _ in range(count):
        text = rng.choice(_FRAGMENTS)
        for _ in range(rng.randint(0, 7)):
            text += rng.choice(_SEPARATORS) + rng.choice(_FRAGMENTS)
        sentences.append(text)
    return sentences


@cache
def _sentences() -> list[str]:
    return [*_corpus(), *_generated(4000, 29)]


def _rests(sentence: str) -> list[str]:
    """What `_ends_in_card_number` reads: the sentence from each word on."""
    words = sentence.split()
    return [" ".join(words[i:]) for i in range(len(words))]


# --- The differential tests ---


@pytest.mark.parametrize("card", CARDS, ids=["plain", "prefixes"])
def test_sole_item_number_matches_the_old_version(card: CardFormat) -> None:
    old = _old()
    differ = []
    for sentence in _sentences():
        for text in _rests(sentence):
            was = _read(old, "sole_item_number", text, card)
            if _read(None, "sole_item_number", text, card) != was:
                differ.append((text, was))
    assert differ == []


@pytest.mark.parametrize("card", CARDS, ids=["plain", "prefixes"])
@pytest.mark.parametrize("name", [n for n, _ in _READERS if n != "sole_item_number"])
def test_the_other_readers_match_the_old_version(name: str, card: CardFormat) -> None:
    old = _old()
    differ = []
    for text in _sentences():
        was = _read(old, name, text, card)
        if _read(None, name, text, card) != was:
            differ.append((text, was))
    assert differ == []


def test_the_copy_runs_the_old_functions() -> None:
    """Guards the harness: the copy must hold the old code, not the new."""
    old = _old()
    for name in ("_clean_lead", "sole_item_number"):
        assert old[name].__code__.co_filename == "numberwords_before_linear"  # type: ignore[attr-defined]


# --- The cost ---


def _seconds(call: Callable[[], object]) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - start)
    return best


# Shapes that cost the old version quadratic time, each repeated.
_LONG = [
    ("die 5", NO_PREFIXES),
    ("Nummer 5", NO_PREFIXES),
    ("und die 24", NO_PREFIXES),
    ("zweimal die 13", NO_PREFIXES),
    ("13 x 2", NO_PREFIXES),
    ("eine", NO_PREFIXES),
    ("äh", NO_PREFIXES),
    (",", NO_PREFIXES),
    ("nummer", NO_PREFIXES),
    ("5 oder", NO_PREFIXES),
    ("die 30 a", CARDS[1]),
    ("S12 Lachs und", CARDS[1]),
]


@pytest.mark.latency
@pytest.mark.parametrize(("unit", "card"), _LONG, ids=[u for u, _ in _LONG])
def test_long_sentences_cost_linear_time(unit: str, card: CardFormat) -> None:
    """Twice the length, about twice the time, never the four times of a
    quadratic loop. Measured against the same call at half the size, so a
    slow machine does not matter; 3 leaves room for noise. Wall clock: runs
    serially with the latency tests, not under xdist."""

    def cost(n: int) -> float:
        text = " ".join([unit] * n) + " 7"
        return _seconds(lambda: sole_item_number(text, card))

    small, large = cost(400), cost(800)
    assert large < 3 * small + 0.002, (small, large)
