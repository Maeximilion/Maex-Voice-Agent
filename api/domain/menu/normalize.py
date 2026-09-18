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


def normalize_alias(text: str) -> str:
    """Kleinschreibung, eine Form je Zeichen (NFC), einfache Leerzeichen, kein Rand."""
    # lower() statt casefold(): casefold macht aus "Soße" "sosse".
    text = unicodedata.normalize("NFC", text).lower()
    text = _SPACE.sub(" ", text)
    return text.strip(_EDGE_PUNCT).strip()
