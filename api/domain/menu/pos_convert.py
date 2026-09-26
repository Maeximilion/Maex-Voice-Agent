"""Artikel der Kasse in die CSV-Dateien nach docs/14 umwandeln (T-4.11).

Die Kasse ist Master für Karte und Preise (docs/02 §6). Hier entsteht aus ihren
Tabellen, was `importer.parse` erwartet; eingespielt wird weiter nur über den
einen Import. Jede Deutung stammt aus den echten Dateien und der Maske der Kasse
(docs/14 §Quelle Kasse). Was sich nicht eindeutig deuten lässt, wird nicht
übernommen und steht als Fehler im Bericht - nie geraten (CLAUDE.md §2).

Größen: `GROESSE` ist eine Zeichenfolge. `+` erlaubt Extras, `-` Weglassen, die
Ziffern sind die Größen, in denen der Artikel verkauft wird. Größe 1 kostet
`VK1_PREIS`, Größe n ab 2 kostet `VK1_PREIS + GRPREIS(n-1)`. Belegt an der Maske
(Suppe 1: klein 6,50, groß 11,00 bei VK1 6,50, GRPREIS1 0, GRPREIS2 4,50) und am
Wein (6,50 + 23,50 = 30,00, so viel kostet die Flasche als eigener Artikel).

Extras: `zutaten` hängen über Warengruppen am Artikel (`WRGSHOWALL` oder `WRG`
in der Liste `WRGSHOW`). Preis = Preisstufe `ZPREIGRP3` aus `zutgrp` plus
`ZGRPREIS(n-1)` für Größe n ab 2.
"""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from api.domain.menu.importer import (
    ALLERGENS_FILE,
    MENU_FILE,
    OPTIONS_FILE,
    is_card_number,
)
from api.domain.menu.numberwords import canonical_card
from api.domain.menu.pos_dbf import Table

# Namen aus der Maske der Kasse ("Größen Bezeichnung ändern"), Maxi 26.09.2026.
# 5 bis 7 haben dort keinen Namen: solche Größen werden nicht übernommen.
SIZE_NAMES = {1: "normal", 2: "klein", 3: "groß", 4: "party"}
SIZE_GROUP = "Größe"
EXTRAS_GROUP = "Extras"
PLACEHOLDER = "000"
NAME_MAX = 40  # BEZEICH C40: volle Länge heißt vermutlich abgeschnitten
EXTRA_NAME_MAX = 16  # ZBEZEICH C16

# Kasse zählt die 14 Hauptallergene a bis n ohne Lücke, LMIV und docs/03
# überspringen I, J, K, Q. Nie 1:1 übernehmen (docs/14, Umsetztabelle).
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
# Nur für die Warnung an Menschen (docs/14): Rezeptur nennt einen typischen
# Allergenträger, ALLERGENE ist leer. Der Agent sagt weiter "keine Auskunft".
_CARRIERS = (
    "eier",
    "soja",
    "weizen",
    "mehl",
    "nudel",
    "erdnuss",
    "sesam",
    "milch",
    "sahne",
    "käse",
    "fisch",
    "garnele",
    "krabbe",
    "sellerie",
    "senf",
    "nuss",
    "cashew",
    "mandel",
    "tintenfisch",
)
_MONEY = re.compile(r"^(-?)(\d*)(?:[.,](\d{1,2}))?$")
_SORT_PREFIX = re.compile(r"^[A-Z]\.")


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
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, delimiter=";", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def cents(value: str) -> int | None:
    """ "6.50", "-12.5", "7,90" -> Cent. Leer -> 0. None, wenn nicht lesbar."""
    value = value.strip()
    if not value:
        return 0
    match = _MONEY.match(value)
    if match is None or not (match.group(2) or match.group(3)):
        return None
    sign, euros, frac = match.groups()
    total = int(euros or "0") * 100 + int((frac or "0").ljust(2, "0"))
    return -total if sign else total


def _eur(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100},{value % 100:02d}"


def _sizes(code: str) -> list[int]:
    return sorted({int(c) for c in code if c.isdigit() and c != "0"})


def _size_price(base: int, row: dict[str, str], prefix: str, size: int) -> int | None:
    """Preis bzw. Aufschlag in Größe `size`: Grundwert plus Spalte size-1."""
    if size == 1:
        return base
    extra = cents(row.get(f"{prefix}{size - 1}", ""))
    return None if extra is None else base + extra


def extra_name(raw: str) -> str:
    """ "B.Mango_Curry" -> "Mango Curry": Präfix sortiert nur in der Kasse."""
    return _SORT_PREFIX.sub("", raw).replace("_", " ").strip()


@dataclass(frozen=True)
class _Extra:
    name: str
    groups: frozenset[str] | None  # None: bei allen Warengruppen
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
    result = Conversion()
    skip = {g.strip() for g in skip_groups}
    categories = {r["W_WRG"]: r["W_BEZEICH"] for r in warengrp.live()}
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
        elif row.get("VK2_PREIS") and vk2 != vk1:
            # Telefon ist Abholung: die Kasse nähme VK2, der Agent nennt VK1.
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
                "price_eur": _eur(price),
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

    if bad_numbers:
        result.errors.append(
            "Nummer versteht die Suche nicht (bis 999, optional Buchstabe a bis f), "
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
    return result


def _option(
    number: str, group: str, name: str, delta: int, *, default: bool, required: bool
) -> dict[str, str]:
    return {
        "number": number,
        "group_name": group,
        "option_name": name,
        "price_delta_eur": _eur(delta),
        "is_default": "ja" if default else "nein",
        "required": "ja" if required else "nein",
    }


def _extras(result: Conversion, zutaten: Table, zutgrp: Table) -> list[_Extra]:
    levels = {r["ZGRP3"] or r["ZGRP"]: r["ZPREIS"] for r in zutgrp.live()}
    extras: list[_Extra] = []
    for row in zutaten.live():
        where = f"Zutat „{row['ZBEZEICH']}“"
        name = extra_name(row["ZBEZEICH"])
        level = row["ZPREIGRP3"]
        # Leerer ZPREIS ist kein Preis 0: sonst wäre das Extra still gratis.
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
        if len(row["ZBEZEICH"]) >= EXTRA_NAME_MAX:
            result.warnings.append(
                f"{where}: Name mit {EXTRA_NAME_MAX} Zeichen, vermutlich "
                f"abgeschnitten; der Agent sagt „{name}“"
            )
        groups = (
            None
            if row["WRGSHOWALL"] == "T"
            else frozenset(g.strip() for g in row["WRGSHOW"].split() if g.strip())
        )
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
    taken: set[str] = set()
    for extra in extras:
        if extra.groups is not None and group not in extra.groups:
            continue
        if extra.name.lower() in taken:
            result.warnings.append(f"{where}: Extra „{extra.name}“ doppelt")
            continue
        prices = {_size_price(extra.base, extra.row, "ZGRPREIS", s) for s in sizes}
        if len(prices) != 1 or None in prices:
            # Unsere Option hat einen Preis je Gericht, nicht je Größe.
            result.warnings.append(
                f"{where}: Extra „{extra.name}“ kostet je Größe anders, "
                "nicht übernommen"
            )
            continue
        [price] = prices
        assert price is not None
        if price < 0:
            result.warnings.append(
                f"{where}: Extra „{extra.name}“ mit negativem Preis, nicht übernommen"
            )
            continue
        taken.add(extra.name.lower())
        result.options.append(
            _option(
                number, EXTRAS_GROUP, extra.name, price, default=False, required=False
            )
        )


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
            f"„{row['ALLERGENE']}“, Gericht ohne Allergenauskunft"
        )
        result.allergens.append(empty)
        return
    if not codes:
        words = re.split(r"[\s,_;/()-]+", row.get("ZUTATEN", "").lower())
        if any(w == "ei" or w.startswith(_CARRIERS) for w in words if w):
            carriers.append(row["ARTNR"])
        result.allergens.append(empty)
        return
    result.allergens_found.append(f"{row['ARTNR']} {raw.lower()} -> {','.join(codes)}")
    if not confirmed_by:
        result.errors.append(
            f"{where}: Allergene {','.join(codes)} nicht übernommen, wer hat sie "
            "geprüft? (--allergens-confirmed-by)"
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


def split_aliases(
    text: str, numbers: Iterable[str]
) -> tuple[str, list[dict[str, str]]]:
    """Aliase aus dem Chat nach Nummern trennen, die die Kasse liefert.

    Eine Alias-Zeile zu einer Nummer, die nicht in der neuen `menu_items.csv`
    steht (Getränk, gesperrter Artikel, Nummer, die die Suche nicht versteht),
    würde den ganzen Import blockieren. Löschen hieße, gewachsenes Wissen zu
    verlieren. Deshalb: (Text ohne diese Zeilen, die abgetrennten Zeilen).
    """
    known = {canonical_card(n) for n in numbers}
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    columns = tuple(reader.fieldnames or ("number", "alias"))
    kept: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    for row in reader:
        number = (row.get("number") or "").strip()
        (kept if canonical_card(number) in known else dropped).append(row)
    return _csv(columns, kept), dropped
