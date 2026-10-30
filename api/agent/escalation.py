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

from api.agent.consent import asks, asks_on_its_own
from api.domain.menu.pos_convert import names_an_allergen
from api.domain.menu.wishes import denies_allergy, mentions_allergy, only_denies

EscalationReason = Literal["complaint", "human_requested", "cancellation"]

# Where a sentence of the guest ends.
SENTENCE = re.compile(r"(?<=[.;!?])\s+")
# What a question behind a comma asks when it has nothing to do with a dish: a
# table, the hours, delivery, time, payment. Any other question next to an
# allergy stays an allergy question, the safe side ("Ich habe eine
# Nussallergie, was empfehlen Sie?").
# shortcut: a closed list, widen it only from what the call log shows.
_OTHER_TOPIC = re.compile(
    r"\b(?:tisch|platz|plätze|reservier|geöffnet|offen|liefer|abhol"
    r"|lange|wann|uhr\b|zahl|bezahl)|\b(?:was|etwas|noch) frei(?:\?|$)|\bauf(?:\?|$)"
)

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
    # "Spuren", also inside a word: "Erdnussspuren", "spurenfrei".
    r"spuren|kontamin|verunreinig|kreuzkontakt"
    # "dasselbe Öl", "in demselben Öl", "das gleiche Öl", "ein eigenes Öl".
    r"|\b(?:(?:das|dem|den|der|die)?selben?|gleich(?:e[nmrs]?)?|eigene[nmrs]?"
    r"|separate[nmrs]?|getrennte[nmrs]?) (?:öl|oel|pfanne|fritteuse|friteuse)"
    # Shared frying with something: "zusammen mit den Garnelen frittiert",
    # "mit Fisch zusammen frittiert"; not "alles zusammen, ... Wan-Tan
    # frittiert" in an order (own review of PR #369).
    r"|\bzusammen mit\b[^.?!]{0,40}\bfrittier|\bmit\b[^.?!]{0,40}\bzusammen (?:ge)?frittier"
)


def speaks_of_allergies(text: str) -> bool:
    """The guest's own allergy ("Erdnussallergie", "ich vertrage keine
    Erdnüsse") or the allergens of a dish ("Allergene")."""
    return mentions_allergy(text) or "allergen" in text.lower()


def is_question(text: str) -> bool:
    """By its mark or by its word order: a speech engine with key terms drops
    the mark on most sentences (T-10.3)."""
    return "?" in text or asks(text.lower())


def _names_one(text: str) -> bool:
    return speaks_of_allergies(text) or names_an_allergen(text)


def _asks_about_one(sentence: str) -> bool:
    """Does the sentence ask about an allergy? A clause that asks and names
    one, or, next to a clause that names one, a question that may mean it
    without naming it, unless it asks about something else ("..., haben Sie
    Platz?"). Such a question needs its mark or `asks_on_its_own`: "Ist gut,
    ich habe eine Erdnussallergie" and "Ist zwölf" (the card number S12,
    docs/04) ask nothing."""
    clauses = [clause.strip().lower() for clause in sentence.split(",")]
    if any(is_question(clause) and _names_one(clause) for clause in clauses):
        return True
    return _names_one(sentence) and any(
        ("?" in clause or asks_on_its_own(clause)) and not _OTHER_TOPIC.search(clause)
        for clause in clauses
    )


def asks_about_allergies(text: str) -> bool:
    """Does a guest with an allergy ask about one? A question about an
    allergy (`_asks_about_one`) next to an allergy that is not denied: "Ist da
    Sellerie drin? Ich habe eine Allergie.", never "Haben Sie Pizza? Ich habe
    keine Allergie." or "Sind Sie ein Roboter? Die 99, ich habe eine
    Nussallergie."."""
    said = [
        sentence
        for sentence in SENTENCE.split(text)
        if not (denies_allergy(sentence) or only_denies(sentence))
    ]
    return any(speaks_of_allergies(sentence) for sentence in said) and any(
        _asks_about_one(sentence) for sentence in said
    )


def asks_beyond_the_allergens(text: str) -> bool:
    """Does the guest ask about allergens what no database value answers, in
    words that say nothing else: traces, contamination, the same oil, pan or
    fryer, shared frying? Contact and certainty are read on the model's
    answer instead (`guards.allergen_claim`): as questions, word lists kept
    finding sentences about payment, persons or time (redesign of PR #369,
    Maxi, 27.10.2026)."""
    return _BEYOND.search(text.lower()) is not None


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
