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


def test_an_order_without_more_is_a_pickup():
    """Until delivery is built an order by phone is a pickup (T-6.5)."""
    assert heard("Ich möchte gerne etwas bestellen") == "pickup"


@pytest.mark.parametrize(
    "sentence",
    [
        "Ich hätte gerne einen Tisch für vier Personen",
        "Ich möchte reservieren",
        "Eine Reservierung für morgen Abend",
        "Haben Sie heute Abend noch Platz für zwei?",
        "Haben Sie noch Plätze frei?",
    ],
)
def test_a_table_is_heard(sentence):
    assert heard(sentence) == "reservation"


def test_ordering_a_table_is_a_reservation():
    """ "bestellen" alone is no pickup: a guest also orders a table."""
    assert heard("Ich möchte einen Tisch bestellen") == "reservation"


@pytest.mark.parametrize(
    "sentence",
    [
        # Two wishes in one sentence: which one is meant is the model's call.
        "Einen Tisch für vier, und vorher etwas zum Mitnehmen",
        "Kein Tisch, ich will nur was abholen",
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
        "Haben Sie auch etwas Vegetarisches?",
        "Gibt es heute Mittagstisch?",
        "Ist das asiatisch?",
    ],
)
def test_a_table_word_inside_another_word_is_no_table(sentence):
    assert heard(sentence) is None
