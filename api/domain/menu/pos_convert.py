"""Convert the register's articles into the CSV files described in docs/14 (T-4.11).

The register is the master for menu and prices (docs/02 §6). This turns its
tables into what `importer.parse` expects; importing still happens only
through the one import. Every interpretation comes from the real files and the
register's screens (docs/14 §Quelle Kasse). What cannot be interpreted
unambiguously is not taken over and appears as an error in the report - never
guessed (CLAUDE.md §2).

Sizes: `GROESSE` is a string. `+` allows extras, `-` allows leaving things out,
the digits are the sizes the article is sold in. Size 1 costs `VK1_PREIS`,
size n from 2 on costs `VK1_PREIS + GRPREIS(n-1)`. Verified on the register's
screen (Suppe 1: small 6.50, large 11.00 with VK1 6.50, GRPREIS1 0, GRPREIS2
4.50) and on the wine (6.50 + 23.50 = 30.00, which is what the bottle costs as
an article of its own).

Extras: `zutaten` are attached to the article through article groups
(`WRGSHOWALL`, or `WRG` in the list `WRGSHOW`). Price = price level
`ZPREIGRP3` from `zutgrp` plus `ZGRPREIS(n-1)` for size n from 2 on.
"""

import csv
import io
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from api.domain.menu.importer import (
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    format_eur,
    is_card_number,
    parse_eur,
)
from api.domain.menu.items import option_key
from api.domain.menu.numberwords import canonical_card, fold
from api.domain.menu.pos_dbf import DbfError, Table

# Names from the register's screen ("Größen Bezeichnung ändern"), Maxi
# 26.09.2026. 5 to 7 have no name there: such sizes are not taken over.
SIZE_NAMES = {1: "normal", 2: "klein", 3: "groß", 4: "party"}
SIZE_GROUP = "Größe"
EXTRAS_GROUP = "Extras"
PLACEHOLDER = "000"
NAME_MAX = 40  # BEZEICH C40: full length probably means cut off
EXTRA_NAME_MAX = 16  # ZBEZEICH C16

# The register counts the 14 main allergens a to n without a gap; LMIV and
# docs/03 skip I, J, K, Q. Never take them over 1:1 (docs/14, mapping table).
ALLERGEN_MAP = {
    "A": "A",
    "B": "B",
    "C": "C",
    "D": "D",
    "E": "E",
    "F": "F",
    "G": "G",
    "H": "H",
    "I": "L",
    "J": "M",
    "K": "N",
    "L": "O",
    "M": "P",
    "N": "R",
}
# Only for the warnings to people (docs/14), never a source for allergens. The
# 14 LMIV allergens by database letter (api/models/menu.py) as a dish name or a
# recipe would spell them, in `fold` spelling, German and English: the menu has
# both. A word that starts with one of these names the whole letter.
ALLERGEN_NAMES = {
    "A": ("gluten",),
    "B": ("krebstier",),
    "C": ("ei", "eier", "egg", "eggs"),
    "D": ("fisch", "fish"),
    "E": ("erdnuss", "erdnuesse", "peanut"),
    "F": ("soja", "soy"),
    "G": ("milch", "milk", "dairy"),
    "H": ("nuss", "nuesse", "schalenfrucht", "schalenfruechte", "nut", "nuts"),
    "L": ("sellerie", "celery"),
    "M": ("senf", "mustard"),
    "N": ("sesam",),
    "O": ("sulfit", "sulphit", "schwefel"),
    "P": ("lupine",),
    "R": ("weichtier",),
}
# One kind or one carrier of a letter. "laktosefrei" does not say "no milk
# allergen", "weizenfrei" not "no gluten": the letter can still be right.
ALLERGEN_KINDS = {
    "A": ("weizen", "roggen", "gerste", "hafer", "dinkel", "wheat"),
    "B": ("garnele", "krabbe", "shellfish"),
    "G": ("laktose", "lactose", "sahne", "kaese"),
    "H": ("mandel", "cashew", "kaschu", "pistazie", "macadamia"),
    "R": ("tintenfisch",),
}
# Short names count only as a whole word ("Eis", "Reis", "Minute") or only at
# the start of one ("Feier" is no egg, "Eiernudeln" are). Every other name
# counts anywhere inside a word ("haselnussfrei").
_WHOLE_WORD_ONLY = frozenset(("ei", "egg", "eggs", "nut", "nuts"))
_PREFIX_ONLY = frozenset(("eier",))
# Look like an allergen and are none.
_LOOKALIKES = ("kokos", "muskat", "buchweizen", "buckwheat")
_NAMES = tuple(
    (name, code, kind)
    for kind, table in ((False, ALLERGEN_NAMES), (True, ALLERGEN_KINDS))
    for code, names in table.items()
    for name in names
)
# A recipe (ZUTATEN) names a carrier while ALLERGENE is empty: the same words
# at the start of a word, plus two that belong to no single letter in a name.
_CARRIERS = (
    *(n for n, _, _ in _NAMES if n not in _WHOLE_WORD_ONLY),
    "mehl",
    "nudel",
)
_REGISTER_LETTER = {db: register.lower() for register, db in ALLERGEN_MAP.items()}
# "glutenfrei", "glutenfreie", English "free".
_FREE = re.compile(r"(.*)fre(?:i(?:e[mnrs]?)?|e)")
# "vegan" claims the animal allergens absent.
_VEGAN = re.compile(r"vegan(?:e[mnrs]?)?")
_VEGAN_CODES = frozenset("BCDGR")
_WITHOUT = ("ohne", "without", "no")
_JOINS = ("und", "oder", "and", "or")
# A new part of the name starts: the list behind "ohne" ends.
_STOPS = (
    "mit",
    "in",
    "im",
    "auf",
    "an",
    "zu",
    "dazu",
    "aber",
    "nach",
    "bei",
    "statt",
    "fuer",
    "with",
)
# "auch vegan", "auf Wunsch vegan", "vegan möglich": a variant on offer.
_OFFER_BEFORE = ("nicht", "auch", "wunsch")
_OFFER_AFTER = ("moeglich",)
# Columns the converter reads directly. If one is missing (other register
# version, wrong file), that is a format error instead of a KeyError (Codex PR
# #149). Optional columns (VK2_PREIS, GRPREIS*, A_PREIS*, ALLERGENE, ZUTATEN,
# ZGRPREIS*) are read with a default.
REQUIRED_COLUMNS = {
    "artikel": ("ARTNR", "BEZEICH", "WRG", "VK1_PREIS", "GROESSE"),
    "warengrp": ("W_WRG", "W_BEZEICH"),
    "zutaten": ("ZBEZEICH", "WRGSHOWALL", "WRGSHOW", "ZPREIGRP3"),
    "zutgrp": ("ZGRP3", "ZPREIS"),
}
_SORT_PREFIX = re.compile(r"^[A-Z]\.")
# dBase logical: T, t, Y and y mean true.
_TRUE = frozenset("TtYy")


@dataclass
class Conversion:
    menu: list[dict[str, str]] = field(default_factory=list)
    options: list[dict[str, str]] = field(default_factory=list)
    allergens: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_placeholder: int = 0
    skipped_deleted: int = 0
    skipped_groups: int = 0
    allergens_found: list[str] = field(default_factory=list)

    def csv_files(self) -> dict[str, str]:
        return {
            MENU_FILE: _csv(
                ("number", "pos_code", "name", "category", "price_eur", "active"),
                self.menu,
            ),
            OPTIONS_FILE: _csv(
                (
                    "number",
                    "group_name",
                    "option_name",
                    "price_delta_eur",
                    "is_default",
                    "required",
                ),
                self.options,
            ),
            ALLERGENS_FILE: _csv(
                ("number", "allergen_codes", "confirmed_by"), self.allergens
            ),
        }

    def as_text(self) -> str:
        lines = [
            f"Gerichte übernommen: {len(self.menu)}, Optionen: {len(self.options)}",
            f"Nicht übernommen: gesperrt (gelöscht) {self.skipped_deleted}, "
            f"Warengruppe ohne Telefonbestellung {self.skipped_groups}, "
            f"Platzhalter {self.skipped_placeholder}",
        ]
        if self.allergens_found:
            lines.append(
                "Allergene aus der Kasse (bitte prüfen): "
                + ", ".join(self.allergens_found)
            )
        lines += [f"Fehler: {e}" for e in self.errors]
        lines += [f"Warnung: {w}" for w in self.warnings]
        return "\n".join(lines)


def _csv(columns: tuple[str, ...], rows: Iterable[dict[str, str]]) -> str:
    return _csv_rows(list(columns), [[row[c] for c in columns] for row in rows])


def cents(value: str) -> int | None:
    """ "6.50", "-12.5", "7,90" -> Cent. Leer -> 0. None, wenn nicht lesbar."""
    value = value.strip()
    return parse_eur(value.replace(".", ",")) if value else 0


def _sizes(code: str) -> list[int]:
    # ASCII 1-9 only: "²" is a digit for isdigit(), but not for int().
    return sorted({int(c) for c in code if c in "123456789"})


def _size_price(base: int, row: dict[str, str], prefix: str, size: int) -> int | None:
    """Preis bzw. Aufschlag in Größe `size`: Grundwert plus Spalte size-1."""
    if size == 1:
        return base
    # Empty or a missing column is not a surcharge of 0: otherwise the size
    # would silently cost the base price (review T-4.11).
    raw = row.get(f"{prefix}{size - 1}", "")
    extra = cents(raw) if raw.strip() else None
    return None if extra is None else base + extra


def extra_name(raw: str) -> str:
    """ "B.Mango_Curry" -> "Mango Curry": the prefix only sorts in the register."""
    return " ".join(_SORT_PREFIX.sub("", raw).replace("_", " ").split())


@dataclass(frozen=True)
class _Extra:
    name: str
    groups: frozenset[str] | None  # None: for all article groups
    base: int
    row: dict[str, str]


def convert(
    artikel: Table,
    warengrp: Table,
    zutaten: Table,
    zutgrp: Table,
    *,
    skip_groups: Iterable[str] = (),
    allergens_confirmed_by: str | None = None,
) -> Conversion:
    """Die vier Tabellen in Zeilen für die drei CSV-Dateien umwandeln."""
    tables = {
        "artikel": artikel,
        "warengrp": warengrp,
        "zutaten": zutaten,
        "zutgrp": zutgrp,
    }
    for name, table in tables.items():
        present = {f.name for f in table.fields}
        missing = [c for c in REQUIRED_COLUMNS[name] if c not in present]
        if missing:
            raise DbfError(f"{name}: Spalte fehlt: {', '.join(missing)}")
    # " " is not a checker: the import would strip the value and reject (Codex PR #149).
    allergens_confirmed_by = (allergens_confirmed_by or "").strip() or None
    result = Conversion()
    skip = {g.strip() for g in skip_groups}
    categories = _unique(warengrp, "warengrp", lambda r: r["W_WRG"], "W_BEZEICH")
    extras = _extras(result, zutaten, zutgrp)

    candidates: list[dict[str, str]] = []
    for row, deleted in zip(artikel.rows, artikel.deleted, strict=True):
        if deleted:
            result.skipped_deleted += 1
        elif row["ARTNR"] == PLACEHOLDER and not row["BEZEICH"]:
            result.skipped_placeholder += 1
        elif row["WRG"] in skip:
            result.skipped_groups += 1
        else:
            candidates.append(row)

    seen: dict[str, int] = {}
    for row in candidates:
        key = canonical_card(row["ARTNR"])
        seen[key] = seen.get(key, 0) + 1

    truncated: list[str] = []
    promo: list[str] = []
    bad_numbers: list[str] = []
    unnamed_sizes: dict[int, list[str]] = {}
    carriers: list[str] = []
    free_from: list[str] = []
    contradicted: list[str] = []
    to_check: list[str] = []
    for row in candidates:
        pos_code = row["ARTNR"]
        where = f"Artikel {pos_code or '(ohne Nummer)'}"
        if not pos_code:
            result.errors.append(f"{where}: Nummer fehlt")
            continue
        if not is_card_number(pos_code.lower()):
            bad_numbers.append(pos_code)
            continue
        if seen[canonical_card(pos_code)] > 1:
            result.errors.append(f"{where}: Nummer doppelt in der Kasse")
            continue
        number = pos_code.lower()
        name = row["BEZEICH"]
        category = categories.get(row["WRG"])
        vk1 = cents(row["VK1_PREIS"]) if row["VK1_PREIS"] else None
        vk2 = cents(row.get("VK2_PREIS", ""))
        problems = []
        if not name:
            problems.append("Name fehlt")
        if not category:
            problems.append(f"Warengruppe „{row['WRG']}“ unbekannt")
        if vk1 is None or vk1 < 0:
            problems.append(f"VK1_PREIS „{row['VK1_PREIS']}“ nicht lesbar")
        elif row.get("VK2_PREIS") and vk2 not in (0, vk1):
            # 0 means not maintained in the register, then VK1 applies (review
            # T-4.11). Phone is pickup: the register would take VK2, the agent
            # quotes VK1.
            problems.append(
                f"VK2_PREIS {row['VK2_PREIS']} weicht von VK1_PREIS "
                f"{row['VK1_PREIS']} ab"
            )
        if problems:
            result.errors.append(f"{where}: {'; '.join(problems)}")
            continue
        assert vk1 is not None and category is not None

        code = row["GROESSE"]
        sizes = _sizes(code)
        named = [s for s in sizes if s in SIZE_NAMES]
        unnamed = [s for s in sizes if s not in SIZE_NAMES]
        if not named:
            result.errors.append(
                f"{where}: keine Größe mit Namen in GROESSE „{code}“ "
                f"(Namen gibt es für {', '.join(map(str, SIZE_NAMES))})"
            )
            continue
        prices = {s: _size_price(vk1, row, "GRPREIS", s) for s in named}
        bad = [s for s, p in prices.items() if p is None or p < 0]
        if bad:
            result.errors.append(
                f"{where}: Preis für Größe {', '.join(SIZE_NAMES[s] for s in bad)} "
                "nicht lesbar oder negativ"
            )
            continue
        for size in unnamed:
            unnamed_sizes.setdefault(size, []).append(pos_code)
        default = named[0]
        price = prices[default]
        assert price is not None
        if price == 0:
            result.warnings.append(f"{where}: Preis 0,00")
        if len(name) >= NAME_MAX:
            truncated.append(pos_code)
        if any((cents(row.get(f"A_PREIS{i}", "")) or 0) != 0 for i in range(1, 7)):
            promo.append(pos_code)

        result.menu.append(
            {
                "number": number,
                "pos_code": pos_code,
                "name": name,
                "category": category,
                "price_eur": format_eur(price),
                "active": "ja",
            }
        )
        if len(named) > 1:
            for size in named:
                size_price = prices[size]
                assert size_price is not None
                result.options.append(
                    _option(
                        number,
                        SIZE_GROUP,
                        SIZE_NAMES[size],
                        size_price - price,
                        default=size == default,
                        required=True,
                    )
                )
        if "+" in code:
            _add_extras(result, where, number, row["WRG"], named, extras)
        _add_allergens(result, where, number, row, allergens_confirmed_by, carriers)
        claimed, whole = _claims(name)
        if claimed and not result.allergens[-1]["allergen_codes"]:
            free_from.append(pos_code)
        # Against the register's letters, confirmed or not: the agent would
        # read the claim aloud and get_item_details would name the allergen
        # (Codex PR #175).
        register = {ALLERGEN_MAP.get(c) for c in row.get("ALLERGENE", "").upper()}
        if clash := whole & register:
            contradicted.append(_with_letters(pos_code, clash))
        elif clash := claimed & register:
            to_check.append(_with_letters(pos_code, clash))

    if bad_numbers:
        result.errors.append(
            "Nummer versteht die Suche nicht (bis 999, optional a bis g dahinter und "
            "bis zu zwei Buchstaben davor, aber kein Praefix wie x, st, nr, no, ja, "
            "es), "
            f"{len(bad_numbers)} Gerichte nicht übernommen: " + ", ".join(bad_numbers)
        )
    for size, numbers in sorted(unnamed_sizes.items()):
        result.warnings.append(
            f"Größe {size} hat in der Kasse keinen Namen, nicht übernommen bei: "
            + ", ".join(numbers)
        )
    if truncated:
        result.warnings.append(
            f"Name mit {NAME_MAX} Zeichen, vermutlich abgeschnitten (der Agent liest "
            "ihn vor, in der Kasse kürzen): " + ", ".join(truncated)
        )
    if promo:
        result.warnings.append(
            "Aktionspreis A_PREIS gesetzt, wird nicht übernommen: " + ", ".join(promo)
        )
    if carriers:
        result.warnings.append(
            "ZUTATEN nennt einen Allergenträger, ALLERGENE ist leer (Agent sagt "
            "weiter „keine Auskunft“): " + ", ".join(carriers)
        )
    if free_from:
        result.warnings.append(
            'Name carries a "free from" claim that the agent would read aloud, but '
            "no allergens are maintained (rename the dish in the register, or "
            "maintain and confirm its allergens): " + ", ".join(free_from)
        )
    if contradicted:
        result.warnings.append(
            'Name carries a "free from" claim that contradicts the allergens in '
            "the register (register letter -> database letter in brackets; "
            "correct the name or the allergens in the register): "
            + ", ".join(contradicted)
        )
    if to_check:
        result.warnings.append(
            'Name carries a "free from" claim for one kind of an allergen the '
            "register has (register letter -> database letter in brackets; both "
            "can be right, check them): " + ", ".join(to_check)
        )
    for category in sorted({row["category"] for row in result.menu}):
        if free_from_claim(category):
            result.warnings.append(
                f'Warengruppe „{category}“: name carries a "free from" claim, the '
                "agent names the category and no allergen data backs it"
            )
    return result


def _unique(
    table: Table, name: str, key: Callable[[dict[str, str]], str], column: str
) -> dict[str, str]:
    """Key -> value from the active rows. The same key with a different value
    is a format error: otherwise the row order would decide a price or a
    category (Codex PR #149)."""
    values: dict[str, str] = {}
    for row in table.live():
        k = key(row)
        if k in values and values[k] != row[column]:
            raise DbfError(
                f"{name}: Schlüssel „{k}“ doppelt mit verschiedenem {column} "
                f"(„{values[k]}“ und „{row[column]}“)"
            )
        values[k] = row[column]
    return values


def _option(
    number: str, group: str, name: str, delta: int, *, default: bool, required: bool
) -> dict[str, str]:
    return {
        "number": number,
        "group_name": group,
        "option_name": name,
        "price_delta_eur": format_eur(delta),
        "is_default": "ja" if default else "nein",
        "required": "ja" if required else "nein",
    }


def _extras(result: Conversion, zutaten: Table, zutgrp: Table) -> list[_Extra]:
    levels = _unique(zutgrp, "zutgrp", lambda r: r["ZGRP3"], "ZPREIS")
    extras: list[_Extra] = []
    for row in zutaten.live():
        where = f"Zutat „{row['ZBEZEICH']}“"
        name = extra_name(row["ZBEZEICH"])
        level = row["ZPREIGRP3"]
        # An empty ZPREIS is not a price of 0: otherwise the extra would silently be free.
        raw_price = levels.get(level, "")
        base = cents(raw_price) if raw_price else None
        if not name:
            result.errors.append(f"{where}: Name fehlt")
            continue
        if base is None:
            result.errors.append(
                f"{where}: Preisstufe ZPREIGRP3 „{level}“ fehlt in zutgrp oder hat "
                "keinen lesbaren Preis, nicht übernommen"
            )
            continue
        if free_from_claim(name):
            result.warnings.append(
                f'{where}: name carries a "free from" claim, the agent offers the '
                "extra and no allergen data backs it"
            )
        if len(row["ZBEZEICH"]) >= EXTRA_NAME_MAX:
            result.warnings.append(
                f"{where}: Name mit {EXTRA_NAME_MAX} Zeichen, vermutlich "
                f"abgeschnitten; der Agent sagt „{name}“"
            )
        groups = (
            None
            if row["WRGSHOWALL"] in _TRUE
            else frozenset(g.strip() for g in row["WRGSHOW"].split() if g.strip())
        )
        if groups == frozenset():
            result.warnings.append(
                f"{where}: bei keiner Warengruppe wählbar (WRGSHOWALL nicht T, "
                "WRGSHOW leer), nicht übernommen"
            )
            continue
        extras.append(_Extra(name, groups, base, row))
    return extras


def _add_extras(
    result: Conversion,
    where: str,
    number: str,
    group: str,
    sizes: list[int],
    extras: list[_Extra],
) -> None:
    # First collect all prices per key (as in the import, option_key), then
    # decide: identical duplicates count once, different prices are an error -
    # the row order does not pick a price (Codex PR #149).
    found: dict[str, list[tuple[str, int | None]]] = {}
    for extra in extras:
        if extra.groups is not None and group not in extra.groups:
            continue
        prices = {_size_price(extra.base, extra.row, "ZGRPREIS", s) for s in sizes}
        if None in prices:
            result.errors.append(
                f"{where}: Extra „{extra.name}“: Aufschlag ZGRPREIS nicht lesbar, "
                "nicht übernommen"
            )
            continue
        price = next(iter(prices)) if len(prices) == 1 else None
        found.setdefault(option_key(extra.name), []).append((extra.name, price))
    for candidates in found.values():
        name = candidates[0][0]
        prices = {price for _, price in candidates}
        if None in prices:
            # Our option has one price per dish, not per size.
            result.warnings.append(
                f"{where}: Extra „{name}“ kostet je Größe anders, nicht übernommen"
            )
            continue
        if len(prices) > 1:
            result.errors.append(
                f"{where}: Extra „{name}“ steht mehrfach mit verschiedenem Preis "
                "in zutaten, nicht übernommen"
            )
            continue
        if len(candidates) > 1:
            result.warnings.append(f"{where}: Extra „{name}“ doppelt")
        [price] = prices
        assert price is not None
        # A negative value is a deduction in the register ("ohne Fleisch") and
        # stays; draft_order rejects a negative total for the dish (Codex PR #149).
        result.options.append(
            _option(number, EXTRAS_GROUP, name, price, default=False, required=False)
        )


def _with_letters(pos_code: str, codes: set[str]) -> str:
    """ "7 (k -> N)": the register counts differently from the ninth letter on."""
    letters = ", ".join(f"{_REGISTER_LETTER[c]} -> {c}" for c in sorted(codes))
    return f"{pos_code} ({letters})"


def _allergens_named(word: str) -> tuple[set[str], set[str]]:
    """Database letters a word names, and those of them it names as a whole.

    The longest name wins: "Erdnuss" is no "Nuss", "Tintenfisch" no "Fisch".
    Only a word that starts with a name of `ALLERGEN_NAMES` stands for the
    whole letter; "Haselnuss" or "Laktose" is one kind of it.
    """
    if any(x in word for x in _LOOKALIKES):
        return set(), set()
    hits = [
        (name, code, kind)
        for name, code, kind in _NAMES
        if (
            word == name
            if name in _WHOLE_WORD_ONLY
            else word.startswith(name)
            if name in _PREFIX_ONLY
            else name in word
        )
    ]
    hits = [h for h in hits if not any(h[0] != m and h[0] in m for m, _, _ in hits)]
    return (
        {code for _, code, _ in hits},
        {code for name, code, kind in hits if not kind and word.startswith(name)},
    )


def free_from_claim(name: str) -> set[str]:
    """Database letters of the allergens the name says are absent.

    Whole words only: "ohne" inside "Bohnen" and "frei" inside "Freilandei"
    claim nothing. A heuristic for the report, never a source for allergens.
    """
    return _claims(name)[0]


def _claims(name: str) -> tuple[set[str], set[str]]:
    """The letters claimed absent, and those of them claimed as a whole."""
    claimed: set[str] = set()
    whole: set[str] = set()
    # "Gluten-frei" is "glutenfrei". A hyphen left open ("gluten- und
    # laktosefrei") stays on its word.
    text = re.sub(r"-(?=fre[ie])", "", fold(name))
    tokens = re.findall(r"[a-z]+(?:-(?![a-z]))?", text)
    words = [t.rstrip("-") for t in tokens]

    def add(word: str) -> bool:
        some, as_whole = _allergens_named(word)
        claimed.update(some)
        whole.update(as_whole)
        return bool(some)

    def add_list(start: int, fillers: int) -> None:
        # "Ei, Milch und Nüsse", "frische Erdnüsse", "Zusatz von Milch".
        for word in words[start:]:
            if word in _STOPS:
                break
            if not add(word) and word not in _JOINS and word != "von":
                fillers -= 1
                if fillers < 0:
                    break

    for i, word in enumerate(words):
        before = words[i - 1] if i else ""
        if before in _JOINS and i > 1:
            before = words[i - 2]  # "mit oder ohne Ei"
        if (
            before in _OFFER_BEFORE
            or (before == "mit" and word in _WITHOUT)
            or words[i + 1 : i + 2] in ([w] for w in _OFFER_AFTER)
        ):
            continue
        if _VEGAN.fullmatch(word):
            claimed.update(_VEGAN_CODES)
            whole.update(_VEGAN_CODES)
        elif word in _WITHOUT:
            # The Japanese "no" stands in dish names ("Tori no Karaage").
            add_list(i + 1, 0 if word == "no" else 1)
        elif free := _FREE.fullmatch(word):
            k = i - 1
            if free[1]:
                add(free[1])
            elif words[i + 1 : i + 2] in (["von"], ["from"]):
                # "Fischsuppe frei von Gluten": the claim stands behind.
                add_list(i + 2, 1)
                continue
            elif word == "free" or i + 1 == len(words) or words[i + 1] in _JOINS:
                # "Gluten frei" ends the name, "Gluten Free Roll" is English;
                # "frei wählbar" and "frei Haus" claim nothing.
                add(words[i - 1] if i else "")
                k = i - 2
            else:
                continue
            # "gluten-, ei- und sojafrei": the open hyphens share the "-frei".
            while k >= 0 and (words[k] in _JOINS or tokens[k].endswith("-")):
                add(words[k])
                k -= 1
    return claimed, whole


def _add_allergens(
    result: Conversion,
    where: str,
    number: str,
    row: dict[str, str],
    confirmed_by: str | None,
    carriers: list[str],
) -> None:
    """Immer eine Zeile je Gericht: die Kasse ist Quelle, leer = keine Auskunft."""
    raw = row.get("ALLERGENE", "").replace(" ", "").upper()
    unknown = sorted({c for c in raw if c not in ALLERGEN_MAP})
    codes = sorted({ALLERGEN_MAP[c] for c in raw if c in ALLERGEN_MAP})
    empty = {"number": number, "allergen_codes": "", "confirmed_by": ""}
    if unknown:
        result.errors.append(
            f"{where}: unbekannter Allergen-Buchstabe {', '.join(unknown)} in "
            f"„{row['ALLERGENE']}“, Gericht ohne Allergenauskunft; der Import "
            "löscht dort bestätigte Allergene"
        )
        result.allergens.append(empty)
        return
    if not codes:
        words = re.split(r"[\s,_;/()-]+", fold(row.get("ZUTATEN", "")))
        if any(w in _WHOLE_WORD_ONLY or w.startswith(_CARRIERS) for w in words):
            carriers.append(row["ARTNR"])
        result.allergens.append(empty)
        return
    result.allergens_found.append(f"{row['ARTNR']} {raw.lower()} -> {','.join(codes)}")
    if not confirmed_by:
        result.errors.append(
            f"{where}: Allergene {','.join(codes)} nicht übernommen, wer hat sie "
            "geprüft? (--allergens-confirmed-by); ohne löscht der Import dort "
            "bestätigte Allergene"
        )
        result.allergens.append(empty)
        return
    result.allergens.append(
        {
            "number": number,
            "allergen_codes": ",".join(codes),
            "confirmed_by": confirmed_by,
        }
    )


@dataclass(frozen=True)
class AliasSplit:
    kept: str
    dropped: str | None  # None: no row split off
    dropped_numbers: list[str]


def split_aliases(
    source: str | None, side: str | None, numbers: Iterable[str]
) -> AliasSplit | None:
    """Match the aliases from the chat against the register's numbers.

    An alias row for a number that is not in the new `menu_items.csv` (drink,
    blocked, number unreadable, error in the report) would block the whole
    import; deleting it would lose knowledge that has grown. `source` is the
    alias file, `side` the file of the rows split off: both are divided anew
    on every run, so the aliases of a dish that was missing for just one run
    come back by themselves (review T-4.11). If the chat has its own rows for
    a number in `source` by now, only those apply; the old ones from `side`
    are dropped instead of coming back.

    Rows stay lists, not dicts: one field too many ("1;Miso; warm") stays as
    it is. None: one file has no column `number`, or other columns than the
    other one - then nothing is touched.
    """
    known = {canonical_card(n) for n in numbers}
    header: list[str] | None = None
    parts: list[list[list[str]]] = []
    for text in (source, side):
        if text is None:
            parts.append([])
            continue
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
        head = [h.strip() for h in next(reader, [])]
        # Both mandatory columns of the import, otherwise import_menu would not
        # read the file (Codex PR #149).
        if head and (
            "number" not in head
            or "alias" not in head
            or (header is not None and head != header)
        ):
            return None
        header = header or head or None
        parts.append([row for row in reader if any(cell.strip() for cell in row)])
    if header is None:
        return None
    index = header.index("number")

    def key(row: list[str]) -> str:
        return canonical_card(row[index].strip()) if index < len(row) else ""

    in_source = {key(row) for row in parts[0]}
    rows: list[list[str]] = []
    unique: set[tuple[str, ...]] = set()
    # If the chat has its own rows for a number, only those apply - even if the
    # dish is still missing, otherwise the old ones would come back later (Codex PR #149).
    for row in parts[0] + [r for r in parts[1] if key(r) not in in_source]:
        if tuple(row) not in unique:
            unique.add(tuple(row))
            rows.append(row)
    kept: list[list[str]] = []
    dropped: list[list[str]] = []
    for row in rows:
        number = row[index].strip() if index < len(row) else ""
        (kept if number and canonical_card(number) in known else dropped).append(row)
    numbers_dropped = sorted({r[index].strip() for r in dropped if index < len(r)})
    return AliasSplit(
        _csv_rows(header, kept),
        _csv_rows(header, dropped) if dropped else None,
        numbers_dropped,
    )


def _csv_rows(header: list[str], rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()
