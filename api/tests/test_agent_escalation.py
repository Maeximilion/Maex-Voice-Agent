"""agent/escalation.py: Sofort-Auslöser aus docs/05 §4, geprüft vor dem Modell."""

import pytest

from api.agent.escalation import asks_if_human, check


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


# -- asks_if_human: the question whether a person is on the line (docs/09 L1) --


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
    ],
)
def test_the_question_for_a_human_is_recognised(text):
    assert asks_if_human(text)


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
    assert not asks_if_human(text)
