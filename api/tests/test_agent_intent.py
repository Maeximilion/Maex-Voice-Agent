"""agent/intent.py: what the guest called for, heard in their own sentence."""

import pytest

from api.agent.intent import heard


@pytest.mark.parametrize(
    "sentence",
    [
        "Ich möchte etwas zum Abholen bestellen",
        "Guten Tag, eine Abholung bitte",
        "Zum Mitnehmen, bitte",
        "Ich würde gern etwas zur Selbstabholung bestellen",
        "Kann ich bei Ihnen auch etwas mitnehmen?",
        "Ich hätte gern was abzuholen",
        "ABHOLUNG",
    ],
)
def test_pickup_is_heard(sentence):
    assert heard(sentence) == "pickup"


@pytest.mark.parametrize(
    "sentence",
    [
        "Ich hätte gerne einen Tisch für vier Personen",
        "Ich möchte reservieren",
        "Eine Tischreservierung für morgen Abend",
        "Haben Sie heute Abend noch Platz für zwei?",
        "Haben Sie noch Plätze frei?",
        "Haben Sie noch Plaetze oder Tische frei?",
    ],
)
def test_a_table_is_heard(sentence):
    assert heard(sentence) == "reservation"


def test_an_order_without_more_is_a_pickup():
    """Until delivery is built an order by phone is a pickup (T-6.5)."""
    assert heard("Ich möchte gerne etwas bestellen") == "pickup"


def test_ordering_a_table_is_a_reservation():
    """ "bestellen" alone is no pickup: a guest also orders a table."""
    assert heard("Ich möchte einen Tisch bestellen") == "reservation"


def test_the_order_word_alone_does_not_talk_a_known_wish_away():
    """The weakest signal: in a call about a table the question is about the
    food for that table."""
    sentence = "Können wir das Essen schon vorbestellen?"
    assert heard(sentence, "reservation") is None
    assert heard(sentence) == "pickup"


def test_a_named_wish_replaces_a_known_one():
    assert heard("Dann lieber zum Mitnehmen", "reservation") == "pickup"
    assert heard("Ach nein, lieber einen Tisch für vier", "pickup") == "reservation"


@pytest.mark.parametrize(
    "sentence",
    [
        # Two wishes in one sentence: which one is meant is the model's call.
        "Einen Tisch für vier, und vorher etwas zum Mitnehmen",
        # Delivery is not offered yet and must not be read as a pickup.
        "Ich möchte etwas bestellen, zum Liefern",
        "Können Sie mir das liefern, oder muss ich es abholen?",
        # No wish at all.
        "Es zwölf bitte",
        "Die 23 und einmal Pho Bo",
        "Müller, mit Doppel-L",
        "",
    ],
)
def test_a_sentence_that_names_no_single_wish_says_nothing(sentence):
    assert heard(sentence) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "Nicht zum Abholen, wir essen bei Ihnen",
        "Ich möchte nicht reservieren",
        "Kein Tisch, ich will nur was abholen",
        "Keine Abholung",
    ],
)
def test_a_wish_that_is_ruled_out_is_not_heard(sentence):
    """A wrong wish would stand in every turn after it; a missed one costs
    nothing the call did not lack before."""
    assert heard(sentence) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "Haben Sie auch etwas Vegetarisches?",
        "Gibt es heute Mittagstisch?",
        "Ist das asiatisch?",
        # A surname or a wine that begins like a table word.
        "Auf den Namen Tischler",
        "Platzer ist mein Name",
        "Eine Flasche Gran Reserva",
    ],
)
def test_a_table_word_inside_another_word_is_no_table(sentence):
    assert heard(sentence) is None
