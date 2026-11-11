"""agent/escalation.py: Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell."""

import unicodedata

import pytest

from api.agent.escalation import (
    addresses_a_person,
    asks_more,
    check,
    names_an_additive,
    without_human_question,
)


def test_beschwerde_wird_erkannt():
    assert check("Ich möchte mich beschweren, das war eine Katastrophe") == "complaint"


def test_mensch_wunsch_wird_erkannt():
    assert check("Kann ich bitte mit einem Menschen sprechen?") == "human_requested"


def test_storno_wird_erkannt():
    assert check("Ich möchte meine Reservierung stornieren") == "cancellation"


def test_storno_als_substantiv_wird_erkannt():
    """Die kanonische Kundenformulierung aus docs/05 §4 ("Storno meiner
    Reservierung") traf bisher kein Stichwort, nur die Verbformen (Codex-Review
    PR #102, P2)."""
    assert check("Storno meiner Reservierung, bitte") == "cancellation"


def test_unauffaelliger_text_loest_nichts_aus():
    assert check("Ich hätte gerne einen Tisch für vier Personen") is None


def test_erkennung_ist_gross_klein_unabhaengig():
    assert check("BESCHWERDE!") == "complaint"


def test_leerer_text_loest_nichts_aus():
    assert check("") is None


# -- without_human_question: whether a person is on the line (docs/09 L1) ------


def _rest(text: str) -> str:
    rest = without_human_question(text)
    assert rest is not None
    return rest


@pytest.mark.parametrize(
    "text",
    [
        "Sind Sie ein Mensch?",
        "sind sie ein echter mensch",
        "Bist du eine KI?",
        "Ist das ein Roboter?",
        "Spreche ich mit einem Menschen?",
        "Spreche ich da gerade mit einer Maschine?",
        "Rede ich hier mit einem Computer?",
        "Bin ich mit einem echten Menschen verbunden?",
        "Moment, sind Sie ein Bot?",
        "Sind Sie eine künstliche Intelligenz?",
        "Sind Sie ein Mensch oder eine Maschine?",
        "Ist das ein Chatbot? Ich wollte bestellen.",
        # Ultrareview of PR #372: phrasings the first list missed.
        "Sind Sie überhaupt ein Mensch?",
        "Sind Sie tatsächlich ein Mensch?",
        "Ist da ein Mensch?",
        "Ist hier ein Mensch dran?",
        "Bin ich bei einem Roboter?",
        "Sind Sie eine K.I.?",
        "Sie sind ein Roboter, oder?",
        "Du bist doch eine Maschine?",
    ],
)
def test_the_question_for_a_human_is_recognised(text):
    assert without_human_question(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        # The wish, not the question: escalation, not an answer.
        "Kann ich bitte mit einem Menschen sprechen?",
        "Ich will mit einem Menschen sprechen.",
        # The words, but no question about the agent.
        "Ist das Gericht auch für Menschen mit Nussallergie?",
        "Ist das automatisch mit Reis?",
        "Sind Sie echt schon ausgebucht?",
        "Ich bin ein Mensch, der gern scharf isst.",
        "Ist das Problem mit dem Computer gelöst?",
        "Ich hätte gerne einen Tisch für vier Personen",
        "",
    ],
)
def test_other_sentences_are_no_question_for_a_human(text):
    assert without_human_question(text) is None


def test_what_else_the_sentence_says_is_left_for_check():
    """Codex PR #372: the words of the question ("mit einem Menschen") are
    those of the wish to talk to one; read past them, a cancellation or the
    wish itself in the same sentence still counts."""
    rest = without_human_question(
        "Spreche ich mit einem Menschen? Ich möchte meine Reservierung stornieren."
    )
    assert rest is not None and "mensch" not in rest
    assert check(rest) == "cancellation"

    rest = without_human_question(
        "Sind Sie ein Mensch? Ich will mit einem Menschen sprechen."
    )
    assert rest is not None
    assert check(rest) == "human_requested"

    assert check(_rest("Spreche ich mit einem Menschen?")) is None


@pytest.mark.parametrize(
    "text",
    [
        "Spreche ich mit einer Maschine oder mit einem Menschen?",
        "Spreche ich hier mit einem Roboter oder mit einem Menschen?",
        "Bin ich mit einem Computer oder mit einem Menschen verbunden?",
    ],
)
def test_both_halves_of_an_either_or_question_are_the_question(text):
    """Ultrareview of PR #372: the half after "oder" holds the words of the
    wish ("mit einem Menschen") and is still the question."""
    assert check(_rest(text)) is None


@pytest.mark.parametrize(
    ("text", "more"),
    [
        ("Sind Sie ein Mensch?", False),
        ("Hallo, sagen Sie mal, sind Sie ein Mensch?", False),
        ("Guten Tag, ist das ein Roboter? Ehrlich jetzt?", False),
        ("Sind Sie ein Roboter? Ich möchte einen Tisch für vier.", True),
        ("Ist das ein Computer? Morgen um 19 Uhr bitte.", True),
        ("Sind Sie ein Mensch? 2 mal die 13", True),
    ],
)
def test_asks_more_tells_a_request_from_greeting_words(text, more):
    assert asks_more(_rest(text)) is more


# -- taken for a person without asking (Art. 50 guidelines point 40, #638) -----


@pytest.mark.parametrize(
    ("text", "more"),
    [
        ("Wie heißen Sie denn?", False),
        ("wie heissen sie", False),
        ("Wie war noch mal Ihr Name?", False),
        ("Hallo, mit wem spreche ich?", False),
        ("Wie heißt du eigentlich?", False),
        ("Wie heißen Sie? Einen Tisch für vier bitte.", True),
    ],
)
def test_the_question_for_the_agents_name_is_read_like_the_one_for_a_human(text, more):
    rest = without_human_question(text)
    assert rest is not None
    assert asks_more(rest) is more


@pytest.mark.parametrize(
    "text",
    [
        "Wie ist Ihr Name bitte?",
        "Wie ist Ihr Name, bitte?",
        "Wie war Ihr Name noch mal?",
        "Wie heißen Sie bitte?",
        "Mit wem spreche ich bitte?",
    ],
)
def test_a_filler_behind_the_name_question_asks_nothing_more(text):
    """#649: "Wie war noch mal Ihr Name?" got the offer of the team, the same
    filler behind the name counted as a request of its own."""
    rest = without_human_question(text)
    assert rest is not None
    assert not asks_more(rest)


@pytest.mark.parametrize(
    "text",
    [
        # Decomposed umlaut: "a" and the combining mark.
        unicodedata.normalize("NFD", "Ist da Geschmacksverstärker drin?"),
        "Sind da E\u2011Nummern drin?",  # non-breaking hyphen
        "Sind da E\u2013Nummern drin?",  # en dash
    ],
)
def test_an_additive_in_another_unicode_form_is_still_read(text):
    """#649: the patterns read one form of each letter and of the hyphen."""
    assert names_an_additive(text)


@pytest.mark.parametrize(
    "text",
    [
        "Danke, junge\u00a0Frau.",  # no-break space
        unicodedata.normalize("NFD", "Danke, Fräulein."),
    ],
)
def test_an_address_in_another_unicode_form_is_still_read(text):
    assert addresses_a_person(text)


@pytest.mark.parametrize(
    "text",
    [
        "Danke, junge Frau, das wars dann.",
        "Junger Mann, zweimal die 13 bitte.",
        "Guten Tag, gute Frau, ich hätte gern einen Tisch.",
        "Fraeulein?",
        "Ja, junge Frau.",
    ],
)
def test_an_address_as_a_person_is_a_cue_and_no_question(text):
    """A "Ja" next to it answers what the agent asked: read as a question, it
    got the offer of the team and the yes was lost."""
    assert addresses_a_person(text)
    assert without_human_question(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Wie heißt das Gericht mit den Nudeln?",
        "Wie ist der Name der Suppe?",
        "Auf welchen Namen? Mein Name ist Müller.",
        "Ich komme mit meiner Frau und einem jungen Mann.",
        "Mit wem muss ich wegen der Rechnung sprechen?",
    ],
)
def test_other_sentences_take_the_agent_for_no_person(text):
    assert without_human_question(text) is None
    assert not addresses_a_person(text)


def test_a_cue_inside_the_question_is_cut_once():
    """The name question and the question for a human share words: cut one
    after the other, the second cut would take what follows the sentence."""
    rest = without_human_question(
        "Mit wem spreche ich, mit einem Menschen? Einen Tisch für vier bitte."
    )
    assert rest is not None
    assert "tisch für vier bitte" in rest
    assert "mensch" not in rest and "wem" not in rest
