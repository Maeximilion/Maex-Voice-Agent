"""Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell (docs/11 §agent).

Bei diesen Signalen steht die Reaktion schon fest (sofortige Übergabe an das
Team): ein Modell-Aufruf würde nur Tokens kosten, um zum selben Ergebnis zu
kommen (CLAUDE.md §2 Regel 6). Reine Stichwort-Erkennung auf dem rohen
Kundentext, kein Sprachverständnis — eindeutige Signalwörter, keine
Interpretation. Feinere Fälle (Allergie ohne DB-Wert, große Bestellung, …)
kommen erst mit Stufe 2/3, wenn es Bestellungen überhaupt gibt.
"""

from typing import Literal

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


def check(text: str) -> EscalationReason | None:
    """Erster treffende Auslöser aus docs/05 §4, oder `None`. Wird auf jedem
    rohen Kundenzug geprüft, bevor `loop.py` das Modell überhaupt aufruft."""
    lowered = text.lower()
    for reason, keywords in _TRIGGERS:
        if any(keyword in lowered for keyword in keywords):
            return reason
    return None
