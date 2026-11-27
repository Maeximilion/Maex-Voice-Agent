"""The splitting helpers of `search.py` stay linear and give the same results.

`_takes_a_number_back`, `_ends_in_card_number` and `_keep_allergy_clauses`
used to read earlier pieces, the rest of a piece or the rest of the list again
for each piece (docs/24, deep review of round 3 item 7). The old versions are
kept below as the reference only: every caller sentence of the menu tests and
the eval cases, plus generated sentences, must give the same result and the
same piece searches with the new ones (a differential test).
"""

import ast
import json
import random
import time
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from api.core.errors import Ambiguous, NotFound
from api.domain.menu import search
from api.domain.menu.numberwords import (
    NO_PREFIXES,
    CardFormat,
    fold,
    holds_number,
    sole_item_number,
)
from api.domain.menu.split import cut_at_separators, raw_pieces, split_positions
from api.domain.menu.wishes import has_number, opens_with_allergy
from api.schemas.menu import SearchResult

ROOT = Path(__file__).resolve().parents[2]


# --- The old versions, verbatim from main before the change (reference only) ---


def _old_keep_allergy_clauses(query: str, parts: list[str]) -> list[str]:
    if len(parts) <= 1 or not any(opens_with_allergy(p) for p in parts):
        return parts
    if opens_with_allergy(parts[0]):
        rest = _old_keep_allergy_clauses(query, parts[1:])
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


def _old_takes_a_number_back(
    search_piece: Callable[[str], SearchResult], pieces: list[str], card: CardFormat
) -> bool:
    said = [sole_item_number(piece, card) for piece in pieces]
    card_numbers = [ref is not None or more for ref, more in said]
    for k, piece in enumerate(pieces):
        if card_numbers[k] or not any(
            card_numbers[i] or has_number(pieces[i]) for i in range(k)
        ):
            continue
        own = _old_ends_in_card_number(piece, card)
        if not (own or any(card_numbers[k + 1 :])):
            continue
        if not search.normalize_query(piece) or (has_number(piece) and not own):
            continue
        try:
            search_piece(piece)
        except NotFound:
            return True
        except Ambiguous:
            continue
    return False


def _old_ends_in_card_number(piece: str, card: CardFormat) -> bool:
    words = piece.split()
    return any(
        fold(word).strip(".") in search._CARD_NUMBER_LEADS
        and sole_item_number(" ".join(words[i:]), card)[0] is not None
        for i, word in enumerate(words)
        if i
    )


# --- The corpus ---


def _test_sentences() -> set[str]:
    """Every string in the menu, dispatch and number tests: the caller
    sentences are among them, the rest only costs a little time."""
    files = [
        *sorted((ROOT / "api" / "tests").glob("test_domain_menu*.py")),
        ROOT / "api" / "tests" / "test_agent_dispatch_menu.py",
        ROOT / "evals" / "number_eval.py",
    ]
    found: set[str] = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and 0 < len(node.value) <= 400
                and any(c.isalnum() for c in node.value)
            ):
                found.add(node.value)
    return found


def _eval_sentences() -> set[str]:
    found: set[str] = set()
    for folder in ("cases", "targets"):
        for path in sorted((ROOT / "evals" / folder).glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            for turn in case.get("transcript", []):
                if isinstance(turn.get("text"), str):
                    found.add(turn["text"])
    return found


_FRAGMENTS = [
    "die 23", "die 24", "Nummer 24", "Nr. 12", "nr 7", "die 25g", "die 30 a",
    "doch die 24", "ne, die 24", "quatsch die 24", "nein nummer 13",
    "die", "nummer", "nr.", "die die 24", "die 24 nr", "zwei Nummer 23",
    "S12", "Sushi zwölf", "die dreiundzwanzig", "drei und zwanzig",
    "Ente", "Pho Bo", "Fisch und Chips", "knusprige Ente", "Ente süß sauer",
    "einer Sesamallergie", "und einer Sesamallergie", "ich habe eine Erdnussallergie",
    "meine Frau ist gegen Sesam allergisch", "und auch noch", "und",
    "ohne Zwiebeln", "mit Reis", "zweimal", "also", "danke", "bitte", "äh",
    "zwei Cola", "für 8,50", "um 19 Uhr", "die 99", "Nummer 1000", "tausend",
]
_SEPARATORS = [", ", " und ", " ", ". ", ", und ", " oder "]


def _generated_sentences(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    sentences = []
    for _ in range(count):
        size = rng.randint(1, 9)
        text = rng.choice(_FRAGMENTS)
        for _ in range(size - 1):
            text += rng.choice(_SEPARATORS) + rng.choice(_FRAGMENTS)
        sentences.append(text)
    return sentences


CORPUS = sorted(_test_sentences() | _eval_sentences()) + _generated_sentences(4000, 9)
CARDS = [
    NO_PREFIXES,
    CardFormat(
        prefixes=frozenset({"s", "sm"}),
        words=(("sushi", ("s", "sm")),),
        lettered=frozenset({"25g", "30a"}),
    ),
]


def _stub_search(calls: list[str]) -> Callable[[str], SearchResult]:
    """A menu that answers by the piece: no dish, two dishes, or a hit. Every
    call is recorded, so old and new must search the same pieces in turn."""

    def find(piece: str) -> SearchResult:
        calls.append(piece)
        answer = zlib.crc32(piece.encode()) % 3
        if answer == 0:
            raise NotFound("none")
        if answer == 1:
            raise Ambiguous("two")
        return SearchResult(match_type="alias", results=[])

    return find


def _part_lists(sentence: str) -> list[list[str]]:
    lists = [raw_pieces(sentence), split_positions(sentence), cut_at_separators(sentence)]
    return [parts for parts in lists if parts]


# --- The differential tests ---


def test_the_corpus_holds_test_and_eval_sentences() -> None:
    assert len(_eval_sentences()) > 250
    assert len(_test_sentences()) > 1000


@pytest.mark.parametrize("card", CARDS, ids=["plain", "prefixes"])
def test_ends_in_card_number_matches_the_old_version(card: CardFormat) -> None:
    differ = []
    for sentence in CORPUS:
        for piece in {sentence, *raw_pieces(sentence)}:
            old = _old_ends_in_card_number(piece, card)
            if search._ends_in_card_number(piece, card) != old:
                differ.append((piece, old))
    assert differ == []


@pytest.mark.parametrize("card", CARDS, ids=["plain", "prefixes"])
def test_takes_a_number_back_matches_the_old_version(card: CardFormat) -> None:
    differ = []
    for sentence in CORPUS:
        pieces = raw_pieces(sentence)
        old_calls: list[str] = []
        new_calls: list[str] = []
        old = _old_takes_a_number_back(_stub_search(old_calls), pieces, card)
        new = search._takes_a_number_back(_stub_search(new_calls), pieces, card)
        if (old, old_calls) != (new, new_calls):
            differ.append((sentence, old, new, old_calls, new_calls))
    assert differ == []


def test_keep_allergy_clauses_matches_the_old_version() -> None:
    differ = []
    for sentence in CORPUS:
        for parts in _part_lists(sentence):
            old = _old_keep_allergy_clauses(sentence, list(parts))
            if search._keep_allergy_clauses(sentence, list(parts)) != old:
                differ.append((sentence, parts, old))
    assert differ == []


_CLAUSES = [
    "einer Sesamallergie", "und einer Sesamallergie", "ich bin gegen Sesam allergisch",
    "und", "auch noch", "die 23", "Ente", "Pho Bo", "und die", "noch eine Erdnussallergie",
]


def test_keep_allergy_clauses_matches_on_generated_part_lists() -> None:
    """Part lists of every shape, also with parts that are not in the query in
    that order, so the old fallbacks (`return parts`) are reached as well."""
    rng = random.Random(3)
    differ = []
    for _ in range(4000):
        parts = [rng.choice(_CLAUSES) for _ in range(rng.randint(1, 8))]
        query = ", ".join(parts)
        if rng.random() < 0.2:
            rng.shuffle(parts)
        old = _old_keep_allergy_clauses(query, list(parts))
        if search._keep_allergy_clauses(query, list(parts)) != old:
            differ.append((query, parts, old))
    assert differ == []


def test_holds_number_finds_every_number_sole_item_number_reads() -> None:
    """The shortcut of `_ends_in_card_number`: a text without such a token is
    never a card number, valid or not."""
    for sentence in CORPUS:
        for card in CARDS:
            if not holds_number(sentence):
                assert sole_item_number(sentence, card)[0] is None, sentence


# --- The cost ---


def _seconds(call: Callable[[], object]) -> float:
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


def test_long_sentences_cost_linear_time() -> None:
    """Twice the pieces, about twice the time, never the four times of a
    quadratic loop. Measured against the same call at half the size, so a
    slow machine does not matter; 3 leaves room for noise."""

    def costs(n: int) -> tuple[float, float, float]:
        pieces = ["Ente"] * n
        parts = ["einer Sesamallergie"] * n + ["Ente"]
        piece = "doch " + "die " * n + "Ente"
        return (
            _seconds(
                lambda: search._takes_a_number_back(
                    _stub_search([]), pieces, NO_PREFIXES
                )
            ),
            _seconds(lambda: search._keep_allergy_clauses(", ".join(parts), parts)),
            _seconds(lambda: search._ends_in_card_number(piece, NO_PREFIXES)),
        )

    small = [min(c) for c in zip(*(costs(1500) for _ in range(3)), strict=True)]
    large = [min(c) for c in zip(*(costs(3000) for _ in range(3)), strict=True)]
    for name, a, b in zip(("takes_back", "keep_allergy", "ends_in_card"), small, large, strict=True):
        assert b < 3 * a + 0.002, (name, a, b)


def test_many_leading_allergy_clauses_do_not_recurse() -> None:
    """The old version recursed once per leading allergy part and stopped with
    a RecursionError at about a thousand."""
    parts = ["einer Sesamallergie"] * 5000 + ["Ente"]
    merged = search._keep_allergy_clauses(", ".join(parts), parts)
    assert len(merged) == 1
    assert merged[0].startswith("Ente, einer Sesamallergie")
