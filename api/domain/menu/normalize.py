"""Kundensprache vergleichbar machen (docs/11 §domain/menu).

Ein Alias wird so gespeichert, wie ihn die Suche später auch aus dem Gesprochenen
bildet. Beide Seiten müssen dieselbe Funktion benutzen, sonst trifft "Die
knusprigen Rollen!" nie "die knusprigen rollen".

Umlaute bleiben: die Trigram-Suche vergleicht Zeichenfolgen, und "Frühling"
gegen "Fruehling" ist dort ein Fehlertreffer, kein Treffer. Füllwörter
("einmal ... bitte") entfernt erst die Suche (T-4.3), nicht der Import - ein
Alias aus der Karte ist schon die gewünschte Kurzform.
"""

import re
import unicodedata

_SPACE = re.compile(r"\s+")
# Satzzeichen am Rand tragen am Telefon nichts; im Wort ("Wan-Tan") bleiben sie.
# Dazu die typografischen Anfuehrungszeichen, als Escape geschrieben, damit sie
# im Quelltext nicht mit Komma oder Apostroph zu verwechseln sind.
_EDGE_PUNCT = " \t\"'.,;:!?()[]{}\u201e\u201c\u201d\u201a\u2018\u2019\u00ab\u00bb"


# Was am Telefon um den Gerichtnamen herum gesagt wird und nichts über das
# Gericht sagt. Bewusst kurz: jedes Wort hier kann nie Teil eines Treffers sein.
FILLER = frozenset(
    {
        "ich", "wir", "hätte", "hätten", "möchte", "möchten", "nehme", "nehmen",
        "gern", "gerne", "bitte", "dann", "noch", "und", "also", "ja", "äh", "ähm",
        "hm", "mal", "einmal", "die", "der", "das", "den", "dem", "des", "ein",
        "eine", "einen", "einem", "einer", "nummer", "nr", "x", "portion",
        "portionen",
    }
)  # fmt: skip


def _is_number_word(token: str) -> bool:
    # Import hier, weil numberwords normalize nicht kennt und nicht kennen soll.
    from api.domain.menu.numberwords import parse_cardinal

    if token.isdigit() or parse_cardinal(token) is not None:
        return True
    # "zweimal", "dreimal": Menge, kein Teil des Gerichtnamens.
    return token.endswith("mal") and parse_cardinal(token[:-3]) is not None


def normalize_query(text: str) -> str:
    """Das Gesprochene auf den Gerichtnamen verkürzen: wie ein Alias, ohne
    Füllwörter und ohne Zahl- und Mengenwörter.

    "Ich hätte gern zweimal die knusprige Ente, bitte" -> "knusprige ente".
    Die Zahl selbst wertet search_menu vorher aus (Nummer oder Menge).
    """
    tokens = normalize_alias(text).split(" ")
    kept = [
        t.strip(_EDGE_PUNCT)
        for t in tokens
        if t.strip(_EDGE_PUNCT) not in FILLER
        and not _is_number_word(t.strip(_EDGE_PUNCT))
    ]
    return " ".join(t for t in kept if t)


def normalize_alias(text: str) -> str:
    """Kleinschreibung, eine Form je Zeichen (NFC), einfache Leerzeichen, kein Rand."""
    # lower() statt casefold(): casefold macht aus "Soße" "sosse".
    text = unicodedata.normalize("NFC", text).lower()
    text = _SPACE.sub(" ", text)
    return text.strip(_EDGE_PUNCT).strip()
