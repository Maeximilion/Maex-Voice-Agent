"""sim/noise.py: absichtlich verrauschte Eingaben, aber reproduzierbar."""

import random

from sim.noise import MIN_WORD_LENGTH, noisy_text

SATZ = "Guten Tag ich haette gern einen Tisch fuer vier Personen"


def test_ohne_rauschen_bleibt_der_satz_gleich():
    assert noisy_text(SATZ, 0.0) == SATZ


def test_gleiche_saat_ergibt_denselben_satz():
    erst = noisy_text(SATZ, 0.5, random.Random(7))
    zweit = noisy_text(SATZ, 0.5, random.Random(7))

    assert erst == zweit
    assert erst != SATZ


def test_kurze_woerter_bleiben_unberuehrt():
    """Aus "ja" darf kein Unsinn werden: ein Dreher in zwei Buchstaben ist kein
    realistischer Erkennungsfehler, sondern ein anderes Wort."""
    kurz = " ".join(w for w in SATZ.split() if len(w) < MIN_WORD_LENGTH)

    assert noisy_text(kurz, 1.0, random.Random(1)) == kurz


def test_voll_verrauschter_satz_bleibt_ein_satz():
    """Ein Zug ohne jeden Inhalt waere kein verrauschter Kundenzug mehr, sondern
    gar keiner - dann lieber der Originalsatz."""
    assert noisy_text("Tisch", 1.0, random.Random(0)).strip()
    assert noisy_text(SATZ, 1.0, random.Random(3)).strip()
