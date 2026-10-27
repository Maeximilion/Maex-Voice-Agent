"""Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell (docs/11 §agent).

Bei diesen Signalen steht die Reaktion schon fest (sofortige Übergabe an das
Team): ein Modell-Aufruf würde nur Tokens kosten, um zum selben Ergebnis zu
kommen (CLAUDE.md §2 Regel 6). Reine Stichwort-Erkennung auf dem rohen
Kundentext, kein Sprachverständnis — eindeutige Signalwörter, keine
Interpretation. Feinere Fälle (Allergie ohne DB-Wert, große Bestellung, …)
kommen erst mit Stufe 2/3, wenn es Bestellungen überhaupt gibt.

Also checked here: the question whether a person is on the line
(`without_human_question`), whose answer is as fixed as a handoff (docs/09 L1).
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
# Frying only as shared or separate frying: "Wan-Tan frittiert" is a dish on
# the menu, and a guest who orders it asks nothing (Codex PR #369).
_BEYOND = re.compile(
    r"\bspuren|kontamin|verunreinig|kreuzkontakt"
    r"|\b(?:selben|gleichen|derselben|eigenen?|separaten?|getrennten?)"
    r" (?:öl|oel|pfanne|fritteuse)"
    r"|\bzusammen\b[^.?!]{0,40}\bfrittier"
)
# Contact or touch of food ("Kommt das mit Sesam in Kontakt?"), never contact
# with a person: "damit Sie mit mir in Kontakt bleiben" gives a number, it
# asks nothing about allergens (Codex PR #369).
_CONTACT = re.compile(r"\bin kontakt|\bkontakt mit|ber(?:ü|ue)hr")
_WITH_A_PERSON = re.compile(
    r"\bmit (?:mir|uns|ihnen|dir|euch|jemandem|dem team)\b"
    r"|\bkontakt (?:bleiben|aufnehmen|halten)|\bin kontakt (?:bleiben|treten|setzen)"
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
    if _CONTACT.search(lowered) and not _WITH_A_PERSON.search(lowered):
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


# The question whether a person is on the line ("Sind Sie ein Mensch?"). The
# AI Act asks for a truthful answer every time (docs/09 L1), so the core gives
# it before the model and never depends on the model's wording (agent/loop.py).
# Question order only: the verb before its subject, then at most filler words,
# then what is asked about. "Ich will mit einem Menschen sprechen" is the wish
# of `check`, not this question; "Kann ich mit einem Menschen sprechen?" too.
_ASKING = frozenset(
    {
        ("sind", "sie"),
        ("bist", "du"),
        ("ist", "das"),
        ("ist", "da"),
        ("ist", "hier"),
        ("spreche", "ich"),
        ("rede", "ich"),
        ("bin", "ich"),
        ("telefoniere", "ich"),
        # Statement order with "oder?" behind it, or said as a guess: the
        # truthful answer fits it as well (ultrareview of PR #372).
        ("sie", "sind"),
        ("du", "bist"),
    }
)
_BETWEEN = frozenset(
    {"hier", "da", "gerade", "jetzt", "denn", "eigentlich", "wirklich", "mit"}
    | {"überhaupt", "ueberhaupt", "tatsächlich", "tatsaechlich", "doch", "bei"}
    | {"ein", "eine", "einem", "einer", "echt", "echte", "echter", "echten"}
    | {"richtig", "richtige", "richtiger", "richtigen"}
    | {"künstliche", "kuenstliche", "künstlichen", "kuenstlichen"}
)
_PERSON_OR_MACHINE = frozenset(
    {"mensch", "menschen", "person", "roboter", "bot", "chatbot", "maschine"}
    | {"computer", "sprachcomputer", "ki", "intelligenz", "automat", "assistent"}
)
# What may stand around the question without asking anything else: greetings
# and the words of the question itself. Any other word, or a digit, is a
# request the model answers after the truthful sentence (agent/loop.py).
_NOTHING_ELSE = (
    frozenset({"hallo", "guten", "tag", "abend", "morgen", "moin", "servus"})
    | {"sagen", "sie", "mal", "kurz", "eine", "frage", "darf", "ich", "fragen"}
    | {"entschuldigung", "also", "ja", "nein", "äh", "ähm", "hm", "ehrlich", "oder"}
    | _BETWEEN
)
_WORD = re.compile(r"[a-zäöüß]+")


def without_human_question(text: str) -> str | None:
    """The guest's sentence without the question whether a person or a machine
    is on the line, lowercased; None when it asks no such question. `check`
    reads the rest: the question's own words ("mit einem Menschen") are those
    of the wish to talk to one, and a wish or a cancellation next to the
    question still counts (Codex PR #372). An either-or ("mit einer Maschine
    oder mit einem Menschen") is one question, both halves are cut."""
    lowered = text.lower().replace("k.i.", "ki")
    words = list(_WORD.finditer(lowered))
    found: list[tuple[int, int]] = []
    for at in range(len(words) - 1):
        if (words[at].group(), words[at + 1].group()) not in _ASKING:
            continue
        noun = _person_or_machine(words, at + 2)
        while noun is not None and noun + 1 < len(words):
            if words[noun + 1].group() != "oder":
                break
            other = _person_or_machine(words, noun + 2)
            if other is None:
                break
            noun = other
        if noun is not None:
            found.append((words[at].start(), words[noun].end()))
    if not found:
        return None
    for start, end in reversed(found):
        lowered = lowered[:start] + lowered[end:]
    return lowered


def _person_or_machine(words: list[re.Match[str]], at: int) -> int | None:
    """Where the person or machine asked about stands, from `at` on past the
    words in between; None when none follows."""
    while at < len(words) and words[at].group() in _BETWEEN:
        at += 1
    if at < len(words) and words[at].group() in _PERSON_OR_MACHINE:
        return at
    return None


def asks_more(rest: str) -> bool:
    """Whether the sentence asks for more than the question whether a person
    is on the line: `rest` is what `without_human_question` left of it."""
    if any(char.isdigit() for char in rest):
        return True
    return any(word not in _NOTHING_ELSE for word in _WORD.findall(rest))
