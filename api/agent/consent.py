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
the draft and ended with them, or with a closing question made of the words
above ("Passt das so?", "Ist das so richtig?"). Another question behind the
read-back would be what the guest's yes answers.

Strict on purpose: a yes that is missed costs the guest one more question, a
yes that is wrongly heard books a table or an order nobody agreed to.
ponytail: the word lists are hand-made. An unusual yes ("von mir aus") is
missed; add the word when the call log shows it, never a pattern that lets
free text through.
"""

import re

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
# makes the sentence no yes anyway. Not when a subject already stood before the
# verb: "Das ist es" and "Es ist alles richtig" are statements.
#
# "alles" counts as the subject of a question after "ist" only ("Ist alles
# richtig"). "Passt alles" and "Stimmt alles" are how guests say yes, with the
# "es" in front left out: "Ja, passt alles." closes a real order in the eval
# targets (abholung_0064).
_ASKS = re.compile(
    r"(?<!das )(?<!es )\b(?:(?:ist|stimmt|passt)\s+(?:das|es)|ist\s+alles)\b"
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
_BREAK = frozenset(".!?:;")


def _asks(lowered: str) -> bool:
    """The word order of a question, sentence by sentence and on the words
    alone: punctuation inside a sentence is not spoken, and speech recognition
    sets a comma where it likes ("Stimmt, das so"). The end of a sentence does
    count: in "Stimmt. Das passt." the second is a statement of its own."""
    sentences = [
        " ".join(_WORD.findall(sentence)) for sentence in _SENTENCE_END.split(lowered)
    ]
    if any(_ASKS.search(sentence) for sentence in sentences):
        return True
    # "Alles richtig" has no verb to go by: it is the short form of the question
    # "Ist alles richtig?" as much as of the answer, and text has no voice that
    # rises or falls. It counts as a yes only next to a word that cannot ask.
    words = {word for sentence in sentences for word in sentence.split()}
    return not words & _PLAIN_YES and any(
        _ELLIPTIC.search(sentence) for sentence in sentences
    )


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


def read_out(say: str, readback: str) -> bool:
    """Did this sentence read the draft to the guest, and end with it?

    The words of the draft, in order and without a gap: a read-back that leaves
    the name out or says another number is not this draft. Case and punctuation
    do not count, they are not spoken.

    What comes after may only be the closing question, in the draft's words or
    the model's own, as long as it is made of assent and filler words. "...
    Möchten Sie noch einen Hochstuhl?" is not: the guest's yes would answer
    the high chair.

    What comes before must not run into the draft: "Einmal statt zweimal
    Nummer 23 ..." holds every word of "Zweimal Nummer 23 ..." and says another
    order. So the draft begins the sentence, or follows polite words ("Gerne,
    ..."), or follows a sentence that has ended ("Wir haben bis 22 Uhr
    geöffnet. ...", "Ich wiederhole: ...").
    """
    draft = _SPOKEN.findall(_without_closing_question(readback).lower())
    lowered = say.lower()
    tokens = list(_SPOKEN.finditer(lowered))
    said = [token.group() for token in tokens]
    size = len(draft)
    if not size:
        return False
    for at in range(len(said) - size + 1):
        if said[at : at + size] != draft or not _polite(said[at + size :]):
            continue
        if (
            at == 0
            or _polite(said[:at])
            or _BREAK & set(lowered[tokens[at - 1].end() : tokens[at].start()])
        ):
            return True
    return False
