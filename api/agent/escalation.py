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
import unicodedata
from typing import Literal

from api.agent.consent import asks
from api.domain.menu.pos_convert import names_an_allergen
from api.domain.menu.wishes import (
    denies_allergy,
    mentions_allergy,
    only_denies,
    speaks_of_health,
)

EscalationReason = Literal["complaint", "human_requested", "cancellation"]

# Where a sentence of the guest ends.
SENTENCE = re.compile(r"(?<=[.;!?])\s+")
# Typographic hyphens, dashes and the minus sign as the plain hyphen.
_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2212", "-"))


def _folded(text: str) -> str:
    """One form of each letter, space and hyphen, lowercased, for the newer
    patterns (#649): a decomposed "ä", a no-break space in "junge Frau" or a
    typographic hyphen in "E-Nummer" no longer slips past them."""
    return unicodedata.normalize("NFKC", text).translate(_DASHES).lower()


# Reihenfolge ist die Prüfreihenfolge: "storniert die Reklamation" träfe sowohl
# complaint- als auch cancellation-Wörter, complaint zuerst geprüft ist die
# sicherere Lesart (docs/05 §4 nennt Beschwerde vor Storno).
# Patterns (`re.search` on the lowered text); every one so far is plain text
# but the two stems of a complaint below.
_TRIGGERS: tuple[tuple[EscalationReason, tuple[str, ...]], ...] = (
    (
        "complaint",
        (
            # The stems of `_COMPLAINT_WORD` with their endings, so "Ich
            # beschwere mich", "Ich beschwer mich", "beschwert" and "Ich
            # reklamiere" count (#861), and "beschwerlich", "unbeschwert"
            # and "Reklame" do not.
            r"(?<!un)beschwer(?:de|e|t|st|\b)",
            r"reklam(?:ation|ier)",
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

# An additive (§ 5 LMZDV, #638): the data model has no field for one and the
# register's is not maintained (docs/14), so nothing a model says about it can
# come from the database. The words are the register's legend and what guests
# call them; sulphur and milk protein are allergens and read as such. The
# word alone sends the call to the team, as "Spuren" does: a speech engine
# drops the question mark, and a rule that told a question from a wish by its
# word order missed "Sind da Konservierungsstoffe drin" (Codex PR #643).
# "E-Nummer" only with its hyphen: "L, E, Nummer 12" spells a street.
# "chinin" only where a word begins: "Zucchininudeln".
# shortcut: a closed word list, an additive it misses reaches the model. Add
# the word when the call log shows it.
_ADDITIVE = re.compile(
    r"zusatzstoff|geschmacksverst(?:ä|ae)rker|glutamat"
    r"|konservierungs(?:stoff|mittel)|farbstoff|antioxidationsmittel"
    r"|schw(?:ä|ae)rzungsmittel|phosphat|s(?:ü|ue)(?:ß|ss)(?:ungsmittel|stoff)"
    r"|koffein|\bchinin|\be-nummern?\b"
)


# What no fixed rule answers, whatever else the sentence says: a guest who fell
# ill, or a legal step (D19: on any doubt the team). Stems, as in `_TRIGGERS`.
# shortcut: a closed word list that misses other words for the same thing, add
# the word when the call log shows it.
# Word starts where a stem hides in other words ("zerbrochen"), "übel" but
# in "Kübel", "Dübel" and "grübeln" ("speiübel" counts), and
# "arzt" anywhere but in "Schwarztee" and "Quarztorte": "Hausarzt" counts
# (#657). "beschwerden" in any form (#861): symptoms ("Magenbeschwerden",
# "Magen-Darm-Beschwerden", "seit dem Essen Beschwerden") or several
# complaints, and on that doubt the team.
_TEAM_ONLY = re.compile(
    r"krank|(?<![kdg])(?:übel|uebel)|\berbroch|durchfall|vergift|(?<!schw)(?<!qu)arzt"
    r"|beschwerden"
    r"|ärztin|notaufnahme"
    r"|anwalt|anwältin|polizei|gesundheitsamt|verbraucherzentrale"
)
# What the guest still wants: the call ends with a remedy, and an order or a
# table asked for next to the complaint would be lost (Codex PR #479). Forms of
# the present only: "die Lieferung", "bestellt" and "abgeholt" tell what
# happened. Any of them goes to the team, the safe side.
# shortcut: a dish named with no such word ("und zweimal die 23") is missed and
# stands only on the card, add the word when the call log shows it.
_WANTS = (
    r"\b(?:m(?:ö|oe)chten?|will|wollen|h(?:ä|ae)tten?|nehmen?|brauchen?|bitte"
    r"|gerne?|bestell(?:e|en)|abholen|abzuholen|mitnehmen|mitzunehmen"
    r"|reservieren|tisch(?:e[ns]?)?)\b"
)
_WANTS_ANYTHING = re.compile(_WANTS)
# The wish that is the complaint: "ich möchte mich über die Lieferung
# beschweren". By distance, not by sentence: speech recognition sets commas
# where it likes. Further away, or behind an "und", it is a wish of its own.
# A wish of its own in the gap ("möchte gern bestellen, aber zuerst
# beschweren") is no part of it: it stays and keeps the sentence for the team
# (#657). "gern" and "bitte" may stand there. A modal wish word counts as one
# too: "Die 23 bitte, ich möchte mich auch beschweren" keeps the "bitte" in
# front for the team (#738).
# Verb forms only: "über die Bestellung beschweren" names what happened.
_OTHER_WISH = (
    r"(?:(?:bestell|abhol|mitnehm|reservier|nehm|brauch)(?:e|en)?"
    r"|m(?:ö|oe)chten?|will|wollen|h(?:ä|ae)tten?"
    r"|abzuholen|mitzunehmen|tisch(?:e[ns]?)?)\b"
)
# What else may stand in the gap: the guest, filler, the person complained to
# and a pronoun object ("das reklamieren"). "Ich möchte die 23, beschweren muss
# ich mich aber auch" has the order's own verb in front of its dish, and cut as
# the complaint's wish it took the order with it (#744 follow-up). After
# "über" or "wegen" the words name what is complained about ("über die
# Lieferung"), and so does a dish right before "reklamieren" ("die Pizza
# reklamieren", Maxi, 15.11.2026): no comma between, and no new clause after
# it ("reklamieren muss ich auch").
_COMPLAINT_GAP = (
    r"(?:mich|uns|sich|dich|ich|wir|gerne?|bitte|mal|auch|jetzt|nun|gleich"
    r"|hier|hiermit|heute|einmal|nochmal|noch|nur|kurz|eben|doch|ja|leider|erst"
    r"|zuerst|aber|au(?:ß|ss)erdem|zudem|dann|w(?:ü|ue)rden?|bei|ihnen|euch"
    r"|dar(?:ü|ue)ber|deswegen|deshalb|eine[nm]?|das|dies(?:es)?|es|etwas)\b"
)
_COMPLAINT_WORD = r"(?:beschwer|reklam)"
_GAP_WORD = r"\W+(?!und\b)(?!" + _OTHER_WISH + r")(?!" + _COMPLAINT_WORD + r")"
_DISH_RECLAIMED = (
    r"\s+(?:d(?:er|ie|as|en|em)|eine?[nm]?|meine?[nm]?|unsere?[nm]?)\b(?:\s+"
    + r"(?!und\b)(?!"
    + _OTHER_WISH
    + r")(?!"
    + _COMPLAINT_WORD
    + r")\w+){1,2}"
    + r"(?=\s+reklam\w*\b(?!\W+(?:muss|m(?:ü|ue)ss(?:en|te)|will|werde|sollte?)\b))"
)
# No comma in the phrase and no new clause after the complaint word, as for
# the dish reclaimed: "bei Ihnen Pizza, beschweren muss ich auch" orders.
_NO_NEW_CLAUSE = r"(?!\W+(?:muss|m(?:ü|ue)ss(?:en|te)|will|werde|sollte?)\b)"
# The person complained to: "beim Chef", "bei Ihrem Chef" (Graphify, PR #793).
_PERSON_COMPLAINED_TO = (
    r"\s+beim?\b(?:\s+(?!und\b)(?!"
    + _OTHER_WISH
    + r")(?!"
    + _COMPLAINT_WORD
    + r")\w+){1,2}(?=\s+"
    + _COMPLAINT_WORD
    + r"\w*\b"
    + _NO_NEW_CLAUSE
    + r")"
)
# Where the wish that is the complaint begins: a modal verb, never a bare
# "bitte" or "gern". Those close an order as often ("Die 23 bitte, beschweren
# muss ich mich auch"), and the order went with the complaint (ledger #764);
# inside the gap they may stand ("würde mich gerne beschweren"), and "gern"
# right before the verb at the start of the sentence ("Gerne würde ich mich
# beschweren"); further in, it closes an order the speech engine set no comma
# after ("die 23 gerne möchte ich mich beschweren", Codex on PR #865).
_COMPLAINT_WISH = (
    r"(?:^\W*gerne?\s+|\b)(?:m(?:ö|oe)chten?|will|wollen?|wollten?|h(?:ä|ae)tten?"
    r"|w(?:ü|ue)rden?"
    r"|nehmen?|brauchen?|bestell(?:e|en)|abholen|abzuholen|mitnehmen|mitzunehmen"
    r"|reservieren|tisch(?:e[ns]?)?)\b"
)
_WANTS_TO_COMPLAIN = re.compile(
    _COMPLAINT_WISH
    # At most four words to the complaint, whatever they are.
    + r"(?=(?:"
    + _GAP_WORD
    + r"\w+){0,4}\W+"
    + _COMPLAINT_WORD
    + r")(?:"
    + _GAP_WORD
    + _COMPLAINT_GAP
    + r")*(?:"
    + _GAP_WORD
    + r"(?:(?:ü|ue)ber|wegen)\b(?:"
    + _GAP_WORD
    + r"\w+)*|"
    + _PERSON_COMPLAINED_TO
    + r"|"
    + _DISH_RECLAIMED
    + r")?\W+"
    + _COMPLAINT_WORD
    + r"\w*"
)


def speaks_of_allergies(text: str) -> bool:
    """The guest's own allergy ("Erdnussallergie", "ich vertrage keine
    Erdnüsse") or the allergens of a dish ("Allergene")."""
    return mentions_allergy(text) or "allergen" in text.lower()


def is_question(text: str) -> bool:
    """By its mark or by its word order: a speech engine with key terms drops
    the mark on most sentences (T-10.3)."""
    return "?" in text or asks(text.lower())


def arms_the_net(text: str) -> bool:
    """Does the guest speak of their health, or someone's at the table, in
    words a draft must then carry (`guards.unnoted_allergy`)? Generous on
    purpose (Maxi, 15.11.2026): any allergy or health word in the whole text
    ("Ich habe Zöliakie", "darf keine Nüsse", "Keine Allergie, mein Mann gegen
    Sesam"), unless it is a bare denial. A false arm ("Haben Sie Gerichte für
    Allergiker?") costs one hop and a note."""
    return speaks_of_health(text) and not bare_denial(text, asked=False)


# What a bare "no" to a question about an allergy says ("Nein.", "Keine.",
# "Gegen nichts.").
_BARE_NO = frozenset(
    {"nein", "nee", "ne", "keine", "keins", "keiner", "nichts", "gegen", "danke"}
)
_WORDS = re.compile(r"\w+")


def bare_denial(text: str, *, asked: bool) -> bool:
    """A short sentence that only denies an allergy: "Ich habe keine
    Allergie", "Nein, keine Allergie", at most five words. "Nein" or "Keine"
    alone only as the answer to a question about one (`asked`): after "Darf es
    noch etwas sein?" it takes nothing back."""
    words = _WORDS.findall(text.lower())
    if not words or len(words) > 5:
        return False
    if mentions_allergy(text):
        return denies_allergy(text) or only_denies(text)
    return asked and set(words) <= _BARE_NO


def _not_denied(text: str) -> list[str]:
    """The sentences of `text` that do not only deny an allergy."""
    return [
        sentence
        for sentence in SENTENCE.split(text)
        if not (denies_allergy(sentence) or only_denies(sentence))
    ]


def asks_about_allergies(text: str) -> bool:
    """Does a guest with an allergy ask about one? A question that speaks of
    allergies or names an allergen, next to an allergy that is not denied:
    "Ist da Sellerie drin? Ich habe eine Allergie.", never "Haben Sie Pizza?
    Ich habe keine Allergie." or "Sind Sie ein Roboter? Die 99, ich habe eine
    Nussallergie."."""
    said = _not_denied(text)
    return any(speaks_of_allergies(sentence) for sentence in said) and any(
        is_question(sentence)
        and (speaks_of_allergies(sentence) or names_an_allergen(sentence))
        for sentence in said
    )


def asks_beyond_the_allergens(text: str) -> bool:
    """Does the guest ask about allergens what no database value answers, in
    words that say nothing else: traces, contamination, the same oil, pan or
    fryer, shared frying? Contact and certainty are read on the model's
    answer instead (`guards.allergen_claim`): as questions, word lists kept
    finding sentences about payment, persons or time (redesign of PR #369,
    Maxi, 27.10.2026)."""
    return _BEYOND.search(text.lower()) is not None


def names_an_additive(text: str) -> bool:
    """Does `text` name an additive ("Geschmacksverstärker", "Glutamat")? In
    a guest's sentence that is for the team, a question ("Ist da Glutamat
    drin") and a wish ("die 13 ohne Glutamat") alike: read back, the wish
    would promise what no table holds."""
    return _ADDITIVE.search(_folded(text)) is not None


def names_an_incident(text: str) -> bool:
    """Does `text` name what no fixed rule answers, an illness or a legal
    step (`_TEAM_ONLY`)? Then the team has to read the sentence."""
    return _TEAM_ONLY.search(text.lower()) is not None


def only_a_complaint(text: str) -> bool:
    """Is the complaint all this sentence holds? Not with another trigger, a
    question, an allergy, allergen or additive, a health incident, a legal
    step or a wish of its own in it: those are for the team, however well a
    rule of the catalogue fits."""
    lowered = text.lower()
    return not (
        any(
            re.search(keyword, lowered)
            for reason, keywords in _TRIGGERS
            if reason != "complaint"
            for keyword in keywords
        )
        or is_question(text)
        or speaks_of_allergies(text)
        or names_an_allergen(text)
        or asks_beyond_the_allergens(text)
        or names_an_additive(text)
        or names_an_incident(text)
        or _WANTS_ANYTHING.search(_WANTS_TO_COMPLAIN.sub(" ", lowered))
    )


# The words of a cancel that means the order read back and nothing else (#678
# review): "Oh, so lange? Dann stornieren Sie das bitte." Any other word may
# name another booking ("Tisch", "von vorhin"), one line ("die Cola"), ask
# ("kann", "später") or turn it round ("nicht"): such a sentence goes to the
# team as every cancel did. shortcut: a closed list, add a word when the call
# log shows a plain cancel it misses.
_PLAIN_CANCEL = frozenset(
    {"absagen", "abbestellen", "rückgängig", "machen"}
    | {"dann", "das", "es", "sie", "ich", "mir", "die", "meine", "ganze"}
    | {"bestellung", "lieferung", "abholung", "bitte", "lieber", "doch", "also"}
    | {"oh", "ach", "na", "so", "zu", "lang", "lange", "dauert", "mal"}
    | {"nein", "nee", "ne", "okay", "ok", "gut", "alles", "klar", "ja", "danke"}
    | {"leider", "schade", "aber", "einfach", "wieder", "gerne", "gern"}
)


def only_cancels(text: str) -> bool:
    """Does the sentence cancel and say nothing else? A "storn" word and the
    words of `_PLAIN_CANCEL`, nothing more, and the sentence that holds it no
    question: "Ist die Bestellung storniert?" asks about the state (Codex, PR
    #954). "ist" is left out of the list for the same reason: with its mark
    lost, "Ist die Bestellung storniert" still asks."""
    folded = _folded(text)
    words = _WORDS.findall(folded)
    return (
        any(word.startswith("storn") for word in words)
        and all(word.startswith("storn") or word in _PLAIN_CANCEL for word in words)
        and not any(
            sentence.rstrip().endswith("?") and "storn" in sentence
            for sentence in SENTENCE.split(folded)
        )
    )


# A decline of the order read back that holds no cancel word (Maxi, #977,
# 24.11.2026): "Nein danke, dann lasse ich es", "Vergessen Sie es, tschüss",
# "Dann doch nicht". "nicht" stands only in "doch nicht": anywhere else it turns
# the sentence round ("Vergessen Sie es nicht").
_DECLINE = re.compile(r"\b(?:lass(?:e|en|t)?|vergess(?:e|en)?|vergiss|doch nicht)\b")
# "so" only in "so lange" and "ach so": "Lassen Sie es so" keeps the order.
_SO_LONG = re.compile(r"\bach so\b|\bso(?= lange?\b)")
# The words a decline may stand with: those of a plain cancel without "so",
# the "wir" of "Lassen wir das" and a goodbye. Any other word may name one dish
# ("Lassen Sie die Zwiebeln weg") or ask for more ("Lassen Sie es liefern").
# shortcut: closed lists like `_PLAIN_CANCEL`, add a form when the call log
# shows a plain decline they miss.
_PLAIN_DECLINE = (_PLAIN_CANCEL - {"so"}) | {"wir", "auf", "bis"} | {
    "tschüss",
    "tschüs",
    "tschuess",
    "ciao",
    "wiederhören",
    "wiederhoeren",
    "wiedersehen",
}


def only_declines(text: str) -> bool:
    """Does the sentence decline the order and say nothing else? A word of
    `_DECLINE` and the words of `_PLAIN_DECLINE`, nothing more, and the
    sentence that holds it no question ("Lasse ich es?"). Whether the read-back
    was what the guest heard last is the caller's to check: to "Noch etwas?"
    the same words answer that question."""
    folded = _SO_LONG.sub(" ", _folded(text))
    found = _DECLINE.search(folded)
    rest = _WORDS.findall(_DECLINE.sub(" ", folded))
    return (
        found is not None
        and all(word in _PLAIN_DECLINE for word in rest)
        and not any(
            sentence.rstrip().endswith("?") and _DECLINE.search(sentence)
            for sentence in SENTENCE.split(folded)
        )
    )


def check(text: str) -> EscalationReason | None:
    """Erster treffende Auslöser aus docs/05 §4, oder `None`. Wird auf jedem
    rohen Kundenzug geprüft, bevor `loop.py` das Modell überhaupt aufruft."""
    lowered = text.lower()
    for reason, keywords in _TRIGGERS:
        if any(re.search(keyword, lowered) for keyword in keywords):
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
# The guest takes the agent for a person without asking whether it is one.
# The Art. 50 guidelines (point 40) want the truth said again there too
# (#638). Two cues, both closed lists.
# shortcut: another cue reaches the model. Add the wording when the call log
# shows it. "Wie heißen sie denn?" said of dishes reads the same: alone in its
# sentence it gets the fixed answer with the offer of the team instead of the
# model's answer, and a "Ja" to that offer hands the call to the team (#649).
# They ask for its name: a question about the agent, read like the one for a
# human (`without_human_question`). A filler on either side of the name is
# part of the question, not a request of its own (#649).
_FILLER = r"(?:,? (?:denn|bitte|noch ?mal))*"
_ASKS_ITS_NAME = re.compile(
    rf"\b(?:wie hei(?:ß|ss)en sie|wie hei(?:ß|ss)t du"
    rf"|wie (?:ist|war|lautet){_FILLER} (?:ihr|dein) name"
    rf"|mit wem (?:spreche|rede|telefoniere|habe) ich){_FILLER}\b"
)
# They address it as a person. No question: a "Ja" next to it still answers
# what the agent asked, so the loop only says the truthful sentence first.
_ADDRESSED_AS_A_PERSON = re.compile(
    r"\bjunge frau\b|\bjunger mann\b|\bfr(?:ä|ae)ulein\b"
    r"|\bgute frau\b|\bguter mann\b"
)


def addresses_a_person(text: str) -> bool:
    """Does the guest address the agent as a person ("Danke, junge Frau")?"""
    return _ADDRESSED_AS_A_PERSON.search(_folded(text)) is not None


def takes_for_a_person(text: str) -> bool:
    """Does the guest ask whether a person is on the line, ask for the
    agent's name or address it as a person? Then the truthful sentence comes
    first, in every turn and on every way out of it (docs/09 L1, #638)."""
    return without_human_question(text) is not None or addresses_a_person(text)


def without_human_question(text: str) -> str | None:
    """The guest's sentence without the question whether a person or a machine
    is on the line, lowercased; None when it asks no such question. `check`
    reads the rest: the question's own words ("mit einem Menschen") are those
    of the wish to talk to one, and a wish or a cancellation next to the
    question still counts (Codex PR #372). An either-or ("mit einer Maschine
    oder mit einem Menschen") is one question, both halves are cut. The
    question for the agent's name (`_ASKS_ITS_NAME`) is read like it and cut
    the same way."""
    lowered = _folded(text).replace("k.i.", "ki")
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
    found += [cue.span() for cue in _ASKS_ITS_NAME.finditer(lowered)]
    if not found:
        return None
    # Spans can overlap ("Mit wem spreche ich, mit einem Menschen?").
    rest, at = "", 0
    for start, end in sorted(found):
        rest += lowered[at : max(at, start)]
        at = max(at, end)
    return rest + lowered[at:]


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
