"""Eingaben absichtlich verrauschen (docs/11 §sim), um die Verstaendnis-Leiter zu testen.

Ziel ist nicht Realismus, sondern Wiederholbarkeit: dieselbe Saat ergibt denselben
verrauschten Satz. Ohne das waere ein Fehlschlag der Leiter (docs/05 §2) nicht
reproduzierbar und damit kein Testfall, sondern eine Anekdote.
"""

import random

# Kurze Woerter bleiben unberuehrt: aus "ja" wird durch einen Dreher kein
# missverstandenes Wort, sondern Unsinn, den kein Mensch so ausspricht.
MIN_WORD_LENGTH = 4


def swap(word: str, rng: random.Random) -> str:
    """Buchstabendreher: der haeufigste Fehler der Spracherkennung bei Namen."""
    i = rng.randrange(len(word) - 1)
    return word[:i] + word[i + 1] + word[i] + word[i + 2 :]


def truncate(word: str, rng: random.Random) -> str:
    """Abgeschnittenes Wort: passiert, wenn die Erkennung zu frueh abbricht."""
    return word[: rng.randrange(2, len(word))]


def drop(word: str, rng: random.Random) -> str:
    """Ganz verschlucktes Wort."""
    return ""


OPERATIONS = (swap, truncate, drop)


def noisy_text(text: str, level: float, rng: random.Random | None = None) -> str:
    """Verrauscht im Mittel `level` der langen Woerter (0.0 bis 1.0).

    Gibt nie eine leere Zeichenkette zurueck: ein Zug ohne jeden Inhalt waere kein
    verrauschter Kundenzug mehr, sondern gar keiner.
    """
    if level <= 0:
        return text
    rng = rng or random.Random()
    level = min(level, 1.0)
    words = [
        rng.choice(OPERATIONS)(word, rng)
        if len(word) >= MIN_WORD_LENGTH and rng.random() < level
        else word
        for word in text.split()
    ]
    return " ".join(word for word in words if word) or text
