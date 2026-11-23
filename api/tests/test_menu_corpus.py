"""Corpus of caller sentences through the agent's real menu entry point (#207).

Step 1 of the menu parser redesign: tests only. Every sentence goes through
`dispatch(..., "search_menu", ...)` (`agent/dispatch._search_menu`, which
splits the sentence into positions first), against one seeded menu, and is
judged by four conservation properties instead of one expected answer:

- N  a number with article, marker or prefix is only ever a card number and
     belongs to a clear hit, else the sentence asks; a count is the single
     quantity beside the dish, never part of a wish.
- O  a free note never holds an option of its dish outside a direct
     ohne/kein, nor a dish name or a card number.
- A  every allergy or health mention is the wish's ingredient or denied in
     its own clause, else the allergy stays open (asked).
- P  a piece that is neither hit, number, wish nor ignorable filler keeps
     the sentence whole and asks.

Three blocks: `RED` (confirmed findings of review #207, area E; a row that
breaks its property on main today is `xfail(strict=True)`, so the gate PR
that fixes it must drop the mark), `CLEAN` (what the review checked and
found holding; must pass now and stay passing) and a generated block
(dish form x residue x second element). The caller sentences are German on
purpose (`.claude/rules/tests-language.md`).

`test_row_counts` prints the baseline: rows, green and xfail per property.
"""

import itertools
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.agent.dispatch import dispatch
from api.domain.menu.importer import (
    ALIASES_FILE,
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    apply,
    parse,
)
from api.domain.menu.search import CLEAR_MATCHES
from api.tests.test_domain_draft_order import _call
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)

# One menu for every row: the test menu of `test_domain_menu_search` (numbers
# 12, 13, 23, 24, 47, 48), the register-like prefixed numbers of its `sushi`
# fixture (S, SM, 25G), curries on 40 and 42 for the "vierzig zwei" join, and
# for dish 47 the options of the review probes.
MENU_ROWS = (
    "number;name;category;price_eur;description;active\n"
    "23;Frühlingsrollen (4 Stück);Vorspeisen;6,90;;ja\n"
    "24;Sommerrollen mit Garnelen;Vorspeisen;7,50;;ja\n"
    "12;Wan-Tan-Suppe;Suppen;5,00;;ja\n"
    "13;Pho Bo;Suppen;11,90;;ja\n"
    "47;Ente knusprig;Hauptgerichte;15,50;;ja\n"
    "48;Ente süß-sauer;Hauptgerichte;14,90;;ja\n"
    "S1;Lachs Nigiri;Sushi;4,50;;ja\n"
    "S07;Gurken Maki;Sushi;3,90;;ja\n"
    "S12;Thunfisch Maki;Sushi;5,90;;ja\n"
    "S20;Ebi Nigiri;Sushi;4,50;;ja\n"
    "S22;Tamago Nigiri;Sushi;4,50;;ja\n"
    "S40;Maki Lachs;Sushi;4,50;;ja\n"
    "S42;Maki Gurke;Sushi;4,50;;ja\n"
    "SM1;Sushi-Menü klein;Sushi;16,90;;ja\n"
    "25;Chop Suey;Hauptgerichte;12,50;;ja\n"
    "25G;Soße Chop Suey;Hauptgerichte;2,00;;ja\n"
    "40;Curry rot;Hauptgerichte;12,50;;ja\n"
    "42;Curry grün;Hauptgerichte;12,50;;ja\n"
)
MENU = {
    MENU_FILE: MENU_ROWS,
    OPTIONS_FILE: (
        "number;group_name;option_name;price_delta_eur;is_default;required;price_reason\n"
        "47;Beilage;Reis;0,00;ja;nein;\n"
        "47;Beilage;Nudeln;3,00;nein;nein;\n"
        "47;Extra;Tofu;1,50;nein;nein;\n"
        "47;Extra;Ei;1,00;nein;nein;\n"
    ),
    ALLERGENS_FILE: "number;allergen_codes;confirmed_by\n",
    ALIASES_FILE: (
        "number;alias\n23;Frühlingsrollen\n24;Sommerrollen\n12;Suppe\n13;Suppe\n13;Pho\n"
        "47;die knusprige Ente\n48;süßsaure Ente\n"
    ),
}
DISH_NAMES = ("pho bo", "ente knusprig", "chop suey", "frühlingsrollen", "sommerrollen")
OPTIONS_47 = ("tofu", "nudeln", "ei", "reis")


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    tid = uuid.UUID(
        seed(session, tenant_name="Corpus", timezone="Europe/Berlin").tenant_id
    )
    plan = parse(MENU)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    return tid


# --- the reading of one answer ---------------------------------------------------


@dataclass
class Pos:
    status: str  # clear | ask | notfound | other
    numbers: list[str] = field(default_factory=list)
    wish: dict | None = None
    say: str = ""


@dataclass
class Reading:
    positions: list[Pos]
    say: str

    @property
    def clear(self) -> list[Pos]:
        return [p for p in self.positions if p.status == "clear"]


def _status(match_type: str | None, code: str | None) -> str:
    if code == "ambiguous" or match_type == "ambiguous":
        return "ask"
    if code == "not_found" or match_type == "not_found":
        return "notfound"
    return "clear" if match_type in CLEAR_MATCHES else "other"


def read(session, tid: uuid.UUID, said: str) -> Reading:
    cid = _call(session, tid)
    r = dispatch(session, cid, tid, "search_menu", {"query": said}, now=NOW)
    d = r.data or {}
    if d.get("match_type") == "positions":
        parts = [
            Pos(
                _status(p.get("match_type"), p.get("error_code")),
                [h["number"].lower() for h in p.get("results", [])],
                p.get("wish"),
                p.get("say") or "",
            )
            for p in d["positions"]
        ]
    else:
        parts = [
            Pos(
                _status(d.get("match_type"), r.error_code),
                [h["number"].lower() for h in d.get("results", [])],
                d.get("wish"),
                r.say or "",
            )
        ]
    return Reading(parts, r.say or "")


# --- one assert helper per property ----------------------------------------------


@dataclass(frozen=True)
class Spec:
    numbers: tuple[str, ...] = ()  # card numbers the caller said (lower case)
    either: bool = False  # the numbers are alternatives: at most one may be booked
    no_notfound: bool = False  # a said number is not answered with "not found"
    no_option: str | None = None  # an option whose count or portion was not settled
    keeps: tuple[str, ...] = ()  # words of the caller the read-back must keep
    options: tuple[str, ...] = ()  # options of the dish that no free note may hold
    allergies: tuple[str, ...] = ()  # allergy or health words said as the caller's own
    residue: tuple[
        str, ...
    ] = ()  # pieces that are neither hit, number, wish nor filler


def _free_texts(r: Reading) -> list[str]:
    out = []
    for p in r.clear:
        w = p.wish
        if not w:
            continue
        if w["kind"] == "note":
            out.append((w.get("text") or "").lower())
        if w["kind"] in ("note", "option") and w.get("note"):
            out.append(w["note"].lower())
    return out


def broken_n(r: Reading, s: Spec) -> str | None:
    said = set(s.numbers)
    booked = {n for p in r.clear for n in p.numbers}
    if said and r.clear and not s.either and not said <= booked:
        return f"number said but not booked {sorted(said - booked)}, clear {sorted(booked)}"
    if s.either and len(r.clear) > 1:
        return f"two alternatives both clear {sorted(booked)}"
    if s.either and len(r.positions) > 1 and r.clear:
        seen = {
            n for p in r.positions if p.status in ("clear", "ask") for n in p.numbers
        }
        if not said <= seen:
            return f"alternative {sorted(said - seen)} dropped beside a booked one"
    only = r.positions[0]
    if (
        len(r.positions) == 1
        and only.status == "ask"
        and said
        and only.numbers
        and not said <= set(only.numbers)
        and "welche nummer" not in only.say.lower()
        and not s.either
    ):
        return f"asks about {only.numbers} and leaves out {sorted(said - set(only.numbers))}"
    if s.no_notfound and len(r.positions) == 1 and r.positions[0].status == "notfound":
        return "a said number ends as not found"
    if s.no_option:
        for p in r.clear:
            w = p.wish or {}
            if (
                w.get("kind") == "option"
                and (w.get("option") or "").lower() == s.no_option
            ):
                return (
                    f"option {s.no_option} booked with its count or portion unsettled"
                )
    return None


def broken_o(r: Reading, s: Spec) -> str | None:
    for text in _free_texts(r):
        for option in s.options:
            for m in re.finditer(rf"\b{re.escape(option)}\b", text):
                if not re.search(r"\b(ohne|kein\w*)\s+$", text[: m.start()]):
                    return f"option {option} rides in a free note {text!r}"
        for name in DISH_NAMES:
            if name in text:
                return f"dish name {name} in a free note {text!r}"
        if re.search(r"\d", text):
            return f"card number in a free note {text!r}"
    said = r.say.lower()
    for word in s.keeps:
        if r.clear and word not in said:
            return f"read-back loses {word!r}: {r.say!r}"
    return None


def broken_a(r: Reading, s: Spec) -> str | None:
    if not s.allergies or not r.clear:
        return None
    wishes = [
        p.wish for p in r.positions if p.wish and p.wish["kind"] in ("allergy", "open")
    ]
    if not wishes:
        return f"allergy {s.allergies} said, clear hit without an allergy wish"
    text = " ".join((w.get("text") or "").lower() for w in wishes)
    lost = [a for a in s.allergies if a not in text]
    return f"allergy {lost} dropped from the reading {text!r}" if lost else None


def broken_p(r: Reading, s: Spec) -> str | None:
    if not s.residue or not r.clear:
        return None
    kept = " ".join(
        f"{(p.wish or {}).get('text') or ''} {(p.wish or {}).get('note') or ''}".lower()
        for p in r.positions
    )
    for piece in s.residue:
        if piece not in kept:
            return (
                f"piece {piece!r} neither hit, number, wish nor filler, yet a clear hit"
            )
    return None


CHECKS = {"N": broken_n, "O": broken_o, "A": broken_a, "P": broken_p}


def violated(r: Reading, s: Spec) -> dict[str, str]:
    return {k: msg for k, f in CHECKS.items() if (msg := f(r, s))}


# --- red rows: confirmed findings of review #207 (area E, main 69ef3678) -----------


def row(prop: str, finding: str, said: str, **spec) -> tuple:
    return (prop, finding, said, Spec(**spec))


RED: list[tuple] = []

# PR #720: a priced addition in a wish never rides free in the note.
for _said in (
    "die 47 ohne Zwiebeln, plus Tofu",
    "die 47 ohne Zwiebeln, dazu Tofu",
    "die 47 ohne Zwiebeln, zusätzlich Tofu",
    "die 47 ohne Zwiebeln, stattdessen Tofu",
    "die 47 ohne Zwiebeln, aber Tofu",
    "die 47 ohne Zwiebeln, Tofu dazu",
    "die 47 mit Nudeln, ohne Zwiebeln, plus Tofu",
    "die 47 mit Nudeln, ohne Zwiebeln, dazu Tofu",
):
    RED.append(
        row(
            "O",
            "e720-addition-after-removal",
            _said,
            numbers=("47",),
            options=("tofu",),
        )
    )
for _said in (
    "die 47 ohne Zwiebeln, dafür 2x Tofu",
    "die 47 ohne Zwiebeln, dafür zweimal Tofu",
    "die 47 kein Huhn, sondern 2 Stück Tofu",
):
    RED.append(
        row("N", "e720-count-behind-swap", _said, numbers=("47",), no_option="tofu")
    )
for _said, _option in (
    ("zweimal die 47, einmal mit Tofu", "tofu"),
    ("zweimal die 47, eine mit Nudeln", "nudeln"),
):
    RED.append(
        row("N", "e720-per-portion-addition", _said, numbers=("47",), no_option=_option)
    )
for _said in (
    "die 47 mit 2x Tofu",
    "die 47 mit zweimal Tofu",
    "die 47 mit 2 Stück Tofu",
):
    RED.append(
        row("N", "e720-issue-example-lost", _said, numbers=("47",), no_notfound=True)
    )
for _said in (
    "die 47 ohne Koriander, lieber ohne Zwiebeln",
    "die 47 kein Koriander, dafür ohne Zwiebeln",
):
    RED.append(
        row(
            "O",
            "e720-swapped-removal-echo",
            _said,
            numbers=("47",),
            keeps=("koriander", "zwiebeln"),
        )
    )

# PR #721: a clear card number is never lost or invented in a split.
for _said in (
    "Pho Bo äh die 23",
    "die 23 äh Pho Bo",
    "die dreiundzwanzig äh Pho Bo",
    "Die 23. Pho Bo.",
    "Pho Bo. Die 23.",
    "Pho Bo? Die 23",
    "die 23 - Pho Bo",
    "die 23 also Pho Bo",
    "Pho Bo dazu die 23",
):
    RED.append(row("N", "e721-number-beside-name", _said, numbers=("23",)))
for _said in (
    "die 23 ohne Zwiebeln, nee, Pho Bo",
    "die 23 ohne Zwiebeln, äh, Pho Bo",
    "die 23 ohne Zwiebeln, oder Pho Bo",
    "S12 ohne Wasabi, äh, Pho Bo",
):
    RED.append(row("O", "e721-wish-bypasses-guard", _said, numbers=("23",)))
RED.append(
    row(
        "N",
        "e721-wish-bypasses-guard",
        "die 23 äh Pho Bo ohne Koriander",
        numbers=("23",),
    )
)
for _said in (
    "die 23 statt Pho Bo",
    "die 23 oder Pho Bo",
    "die 23 oder Ente",
    "die 23 plus Pho Bo",
    "Pho Bo mit der 23",
):
    RED.append(row("N", "e721-statt-oder-asks-one", _said, numbers=("23",)))

# PR #722: a quantity and a card number never read into each other.
for _said, _meant in (
    ("S40 zwei Portionen", "s40"),
    ("die S40 zwei Portionen", "s40"),
    ("Sushi vierzig zwei Portionen", "s40"),
    ("S 20 2 Portionen", "s20"),
    ("S40 zwei mal", "s40"),
    ("S20 2x", "s20"),
):
    RED.append(
        row(
            "N",
            "e722-prefixed-number-with-unit",
            _said,
            numbers=(_meant,),
            no_notfound=True,
        )
    )
for _said, _meant in (
    ("Sushi vierzig zwei Portionen", "s40"),
    ("Sushi zwanzig zwei Portionen", "s20"),
    ("Sushi vierzig 2x", "s40"),
):
    RED.append(
        row("N", "e722-category-word-number-with-unit", _said, numbers=(_meant,))
    )
for _said in ("Nummer 40 zwei bitte", "die 40 zwei, bitte"):
    RED.append(row("N", "e722-tens-number-bare-count", _said, numbers=("40",)))
for _said in (
    "die 23 x die 24",
    "die 13 x die 12",
    "die dreizehn mal die zwölf",
    "die 13 mal Nummer 12",
):
    _nums: tuple[str, ...] = ("23", "24") if "23" in _said else ("13", "12")
    RED.append(
        row("N", "e722-article-number-times-article-number", _said, numbers=_nums)
    )

# --- clean rows: what the review checked and found holding -----------------------

CLEAN: list[tuple] = []

# PR #720
for _said in (
    "die 47 ohne Huhn, dafür Tofu, ohne Zwiebeln",
    "die 47 mit Nudeln, ohne Zwiebeln, mit Tofu, ohne Koriander",
    "die 47 ohne Zwiebeln, dafür",
    "die 47 ohne Zwiebeln, lieber scharf",
    "die 47 ohne eine Zwiebel",
    "die 47 mit Ei, zweimal",
    "die 47 mit Ei 2x",
    "zweimal die 47 ohne Zwiebeln mit extra Ei",
    "die 47 zweimal ohne Zwiebeln mit extra Ei",
    "die 47 Tofu statt Huhn",
    "die 47 Tofu anstatt Huhn",
    "die 47 Tofu statt Huhn, ohne Zwiebeln",
    "Nummer 47 mit 2x Tofu",
):
    CLEAN.append(
        row("O", "e720-clean", _said, numbers=("47",), options=("tofu", "ei", "nudeln"))
    )
# PR #721
for _said in (
    "die 23 und Ente",
    "S12 und Ente",
    "SM1 und Ente",
    "25G und Ente",
    "die 25 G und Ente",
    "die dreiundzwanzig und Ente",
    "Ente, die 23",
    "die 23, Pho Bo und die 12",
):
    _num = {
        "die 23 und Ente": "23",
        "S12 und Ente": "s12",
        "SM1 und Ente": "sm1",
        "25G und Ente": "25g",
        "die 25 G und Ente": "25g",
        "die dreiundzwanzig und Ente": "23",
        "Ente, die 23": "23",
    }.get(_said, "23")
    CLEAN.append(row("N", "e721-clean-separate-positions", _said, numbers=(_num,)))
for _said, _nums in (
    ("die 23, äh, Pho Bo", ("23",)),
    ("Pho Bo, ähm, die 23", ("23",)),
    ("die 23, nee, Pho Bo", ("23",)),
    ("die 23, nein Pho Bo", ("23",)),
    ("Nummer 23 Pho Bo", ("23",)),
    ("S12 Pho Bo", ("s12",)),
    ("S12 äh Pho Bo", ("s12",)),
    ("die 23 für 8,50", ("23",)),
    ("die 23,24", ("23", "24")),
    ("die 25,G", ("25", "25g")),
    ("Pho Bo,23", ("23",)),
    ("die 23, nee, die 24", ("23", "24")),
    ("die 23, nein, die 24", ("23", "24")),
    # A spoken correction between two numbers asks (review #207, regression
    # of #721, red rows until #911).
    ("die 23, doch die 24", ("23", "24")),
    ("die 23, ne, die 24", ("23", "24")),
    ("die 23, quatsch, die 24", ("23", "24")),
    ("die 23, sorry, die 24", ("23", "24")),
    ("die 23, Moment, die 24", ("23", "24")),
):
    CLEAN.append(
        row(
            "N",
            "e721-clean-guard-asks",
            _said,
            numbers=_nums,
            either=_said.startswith(("die 23,24", "die 25,G"))
            or _said.endswith("die 24"),
        )
    )
# PR #722
for _said, _num in (
    ("Nummer 40 zwei Portionen", "40"),
    ("Nummer 40 2 Portionen", "40"),
    ("Nummer hundert zwei Portionen", "100"),
    ("die 40 zweimal", "40"),
    ("die vierzig zwei mal", "40"),
    ("Nummer 40 zwei Stück", "40"),
    ("Nummer 40 zwei Stk", "40"),
    ("Nummer 40 zwei Portion", "40"),
    ("Nummer S40 zwei Portionen", "s40"),
    ("zweimal S40", "s40"),
    ("zwei Portionen S40", "s40"),
    ("S40 bitte, zwei Portionen", "s40"),
    ("zwei mal die 13", "13"),
    ("zweimal 13", "13"),
    ("die 13 2x", "13"),
    ("2 x S12", "s12"),
    ("die dreizehn mal zwei", "13"),
    ("die 13 x 2", "13"),
    ("13 x 2", "13"),
    ("13x2", "13"),
    ("die zwölf mal drei", "12"),
    ("S12 x 2", "s12"),
    ("Nummer 13 x 2", "13"),
    ("und die 13 x 2", "13"),
    ("zwei 23", "23"),
    ("zwei die 23", "23"),
    ("Nummer 40 zwei Teller", "40"),
    ("Nummer 40 zwei davon", "40"),
):
    CLEAN.append(row("N", "e722-clean", _said, numbers=(_num,)))
# PR #735
for _said, _who in (
    ("die 13, ich habe keine Allergie, nur meine Frau gegen Sesam", ("sesam",)),
    (
        "die 13, ich habe keine Allergie, aber meine Frau hat eine Sesamallergie",
        ("sesam",),
    ),
    ("die 13, meine Tochter ist gegen Erdnuss allergisch", ("erdnuss",)),
    (
        "die 13, ich habe keine Allergie, nur meine Frau gegen Sesam und Nüsse",
        ("sesam", "nüsse"),
    ),
    (
        "die 13, ich habe keine Allergie, nur meine Frau gegen Sesam, ich gegen Nüsse",
        ("sesam", "nüsse"),
    ),
    ("die 13, ich habe keine Allergie, nur meine Frau nicht gegen Sesam", ("sesam",)),
    ("die 13, ich habe keine Allergie, nur meine Frau gegen alles", ("alles",)),
    (
        "die 13, ich habe keine Allergie, nur meine Frau gegen Sesam, ohne Erdnuss",
        ("sesam",),
    ),
    ("die 13, ich habe keine Allergie, nur meine Freundin gegen Sesam", ("sesam",)),
    (
        "die 13, ich habe keine Allergie, nur meine Kollegin gegen Sesam, glaube ich",
        ("sesam",),
    ),
):
    CLEAN.append(row("A", "e735-clean", _said, numbers=("13",), allergies=_who))
# PR #735: the caller's own allergy in front of the denial is kept, not lost
# behind the other one's allergen (regression of #735, review #207, fixed #911).
for _said, _lost in (
    (
        "Pho Bo, ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam",
        "nuss",
    ),
    (
        "die 13, meine Tochter hat eine Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "erdnuss",
    ),
    (
        "die 13, mein Sohn ist gegen Erdnuss allergisch. Ich habe keine Allergie, aber meine Frau gegen Sesam",
        "erdnuss",
    ),
    (
        "die 13, Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "erdnuss",
    ),
    (
        "die 13, ich habe Zöliakie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "zöliakie",
    ),
    (
        "die 13, ich vertrage keine Nüsse. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "nüsse",
    ),
    (
        "die 13, ich bin laktoseintolerant. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "laktose",
    ),
    # Words no health reader knows: what stands in front of the denial is
    # whitelisted, not read (review of #911).
    (
        "Pho Bo, ich meide Gluten. Ich habe keine andere Allergie, nur meine Frau gegen Sesam",
        "gluten",
    ),
    (
        "die 13, ich muss glutenfrei essen. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "gluten",
    ),
    # As the ear may write it, with the umlaut decomposed.
    (
        unicodedata.normalize(
            "NFD",
            "die 13, ich habe Zöliakie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        ),
        unicodedata.normalize("NFD", "zöliakie"),
    ),
):
    CLEAN.append(
        row(
            "A",
            "e735-allergy-before-denial",
            _said,
            numbers=("13",),
            allergies=(_lost,),
        )
    )


# --- generated block: dish form x residue x second element ------------------------

# (said, card numbers it holds, option of the dish named in it)
DISH_FORMS = (
    ("die 23", ("23",)),
    ("S12", ("s12",)),
    ("Pho Bo", ()),
    ("die 47", ("47",)),
)
RESIDUES = ("", "bitte", "äh", "blorfzt")
SECONDS = (
    ("", (), (), ()),
    ("und die 24", ("24",), (), ()),
    ("und Pho Bo", (), (), ()),
    ("ohne Zwiebeln", (), (), ()),
    ("plus Tofu", (), ("tofu",), ()),
    ("ich habe eine Nussallergie", (), (), ("nuss",)),
)
GENERATED = []
for (_dish, _nums), _res, (_sec, _more, _opt, _all) in itertools.product(
    DISH_FORMS, RESIDUES, SECONDS
):
    if _opt and _dish != "die 47":
        continue  # an option only exists on the dish that has it
    _said = " ".join(x for x in (_dish, _res, _sec) if x)
    GENERATED.append(
        (
            _said,
            Spec(
                numbers=_nums + _more,
                options=_opt,
                allergies=_all,
                residue=("blorfzt",) if _res == "blorfzt" else (),
            ),
        )
    )

# Rows that break a property on main today, by said sentence: the ratchet the
# gate PR (step 2) shrinks. Read off a run on main (value: the properties broken).
GENERATED_RED: dict[str, str] = {
    "die 23 blorfzt und die 24": "N,P",
    "die 23 blorfzt und Pho Bo": "N",
    "S12 blorfzt und die 24": "N,P",
    "Pho Bo blorfzt und die 24": "N,P",
    "die 47 blorfzt und die 24": "N,P",
    "die 47 blorfzt und Pho Bo": "N",
}


def _marked(finding: str, prop: str):
    return pytest.mark.xfail(strict=True, reason=finding, raises=AssertionError)


def _params(rows, red: bool):
    return [
        pytest.param(
            prop,
            said,
            spec,
            id=f"{finding}:{said}",
            marks=_marked(finding, prop) if red else (),
        )
        for prop, finding, said, spec in rows
    ]


@pytest.mark.parametrize(("prop", "said", "spec"), _params(RED, True))
def test_red_row(session, tenant_id, prop, said, spec):
    msg = CHECKS[prop](read(session, tenant_id, said), spec)
    assert msg is None, msg


@pytest.mark.parametrize(("prop", "said", "spec"), _params(CLEAN, False))
def test_clean_row(session, tenant_id, prop, said, spec):
    msg = CHECKS[prop](read(session, tenant_id, said), spec)
    assert msg is None, msg


@pytest.mark.parametrize(
    ("said", "spec"),
    [
        pytest.param(
            said,
            spec,
            id=said,
            marks=pytest.mark.xfail(
                strict=True,
                reason=f"generated {GENERATED_RED[said]}",
                raises=AssertionError,
            )
            if said in GENERATED_RED
            else (),
        )
        for said, spec in GENERATED
    ],
)
def test_generated_row(session, tenant_id, said, spec):
    broken = violated(read(session, tenant_id, said), spec)
    assert not broken, broken


def test_row_counts():
    """The baseline for the over-ask measurement of the gate PR."""
    red = Counter(prop for prop, *_ in RED)
    green = Counter(prop for prop, *_ in CLEAN)
    gen_red = Counter(p for v in GENERATED_RED.values() for p in v.split(","))
    total = len(RED) + len(CLEAN) + len(GENERATED)
    print(
        f"\ncorpus rows {total}: red {len(RED)} {dict(red)}, clean {len(CLEAN)} {dict(green)}, "
        f"generated {len(GENERATED)} (xfail {len(GENERATED_RED)} {dict(gen_red)})"
    )
    assert total == len(RED) + len(CLEAN) + len(GENERATED)


def test_an_open_wish_does_not_hide_a_dropped_allergy() -> None:
    # An "open" wish (one option in two groups) has no ingredient either; it
    # must not wave the allergy check through (Graphify on PR #792).
    open_wish = {"kind": "open", "text": "mit reis", "ingredient": None}
    reading = Reading([Pos("clear", ["13"], open_wish)], "")
    assert broken_a(reading, Spec(allergies=("erdnuss",))) is not None
    kept = {"kind": "allergy", "text": "erdnuss allergie", "ingredient": None}
    reading = Reading([Pos("clear", ["13"], kept)], "")
    assert broken_a(reading, Spec(allergies=("erdnuss",))) is None
