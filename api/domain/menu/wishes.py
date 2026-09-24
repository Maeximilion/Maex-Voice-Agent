"""Wunsch zu einer Position: vom Gericht trennen und einordnen (T-4.10, D8).

"die 23 ohne Karotten" fand vorher nichts - der Wunsch lief mit in die Suche.
Hier wird er abgetrennt und eingeordnet, bevor der Agent etwas zusagt:

- `note`: Weglassen ("ohne Karotten", "keine Zwiebeln"). Ein Hinweis fuer die
  Kueche, ohne Preis.
- `option`: was als Option des Gerichts auf der Karte steht ("mit Nudeln statt
  Reis"). Gruppe, Name, Aufpreis und Begruendung kommen aus `item_options`,
  nie vom Modell (CLAUDE.md §2 Regel 1).
- `allergy`: eine Allergie ist kein Wunsch. Sie geht als Hinweis an die Kueche,
  ohne Zusage, dass das Gericht frei davon ist (CLAUDE.md §9).
- `unknown`: alles andere. Was die Karte nicht kennt, bietet der Agent am
  Telefon nicht an (Entscheidung D8, 24.09.2026).
- `open`: bei mehreren Treffern noch nicht entscheidbar; eingeordnet wird, wenn
  das Gericht feststeht.

Reine Funktionen ohne Datenbank.
"""

import re

from api.domain.menu.numberwords import fold, parse_cardinal
from api.schemas.menu import OptionGroup, Wish

# Nach fold() (ä -> ae).
_REMOVE = frozenset({"ohne", "kein", "keine", "keinen", "keinem"})
_ADD = frozenset({"mit", "extra"})
_INSTEAD = frozenset({"statt", "anstatt"})
_LEAD_FILLER = frozenset({"bitte", "aber", "und", "dann"})
_TOKEN = re.compile(r"[^\W_]+|,", re.UNICODE)
_TRAILING_PLEASE = re.compile(r"[\s,]*bitte[\s.!?]*$", re.IGNORECASE)


def _is_allergy(word: str) -> bool:
    return "allerg" in word or "unvertraeglich" in word


def split_wish(text: str) -> tuple[str, str | None]:
    """Gericht und Wunsch, in der gesprochenen Reihenfolge. Ohne Wunsch: (text, None).

    Der Wunsch beginnt am ersten Merkmal: "ohne", "kein", "mit", "extra"; bei
    "statt" ein Wort davor ("Nudeln statt Reis"); bei einer Allergie am Anfang
    ihres Satzteils ("ich habe eine Erdnussallergie"). Ob "mit Garnelen" doch
    zum Namen gehoert ("Sommerrollen mit Garnelen"), entscheidet die Suche.
    """
    tokens = list(_TOKEN.finditer(text))
    words = [fold(t.group()) for t in tokens]
    start = None
    for i, word in enumerate(words):
        if word in _REMOVE or word in _ADD:
            start = i
        elif word in _INSTEAD and i > 0 and words[i - 1] != ",":
            start = i - 1
        elif _is_allergy(word):
            comma = max((j for j in range(i) if words[j] == ","), default=None)
            start = comma + 1 if comma is not None else i
        if start is not None:
            break
    if start is None:
        return text, None
    dish = text[: tokens[start].start()].strip(" ,.;")
    if not dish:
        return text, None
    wish = _TRAILING_PLEASE.sub("", text[tokens[start].start() :]).strip(" ,.;!?")
    return dish, wish or None


def classify_wish(text: str, groups: list[OptionGroup]) -> Wish:
    """Der Wunsch zu einem feststehenden Gericht, gegen dessen Optionen."""
    words = [fold(w) for w in re.findall(r"[^\W_]+", text)]
    if any(_is_allergy(w) for w in words):
        return Wish(text=text, kind="allergy")
    lead = next((w for w in words if w not in _LEAD_FILLER), None)
    if lead in _REMOVE:
        return Wish(text=text, kind="note")
    # Bei "Nudeln statt Reis" gilt, was vor "statt" steht.
    cut = next((i for i, w in enumerate(words) if w in _INSTEAD), len(words))
    wanted = set(words[:cut])
    matches = [
        (group, option)
        for group in groups
        for option in group.options
        if _names(option.name, group.group, wanted)
    ]
    if len(matches) != 1:
        return Wish(text=text, kind="unknown")
    group, option = matches[0]
    return Wish(
        text=text,
        kind="option",
        group=group.group,
        option=option.name,
        price_delta_cents=option.price_delta_cents,
        reason=option.reason,
    )


def _names(option: str, group: str, wanted: set[str]) -> bool:
    """Nennt der Wunsch die Option? Als eigenes Wort ("mit Erdnuss") oder
    zusammengesetzt mit dem Gruppennamen ("Erdnusssauce" = Erdnuss + Sauce).
    Kein beliebiger Wortanfang: "Reisnudeln" ist nicht die Option Reis."""
    words = fold(option).split()
    if set(words) <= wanted:
        return True
    return len(words) == 1 and words[0] + fold(group).replace(" ", "") in wanted


def open_wish(text: str) -> Wish:
    """Bei mehreren Treffern: Weglassen und Allergie stehen schon fest, eine
    Option erst, wenn der Gast das Gericht gewaehlt hat."""
    wish = classify_wish(text, [])
    return wish if wish.kind in ("note", "allergy") else Wish(text=text, kind="open")


_ARTICLES = frozenset({"ein", "eine", "einen", "einem", "einer"})


def has_number(wish: str) -> bool:
    """Steht eine Zahl im Wunsch ("mit 2 Soßen")? Dann gilt Regel A: eine zweite
    Zahl neben der Nummer wird nachgefragt, nie als Wunsch abgetrennt. Artikel
    ("ohne eine Zwiebel") zaehlen nicht."""
    return any(
        w.isdigit() or (w not in _ARTICLES and parse_cardinal(w) is not None)
        for w in (fold(x) for x in re.findall(r"[^\W_]+", wish))
    )


def names_it(name: str, wish: str) -> bool:
    """Gehoert der "Wunsch" zum Namen des Gerichts? "mit Garnelen" in
    "Sommerrollen mit Garnelen" ist kein Wunsch, sondern der Name."""
    in_name = set(fold(name).split())
    content = {
        w
        for w in (fold(x) for x in re.findall(r"[^\W_]+", wish))
        if w not in _ADD and w not in _REMOVE and w not in _LEAD_FILLER
    }
    return bool(content) and content <= in_name
