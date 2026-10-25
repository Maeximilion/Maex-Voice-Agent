"""Is this sentence an explicit yes, and was the draft read out? (CLAUDE.md §2 rule 3)

One detector for two users: `guards.py` lets a `confirm` through only on a yes
to a draft that was read out, and `evals/recorder.py` counts a `confirm`
without that as a hard violation. Both must agree, or the eval would report
what the core already stops, or miss what it lets through. The stand-in model
in `sim/` keeps its own detection, so it is still checked by a rule that is
not its own.

The rule: a yes is a sentence that is nothing but the assent. It holds an
assent word, and every other word carries no order ("bitte", "danke", "so").
"Ja, und noch eine Cola", "Okay, die 23" and "Ich hätte gerne noch eine Suppe"
hold a yes word and are the start of a change. One closed rule instead of a
growing list of what must not follow a yes (as for card numbers, docs/04).

A question is no yes either: "Stimmt das so?" asks whether the read-back is
right, it does not say so (#251). Speech recognition often drops the question
mark, so the word order counts as well: the verb before its subject ("Ist das
richtig", "Passt das", "Ist alles richtig") is how a German yes-no question begins,
while the statement puts them the other way round ("Das ist richtig", "Das
passt").

Read out (`read_out`) means: the sentence that ended a turn held the words of
the draft, then the question that asks for the yes ("Passt das so?", "Ist das
so richtig?"), and nothing that could change either. Another question behind
the read-back would be what the guest's yes answers.

Strict on purpose: a yes that is missed costs the guest one more question, a
yes that is wrongly heard books a table or an order nobody agreed to.
ponytail: the word lists are hand-made. An unusual yes ("von mir aus") is
missed; add the word when the call log shows it, never a pattern that lets
free text through.
"""

import re
from itertools import pairwise
from typing import Literal

_YES = frozenset(
    {
        "ja",
        "jawohl",
        "jo",
        "genau",
        "richtig",
        "passt",
        "stimmt",
        "korrekt",
        "okay",
        "ok",
        "einverstanden",
        "bestaetigt",
        "bestätigt",
    }
)
# Words that may stand next to the assent. "gern" and "gerne" are here and not
# above: alone they are how an order begins ("Ich hätte gerne ..."), next to a
# yes they are politeness (Codex PR #222, P1).
_FILLER = frozenset(
    {
        "gern",
        "gerne",
        "bitte",
        "danke",
        "dankeschön",
        "dankeschoen",
        "schön",
        "schoen",
        "vielen",
        "dank",
        "so",
        "das",
        "alles",
        "ist",
        "es",
        "gut",
        "super",
        "prima",
        "perfekt",
        "wunderbar",
        "klar",
        "in",
        "ordnung",
        "doch",
        "natürlich",
        "natuerlich",
        "sicher",
    }
)
# "nicht" and "kein" are never filler: they turn a yes around ("stimmt nicht",
# "Ja, kein Reis"). These two phrases are the exception and stay a yes
# (review PR #142).
_HARMLESS = ("kein problem", "nicht schlecht")
_WORD = re.compile(r"[a-zäöüß]+")
# Verb first, subject second: a question, with or without its mark. Only the
# verbs and subjects that are assent or filler words themselves, any other word
# makes the sentence no yes anyway.
#
# "alles" counts as the subject of a question after "ist" only ("Ist alles
# richtig"). "Passt alles" and "Stimmt alles" are how guests say yes, with the
# "es" in front left out: "Ja, passt alles." closes a real order in the eval
# targets (abholung_0064).
_VERB_SUBJECT = frozenset(
    {
        (verb, subject)
        for verb in ("ist", "stimmt", "passt")
        for subject in ("das", "es")
    }
    | {("ist", "alles")}
)
# Words that stand outside the clause that follows them: an assent or a thanks
# said first ("Ja, ist das richtig"). Any other word in front of the verb fills
# the first place of a statement: "Das ist es", "Es ist alles richtig",
# "Natürlich ist das richtig", "Genau so ist das richtig".
_OUTSIDE_THE_CLAUSE = frozenset(
    {"danke", "dankeschön", "dankeschoen", "vielen", "dank", "bitte"}
)
# A word as it is spoken, digits included: what a read-back is compared by.
_SPOKEN = re.compile(r"\w+")
# "Alles richtig", "Alles korrekt so", "Alles okay": a question or an answer.
_ELLIPTIC = re.compile(r"\balles (?:so )?(?:richtig|korrekt|okay|ok)\b")
# Assent words that are no adjective a question could end on.
_PLAIN_YES = frozenset(
    {"ja", "jawohl", "jo", "genau", "passt", "stimmt", "einverstanden"}
    | {"bestaetigt", "bestätigt"}
)
# Where one sentence ends and the next begins, as far as the text shows it.
_SENTENCE_END = re.compile(r"[.!;]")
# What a model may put in front of a read-back without saying anything about
# it. Whole phrases, hand-made and closed like the lists above: single words
# of them are no lead-in, "Noch ein Tisch ..." is another table.
_LEAD_INS = frozenset(
    {
        ("ich", "wiederhole"),
        ("zur", "kontrolle"),
        ("zur", "sicherheit"),
        ("zusammengefasst",),
        ("also",),
    }
)


def _asks(lowered: str) -> bool:
    """The word order of a question, sentence by sentence and on the words
    alone: punctuation inside a sentence is not spoken, and speech recognition
    sets a comma where it likes ("Stimmt, das so"). The end of a sentence does
    count: in "Stimmt. Das passt." the second is a statement of its own."""
    sentences = [
        " ".join(_WORD.findall(sentence)) for sentence in _SENTENCE_END.split(lowered)
    ]
    if any(_verb_first(sentence.split()) for sentence in sentences):
        return True
    # Two short forms leave out what would tell them apart. "Alles richtig" has
    # no verb: the question "Ist alles richtig?" as much as the answer. "Ist so
    # richtig" has no subject: "Ist das so richtig?" as much as "Das ist so
    # richtig". Text has no voice that rises or falls, so either counts as a
    # yes only next to a word that cannot ask ("Ja, ist okay").
    words = {word for sentence in sentences for word in sentence.split()}
    return not words & _PLAIN_YES and any(
        _ELLIPTIC.search(sentence) or _opens_with_ist(sentence.split())
        for sentence in sentences
    )


def _opens_with_ist(words: list[str]) -> bool:
    """ "Ist ..." as the first word of its clause, an assent or a thanks in
    front not counted: the verb without a subject before it."""
    at = 0
    while at < len(words) and (words[at] in _YES or words[at] in _OUTSIDE_THE_CLAUSE):
        at += 1
    return at < len(words) and words[at] == "ist"


def _verb_first(words: list[str]) -> bool:
    """Does the verb open its clause, with the subject right behind it? Only
    then it is the order of a question. With anything else in the first place
    the verb comes second, as in every German statement."""
    return any(
        pair in _VERB_SUBJECT
        and all(word in _YES or word in _OUTSIDE_THE_CLAUSE for word in words[:at])
        for at, pair in enumerate(pairwise(words))
    )


def _neutral_start(words: list[str]) -> bool:
    """What may stand in front of a read-back: nothing, polite words, and after
    them at most one whole lead-in phrase."""
    at = 0
    while at < len(words) and _polite([words[at]]):
        at += 1
    return at == len(words) or tuple(words[at:]) in _LEAD_INS


def _polite(words: list[str]) -> bool:
    """Nothing but assent and filler words: they add nothing to what is said."""
    return all(word in _YES or word in _FILLER for word in words)


def is_yes(text: str) -> bool:
    """An assent word, and nothing else but filler. A digit is never part of a
    yes: "Ja, 2" changes a quantity. A question is none: it asks."""
    lowered = text.lower()
    if any(char.isdigit() for char in lowered):
        return False
    if "?" in lowered or _asks(lowered):
        return False
    for phrase in _HARMLESS:
        lowered = lowered.replace(phrase, " ")
    words = _WORD.findall(lowered)
    return any(word in _YES for word in words) and all(
        word in _YES or word in _FILLER for word in words
    )


def _without_closing_question(readback: str) -> str:
    """The read-back without the question that closes it ("Passt das so?").
    Cut with string functions and not a pattern: the sentence holds the guest's
    name and note."""
    text = readback.rstrip()
    cut = max(text.rfind(". "), text.rfind("! "))
    return text[:cut] if text.endswith("?") and cut != -1 else text


# The questions that ask for the yes to what was just read out, word for word.
# Closed on purpose: the same words in another order or with one more ask
# something else ("Ist es richtig sicher?" asks about safety, and "richtig"
# means "really" there). The question a read-back brings itself always counts.
_CONFIRMATION_QUESTIONS = frozenset(
    {
        ("passt", "das", "so"),
        ("passt", "das"),
        ("passt", "es", "so"),
        ("stimmt", "das", "so"),
        ("stimmt", "das"),
        ("ist", "das", "so", "richtig"),
        ("ist", "das", "richtig"),
        ("ist", "das", "so", "korrekt"),
        ("ist", "das", "korrekt"),
    }
)


def _asks_to_confirm(words: list[str], own: list[str]) -> bool:
    """Is this the question the guest's yes answers: the read-back's own, or
    one of `_CONFIRMATION_QUESTIONS`."""
    return bool(words) and (words == own or tuple(words) in _CONFIRMATION_QUESTIONS)


def read_out(say: str, readback: str) -> bool:
    """Did this sentence read the draft to the guest and ask for a yes to it?

    Three parts, on the words alone (case and punctuation are not spoken):

    - The words of the draft, in order and without a gap: a read-back that
      leaves the name out or says another number is not this draft.
    - Behind them the question that asks for the yes, the draft's own or one
      of a few fixed forms (`_asks_to_confirm`), and nothing else. Without it the
      guest's "Ja" answers whatever was said last: "Das ist falsch: <draft>."
      asks nothing, and "<draft>. Möchten Sie noch einen Hochstuhl?" asks
      about the high chair. A read-back that is a question as a whole needs
      no second one.
    - In front of them nothing, or words that say nothing about the draft:
      polite ones ("Gerne, ...") and one whole phrase of `_LEAD_INS` ("Ich
      wiederhole: ..."). Any other word may change what follows ("Einmal
      statt zweimal ...", "Das ist falsch: ..."), and punctuation is no proof
      that it does not.
    """
    body = _without_closing_question(readback)
    draft = _SPOKEN.findall(body.lower())
    own_question = _SPOKEN.findall(readback.rstrip()[len(body) :].lower())
    said = _SPOKEN.findall(say.lower())
    size = len(draft)
    if not size:
        return False
    asks_itself = body.rstrip().endswith("?")
    for at in range(len(said) - size + 1):
        if said[at : at + size] != draft:
            continue
        after = said[at + size :]
        if not (_asks_to_confirm(after, own_question) or (asks_itself and not after)):
            continue
        if _neutral_start(said[:at]):
            return True
    return False


# The question for the caller's consent to the allergy as a kitchen note, word
# for word as Maxi decided it (legal check L13). The core asks it, never the
# model, and the consent log stores it as the version that was asked.
ALLERGY_QUESTION = "Darf ich die Allergie als Hinweis für die Küche notieren?"
# Words a yes to that question may carry besides the assent and the filler of
# `is_yes`: "Ja, notieren Sie das", "Ja, auf jeden Fall".
_NOTE_IT = frozenset(
    {"notieren", "aufschreiben", "weitergeben", "sie", "unbedingt", "auf"}
    | {"jeden", "fall", "duerfen", "dürfen"}
)
_NO = frozenset({"nein", "nee", "nö", "noe"})
# "Lieber nicht", "Nein, bitte nicht", "Nicht nötig". "doch" is no part of a
# no: "Nein, doch" takes it back.
_NO_FILLER = (_FILLER - {"doch"}) | {"lieber", "besser", "nicht", "nötig", "noetig"}


def consent_answer(text: str) -> Literal["yes", "no", "unclear"]:
    """The answer to `ALLERGY_QUESTION`. A yes as strict as for a booking
    (`is_yes`), with the words that say "note it" besides: anything more, as
    in "Ja, und noch eine Cola", is no explicit consent. A no is a sentence of
    a no word or "nicht" and filler ("Nein, nicht notieren"). Anything else is
    unclear, and unclear is no consent (L13: only an explicit "ja" stores the
    reason)."""
    lowered = text.lower()
    rest = _WORD.sub(
        lambda word: " " if word.group() in _NOTE_IT else word.group(), lowered
    )
    if is_yes(text) or is_yes(rest):
        return "yes"
    if "?" in lowered or any(char.isdigit() for char in lowered):
        return "unclear"
    words = _WORD.findall(lowered)
    if (_NO & set(words) or "nicht" in words) and all(
        word in _NO or word in _NO_FILLER or word in _NOTE_IT for word in words
    ):
        return "no"
    return "unclear"
