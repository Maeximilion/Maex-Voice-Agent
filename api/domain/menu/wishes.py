"""Wunsch zu einer Position: vom Gericht trennen und einordnen (T-4.10, D8).

"die 23 ohne Karotten" fand vorher nichts - der Wunsch lief mit in die Suche.
Hier wird er abgetrennt und eingeordnet, bevor der Agent etwas zusagt:

- `note`: Weglassen ("ohne Karotten", "keine Zwiebeln"). Ein Hinweis fuer die
  Kueche, ohne Preis.
- `option`: was als Option des Gerichts auf der Karte steht ("mit Nudeln statt
  Reis"). Gruppe, Name, Aufpreis und Begruendung kommen aus `item_options`,
  nie vom Modell (CLAUDE.md §2 Regel 1).
- `allergy`: eine eigene Allergie ist kein Wunsch. Sie geht als Hinweis im
  festen Wortlaut an die Kueche (E14), ohne Zusage, dass das Gericht frei davon
  ist (CLAUDE.md §9). Eine Frage nach den Allergenen eines Gerichts ist keine
  eigene Allergie, sondern der Allergenpfad (`get_item_details`).
- `unknown`: alles andere. Was die Karte nicht kennt, bietet der Agent am
  Telefon nicht an (Entscheidung D8, 24.09.2026).
- `open`: noch nicht entscheidbar - bei mehreren Treffern fuer das Gericht oder
  wenn die Option in mehreren Gruppen steht (`groups`); der Agent fragt.

Weglassen zusammen mit einer Zugabe ("ohne Zwiebeln, dafür mit Nudeln") wird
nie zur freien Notiz: die Zugabe ist Option oder unbekannt, das Weglassen kommt
als `note` mit. Sonst bekaeme die Kueche eine Zugabe ohne Preis (Review PR #139).

Reine Funktionen ohne Datenbank.
"""

import re

from api.domain.menu.numberwords import fold, parse_cardinal
from api.schemas.menu import OptionGroup, Wish

# Nach fold() (ä -> ae).
_REMOVE = frozenset({"ohne", "kein", "keine", "keinen", "keinem"})
_ADD = frozenset({"mit", "extra"})
_INSTEAD = frozenset({"statt", "anstatt"})
_LEAD_FILLER = frozenset({"bitte", "aber", "und", "dann", "dafuer"})
# Verbindet Teile eines Wunsches, ohne selbst etwas zu wuenschen.
_GLUE = frozenset({"gern", "gerne", "dazu", "noch", "als", "die", "der", "das", "den"})
_ARTICLES = frozenset({"ein", "eine", "einen", "einem", "einer"})
# Womit ein Satzteil zur eigenen Allergie beginnt, auch ohne Komma davor.
_CLAUSE_OPENERS = frozenset({"ich", "wir", "mein", "meine", "meinem", "meiner"})

_WORD = re.compile(r"[^\W_]+")
_TOKEN = re.compile(r"[^\W_]+|,")
_TRAILING_PLEASE = re.compile(r"[\s,]*bitte[\s.!?]*$", re.IGNORECASE)
# Eine Menge gehoert zur Position, nie in den Hinweis ("zweimal", "2 x").
_TIMES = re.compile(r"[\s,]*\b(\w+?)mal\b", re.IGNORECASE)
_COUNTED = re.compile(r"[\s,]*\b\d+\s*(?:x|portionen?|stück|stueck)\b", re.IGNORECASE)


# Fester Wortlaut des Kuechenhinweises (E14, Maxi 24.09.2026): wird beim
# Vorlesen wiederholt, nie mit der Zusage, das Gericht sei frei davon.
ALLERGY_NOTE = "WICHTIG: Keine {ingredient}. Grund: Allergie"
# Die Zutaten reichen bis zum naechsten Satzteil: "gegen Erdnuesse und Sesam" sind
# zwei (Codex PR #139, P1), "gegen Sesam und dann noch eine Cola" ist eine.
_REST = r"(.+)$"
_INGREDIENT = (
    # "ich bin gegen Nuesse allergisch"
    re.compile(r"gegen\s+([^.;!?]+?)\s+allergisch", re.IGNORECASE),
    # "allergisch gegen Sesam", "Allergie gegen Sellerie"
    re.compile(
        r"(?:allergisch|allergie|unvertr(?:ä|ae)glichkeit)\s+(?:gegen|auf)\s+" + _REST,
        re.IGNORECASE,
    ),
    # "ich vertrage keine Erdnuesse"
    re.compile(r"vertr(?:a|ä|ae)g\w*\s+(?:keine[nm]?|kein)\s+" + _REST, re.IGNORECASE),
    # "Erdnussallergie"
    re.compile(r"(\w+?)allergie", re.IGNORECASE),
)
# Nach einem "und" oder Komma beginnt hier ein neuer Satzteil, keine Zutat mehr.
_CLAUSE_WORDS = (
    frozenset(
        {"dann", "noch", "ich", "wir", "bitte", "aber", "dazu", "auch", "ausserdem"}
    )
    | _ARTICLES
    | frozenset({"die", "der", "das", "den", "einmal"})
)
_PIECE = re.compile(r"[^\W_]+|[,.;!?]")
# Unsicherheit oder Ablehnung ist keine Zutat: "weiss ich nicht", "nein", "keine
# Ahnung" - dann bleibt die Frage offen (Codex PR #139, P1).
_NO_INGREDIENT = frozenset(
    {"nein", "ja", "nicht", "nichts", "weiss", "ahnung", "egal", "vielleicht"}
    | {"unsicher", "sicher", "genau", "irgendwas", "irgendwelche"}
)


def _words(text: str) -> list[str]:
    return [fold(w) for w in _WORD.findall(text)]


def _is_allergy(word: str) -> bool:
    """Eine eigene Allergie. "Allergene" ist die Frage nach dem Gericht und
    gehoert in den Allergenpfad, nicht hierher (Review PR #139)."""
    if word.startswith("allergen"):
        return False
    return (
        "allerg" in word
        or "unvertraeglich" in word
        or word.startswith(("vertrag", "vertraeg"))
    )


def _ingredient(text: str) -> str | None:
    """Die Zutat, wie der Gast sie sagt: "Erdnussallergie" -> "Erdnuss",
    "allergisch gegen Sesam" -> "Sesam". None: nicht erkennbar, nachfragen."""
    for pattern in _INGREDIENT:
        match = pattern.search(text)
        if match:
            found = _until_new_clause(match.group(1))
            if not found or _NO_INGREDIENT & set(_words(found)):
                return None
            return found[0].upper() + found[1:]
    return None


def _until_new_clause(rest: str) -> str:
    """Die Zutaten bis zum naechsten Satzteil, mit "und" und Komma dazwischen."""
    pieces = _PIECE.findall(rest)
    kept: list[str] = []
    for i, piece in enumerate(pieces):
        word = fold(piece)
        if piece in ".;!?" or word == "bitte":
            break
        if piece == "," or word in ("und", "sowie", "oder"):
            nxt = fold(pieces[i + 1]) if i + 1 < len(pieces) else None
            if (
                nxt is None
                or nxt in ",.;!?"
                or nxt in _CLAUSE_WORDS
                or nxt.endswith("mal")
                or parse_cardinal(nxt) is not None
            ):
                break
        kept.append(piece)
    text = ""
    for piece in kept:
        text += piece if piece == "," else f" {piece}"
    return text.strip()


def _adds(words: list[str], i: int) -> bool:
    """Beginnt hier eine Zugabe? "extra" direkt nach "ohne" gehoert zum
    Weglassen: "ohne extra Kaese" ist keine Zugabe (Codex PR #139)."""
    if words[i] not in _ADD:
        return False
    return not (words[i] == "extra" and i > 0 and words[i - 1] in _REMOVE)


def _starts(words: list[str]) -> list[int]:
    """Wo ein Wunsch beginnen kann, in Reihenfolge: an "ohne", "kein", "mit",
    "extra"; bei "statt" ein Wort davor; bei einer Allergie am Anfang ihres
    Satzteils - am Komma oder, ohne Komma, bei "ich", "wir", "mein" (Codex PR
    #139). Nach einer Allergie beginnt kein weiterer Wunsch."""
    starts: list[int] = []
    for i, word in enumerate(words):
        if word in _REMOVE or _adds(words, i):
            starts.append(i)
        elif word in _INSTEAD and i > 0 and words[i - 1] != ",":
            starts.append(i - 1)
        elif _is_allergy(word):
            comma = max((j for j in range(i) if words[j] == ","), default=-1)
            opener = next(
                (j for j in range(comma + 1, i) if words[j] in _CLAUSE_OPENERS), None
            )
            starts.append(
                opener if opener is not None else comma + 1 if comma >= 0 else i
            )
            break
    return sorted(set(starts))


def wish_candidates(text: str) -> list[tuple[str, str, str]]:
    """Jede moegliche Trennung in Gericht, Wunsch und dessen ersten Satzteil, von
    vorn. Die Suche nimmt die erste, deren Satzteil nicht zum Namen gehoert:
    "Sommerrollen mit Garnelen ohne Koriander" - "mit Garnelen" ist Name,
    "ohne Koriander" der Wunsch (Codex PR #139). Eine Menge im Wunsch bleibt
    nicht darin ("ohne Zwiebeln, zweimal"), sie gehoert zur Position."""
    tokens = list(_TOKEN.finditer(text))
    words = [fold(t.group()) for t in tokens]
    starts = [i for i in _starts(words) if i > 0]
    candidates = []
    for n, start in enumerate(starts):
        at = tokens[start].start()
        dish = text[:at].strip(" ,.;")
        wish = _clean(text[at:])
        end = tokens[starts[n + 1]].start() if n + 1 < len(starts) else len(text)
        segment = _clean(text[at:end])
        if dish and wish:
            candidates.append((dish, wish, segment))
    return candidates


def _clean(text: str) -> str:
    return _TRAILING_PLEASE.sub("", _drop_quantity(text)).strip(" ,.;!?")


def _drop_quantity(text: str) -> str:
    """Nur echte Mengen: "zweimal", "einmal", "2 x" - nicht "normal"."""
    text = _COUNTED.sub("", text)
    return _TIMES.sub(
        lambda m: (
            ""
            if fold(m.group(1)) == "ein" or parse_cardinal(fold(m.group(1))) is not None
            else m.group(0)
        ),
        text,
    )


def classify_wish(text: str, groups: list[OptionGroup]) -> Wish:
    """Der Wunsch zu einem feststehenden Gericht, gegen dessen Optionen."""
    text = _clean(text)
    words = _words(text)
    if any(_is_allergy(w) for w in words):
        ingredient = _ingredient(text)
        if ingredient is None:
            return Wish(text=text, kind="allergy")
        return Wish(
            text=ALLERGY_NOTE.format(ingredient=ingredient),
            kind="allergy",
            ingredient=ingredient,
        )
    lead = next((w for w in words if w not in _LEAD_FILLER), None)
    if lead in _REMOVE:
        removal, addition = _split_addition(text)
        if addition is None:
            return Wish(text=text, kind="note")
        return classify_wish(addition, groups).model_copy(update={"note": removal})
    addition, removal = _split_removal(text)
    if removal is not None:
        # "mit Nudeln ohne Zwiebeln": die Option und der Hinweis, wie umgekehrt
        # (Codex PR #139). Nur eine sichere Option nimmt den Hinweis mit.
        wish = classify_wish(addition, groups)
        if wish.kind != "option" or wish.note:
            return Wish(text=text, kind="unknown")
        return wish.model_copy(update={"text": text, "note": removal})
    # Bei "Nudeln statt Reis" gilt, was vor "statt" steht.
    cut = next((i for i, w in enumerate(words) if w in _INSTEAD), len(words))
    wanted = set(words[:cut])
    matches = [
        (group, option, used)
        for group in groups
        for option in group.options
        if (used := _names(option.name, group.group, wanted))
    ]
    if not matches:
        return Wish(text=text, kind="unknown")
    if len({option.name for _, option, _ in matches}) == 1 and len(matches) > 1:
        # Dieselbe Option in zwei Gruppen (Reis als Beilage und als Extra): nie
        # selbst waehlen, nachfragen (Review PR #139).
        return Wish(
            text=text,
            kind="open",
            option=matches[0][1].name,
            groups=[group.group for group, _, _ in matches],
        )
    if len(matches) != 1:
        return Wish(text=text, kind="unknown")
    group, option, used = matches[0]
    # Was die Option nicht erklaert, darf nicht still wegfallen: "mit Nudeln und
    # Pommes" ist kein "mit Nudeln" (Codex PR #139).
    if wanted - used - _ADD - _LEAD_FILLER - _GLUE:
        return Wish(text=text, kind="unknown")
    return Wish(
        text=text,
        kind="option",
        group=group.group,
        option=option.name,
        price_delta_cents=option.price_delta_cents,
        reason=option.reason,
    )


def _split_addition(text: str) -> tuple[str, str | None]:
    """ "ohne Zwiebeln, dafür mit Nudeln" -> ("ohne Zwiebeln", "mit Nudeln")."""
    tokens = list(_TOKEN.finditer(text))
    words = [fold(t.group()) for t in tokens]
    at = next(
        (
            i
            for i, w in enumerate(words)
            if i > 0 and (_adds(words, i) or w in _INSTEAD)
        ),
        None,
    )
    if at is None:
        return text, None
    if words[at] in _INSTEAD:
        at -= 1
    removal = text[: tokens[at].start()]
    removal = re.sub(r"[\s,]*(?:dafür|dafuer|aber|und)[\s,]*$", "", removal)
    return removal.strip(" ,.;"), text[tokens[at].start() :].strip(" ,.;")


def _split_removal(text: str) -> tuple[str, str | None]:
    """ "mit Nudeln, aber ohne Zwiebeln" -> ("mit Nudeln", "ohne Zwiebeln")."""
    tokens = list(_TOKEN.finditer(text))
    words = [fold(t.group()) for t in tokens]
    at = next((i for i, w in enumerate(words) if i > 0 and w in _REMOVE), None)
    if at is None:
        return text, None
    addition = text[: tokens[at].start()]
    addition = re.sub(r"[\s,]*(?:dafür|dafuer|aber|und)[\s,]*$", "", addition)
    return addition.strip(" ,.;"), text[tokens[at].start() :].strip(" ,.;")


def _names(option: str, group: str, wanted: set[str]) -> set[str]:
    """Die Woerter des Wunsches, die die Option nennen - leer, wenn sie es nicht
    tun. Als eigenes Wort ("mit Erdnuss") oder zusammengesetzt mit dem
    Gruppennamen ("Erdnusssauce" = Erdnuss + Sauce). Kein beliebiger
    Wortanfang: "Reisnudeln" ist nicht die Option Reis."""
    words = _words(option)
    if set(words) <= wanted:
        return set(words)
    compound = words[0] + "".join(_words(group)) if len(words) == 1 else None
    return {compound} if compound in wanted else set()


def open_wish(text: str) -> Wish:
    """Bei mehreren Treffern: Weglassen und Allergie stehen schon fest, eine
    Option erst, wenn der Gast das Gericht gewaehlt hat."""
    wish = classify_wish(text, [])
    if wish.kind in ("note", "allergy"):
        return wish
    return Wish(text=_clean(text), kind="open")


def has_number(wish: str) -> bool:
    """Steht eine Zahl im Wunsch ("mit 2 Soßen")? Dann gilt Regel A: eine zweite
    Zahl neben der Nummer wird nachgefragt, nie als Wunsch abgetrennt. Artikel
    ("ohne eine Zwiebel") zaehlen nicht."""
    return any(
        w.isdigit() or (w not in _ARTICLES and parse_cardinal(w) is not None)
        for w in _words(wish)
    )


def names_it(name: str, wish: str) -> bool:
    """Gehoert der "Wunsch" zum Namen des Gerichts? "mit Garnelen" in
    "Sommerrollen mit Garnelen" ist kein Wunsch, sondern der Name - auch mit
    Satzzeichen im Namen ("Sommerrollen (mit Garnelen)", Review PR #139)."""
    words = _words(wish)
    # "ohne Garnelen" gehoert nie zum Namen, auch wenn die Garnelen darin stehen:
    # es ist genau der Hinweis fuer die Kueche (Codex PR #139).
    lead = next((w for w in words if w not in _LEAD_FILLER), None)
    if lead in _REMOVE:
        return False
    # Der Satzteil steht so im Namen, samt "mit": "extra Garnelen" nennt die
    # Garnelen aus "Sommerrollen mit Garnelen", ist aber ein Wunsch (Codex PR
    # #139).
    said = [w for w in words if w not in _LEAD_FILLER]
    named = _words(name)
    return bool(said) and any(
        named[i : i + len(said)] == said for i in range(len(named) - len(said) + 1)
    )
