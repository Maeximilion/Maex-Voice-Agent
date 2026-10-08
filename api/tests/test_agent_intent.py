"""agent/intent.py: what the guest called for, heard in their own sentence."""

import pytest

from api.agent import intent
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
        "Haben Sie auch etwas Vegetarisches zum Mitnehmen?",
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
        "Haben Sie noch Tische frei?",
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


@pytest.mark.parametrize(
    "sentence",
    [
        "Ich möchte einen Vierertisch bestellen",
        "Können wir einen Stammtisch bestellen?",
        "Ich möchte vier Sitzplätze bestellen",
        "Ich würde gern vier Plaetze bestellen",
    ],
)
def test_ordering_something_table_like_is_no_pickup(sentence):
    """A compound is not sure enough to be a table and names no pickup."""
    assert heard(sentence) is None


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


RULED_OUT = [
    "Nicht zum Abholen, wir essen bei Ihnen",
    "Ich möchte nicht reservieren",
    "Keine Abholung",
    "Zum Abholen? Nein, wir essen bei Ihnen",
    "Wir wollen diesmal vor Ort essen statt abholen",
    "Heute mal ohne Abholung, wir kommen vorbei",
    "Ich hole es selbst, also ohne Tisch",
]


@pytest.mark.parametrize("sentence", RULED_OUT)
def test_a_wish_that_is_ruled_out_is_not_heard(sentence):
    """A wrong wish would stand in every turn after it; a missed one costs
    nothing the call did not lack before."""
    assert heard(sentence) is None


@pytest.mark.parametrize("sentence", RULED_OUT)
def test_ruled_out_cases_depend_on_the_negation_rule(sentence, monkeypatch):
    """Each case above is one the negation rule decides, not another rule."""
    monkeypatch.setattr(intent, "_NEGATION", intent.re.compile(r"(?!)"))
    assert heard(sentence) is not None


@pytest.mark.parametrize(
    "sentence",
    [
        "Haben Sie auch etwas Vegetarisches?",
        "Gibt es heute Mittagstisch?",
        "Ist das asiatisch?",
        # A surname or a wine that begins like a table word.
        "Auf den Namen Tischler",
        "Eine Flasche Gran Reserva",
        # "Platz" is no table word: it stands in addresses.
        "Sind Sie das Restaurant am Berliner Platz?",
        "Konrad-Adenauer-Platz 5",
        "Platzer ist mein Name",
    ],
)
def test_a_word_that_only_looks_like_a_table_is_none(sentence):
    assert heard(sentence) is None
