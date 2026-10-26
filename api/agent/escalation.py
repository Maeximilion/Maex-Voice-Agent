"""Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell (docs/11 §agent).

Bei diesen Signalen steht die Reaktion schon fest (sofortige Übergabe an das
Team): ein Modell-Aufruf würde nur Tokens kosten, um zum selben Ergebnis zu
kommen (CLAUDE.md §2 Regel 6). Reine Stichwort-Erkennung auf dem rohen
Kundentext, kein Sprachverständnis — eindeutige Signalwörter, keine
Interpretation. Feinere Fälle (Allergie ohne DB-Wert, große Bestellung, …)
kommen erst mit Stufe 2/3, wenn es Bestellungen überhaupt gibt.

Also checked here: the question whether a person is on the line
(`asks_if_human`), whose answer is as fixed as a handoff (docs/09 L1).
"""

import re
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


# The question whether a person is on the line ("Sind Sie ein Mensch?"). The
# AI Act asks for a truthful answer every time (docs/09 L1), so the core gives
# it before the model and never depends on the model's wording (agent/loop.py).
# Question order only: the verb before its subject, then at most filler words,
# then what is asked about. "Ich will mit einem Menschen sprechen" is the wish
# of `check`, not this question.
_ASKING = frozenset(
    {
        ("sind", "sie"),
        ("bist", "du"),
        ("ist", "das"),
        ("spreche", "ich"),
        ("rede", "ich"),
        ("bin", "ich"),
        ("telefoniere", "ich"),
    }
)
_BETWEEN = frozenset(
    {"hier", "da", "gerade", "jetzt", "denn", "eigentlich", "wirklich", "mit"}
    | {"ein", "eine", "einem", "einer", "echt", "echte", "echter", "echten"}
    | {"richtig", "richtige", "richtiger", "richtigen"}
    | {"künstliche", "kuenstliche", "künstlichen", "kuenstlichen"}
)
_PERSON_OR_MACHINE = frozenset(
    {"mensch", "menschen", "person", "roboter", "bot", "chatbot", "maschine"}
    | {"computer", "sprachcomputer", "ki", "intelligenz", "automat", "assistent"}
)
_WORD = re.compile(r"[a-zäöüß]+")


def asks_if_human(text: str) -> bool:
    """Whether the guest asks if a person or a machine is on the line."""
    words = _WORD.findall(text.lower())
    for at in range(len(words) - 1):
        if (words[at], words[at + 1]) not in _ASKING:
            continue
        rest = at + 2
        while rest < len(words) and words[rest] in _BETWEEN:
            rest += 1
        if rest < len(words) and words[rest] in _PERSON_OR_MACHINE:
            return True
    return False
