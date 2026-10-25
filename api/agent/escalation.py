"""Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell (docs/11 §agent).

Bei diesen Signalen steht die Reaktion schon fest (sofortige Übergabe an das
Team): ein Modell-Aufruf würde nur Tokens kosten, um zum selben Ergebnis zu
kommen (CLAUDE.md §2 Regel 6). Reine Stichwort-Erkennung auf dem rohen
Kundentext, kein Sprachverständnis — eindeutige Signalwörter, keine
Interpretation. Feinere Fälle (Allergie ohne DB-Wert, große Bestellung, …)
kommen erst mit Stufe 2/3, wenn es Bestellungen überhaupt gibt.
"""

import re
from typing import Literal

from api.domain.menu.wishes import mentions_allergy

EscalationReason = Literal["complaint", "human_requested", "cancellation"]

# Reihenfolge ist die Prüfreihenfolge: "storniert die Reklamation" träfe sowohl
# complaint- als auch cancellation-Wörter, complaint zuerst geprüft ist die
# sicherere Lesart (docs/05 §4 nennt Beschwerde vor Storno).
_TRIGGERS: tuple[tuple[EscalationReason, tuple[str, ...]], ...] = (
    (
        "complaint",
        (
            "beschwerde",
            "beschweren",
            "reklamation",
            "reklamieren",
            "ärger",
            "ärgerlich",
            "geärgert",
            "unzufrieden",
            "schlechter service",
        ),
    ),
    (
        "human_requested",
        (
            "mit einem menschen",
            "menschen sprechen",
            "jemanden sprechen",
            "mitarbeiter sprechen",
            "kollegen sprechen",
            "eine person sprechen",
        ),
    ),
    (
        "cancellation",
        (
            # "storn" statt einzelner Formen: deckt "Storno", "stornieren",
            # "Stornierung", "storniert" in einem Wort ab, sie teilen sich nur
            # die ersten fünf Buchstaben, nicht mehr (Codex-Review PR #102, P2:
            # "Storno meiner Reservierung" traf bisher kein Stichwort).
            "storn",
            "rückgängig machen",
            "absagen",
            "abbestellen",
        ),
    ),
)


# A question about allergens beyond the maintained values (legal check L17,
# D24): traces and cross-contact are never in the database, so these words go
# to the team in any call. A phone order is distance selling, and the answer
# has to come before the contract, so before `confirm`.
# ponytail: closed word lists. Add the word when the call log shows it.
_BEYOND = re.compile(
    r"\bspuren|kontamin|verunreinig|kreuzkontakt|\bin kontakt|\bkontakt mit"
    r"|ber(?:ü|ue)hrung|fritteuse|frittier|\b(?:selben|gleichen) (?:öl|oel|pfanne)"
)
# Certainty asked for: only in a call that speaks of allergies ("Ist das
# wirklich sicher?" after the allergens of a dish), not "Ist das sicher bis
# sieben fertig?" in a call about a table.
_CERTAIN = re.compile(
    r"\b(?:wirklich|ganz|absolut) sicher|hundertprozentig"
    r"|\b100 ?(?:%|prozent)|garantier|versprechen"
)


def speaks_of_allergies(text: str) -> bool:
    """The guest's own allergy ("Erdnussallergie", "ich vertrage keine
    Erdnüsse") or the allergens of a dish ("Allergene")."""
    return mentions_allergy(text) or "allergen" in text.lower()


def asks_beyond_the_allergens(text: str, allergy_talk: bool) -> bool:
    """Does the guest ask about allergens what no database value answers?
    `allergy_talk`: the call has spoken of allergies before this sentence."""
    lowered = text.lower()
    if _BEYOND.search(lowered):
        return True
    return (allergy_talk or speaks_of_allergies(text)) and bool(
        _CERTAIN.search(lowered)
    )


def check(text: str) -> EscalationReason | None:
    """Erster treffende Auslöser aus docs/05 §4, oder `None`. Wird auf jedem
    rohen Kundenzug geprüft, bevor `loop.py` das Modell überhaupt aufruft."""
    lowered = text.lower()
    for reason, keywords in _TRIGGERS:
        if any(keyword in lowered for keyword in keywords):
            return reason
    return None
