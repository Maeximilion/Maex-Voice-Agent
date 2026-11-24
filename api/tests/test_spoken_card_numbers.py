"""Card numbers as the agent says them (#987): the prefix in capitals.

The menu stores every number lower case (docs/14), so without this the guest
heard "Nummer s12". The trailing letter stays as stored ("35b", "25g"): the
form of the eval menu and of the voice runs of T-10.3, where the voice read
"23g" as a letter (docs/20).
"""

import uuid

import pytest

from api.agent import guards
from api.domain.menu.details import say_allergens
from api.domain.menu.numberwords import spoken_card
from api.domain.menu.search import say_understood
from api.domain.ordering.readback import SpokenLine, _spoken_line
from api.schemas.menu import MenuHit


@pytest.mark.parametrize(
    ("stored", "spoken"),
    [
        ("s12", "S12"),
        ("sm1", "SM1"),
        ("s07a", "S07a"),
        ("35b", "35b"),
        ("25g", "25g"),
        ("13", "13"),
        ("007", "007"),
        ("s99 oder sm99", "S99 oder SM99"),
    ],
)
def test_the_prefix_is_said_in_capitals(stored, spoken):
    assert spoken_card(stored) == spoken


def test_the_read_back_says_the_prefix_in_capitals():
    line = SpokenLine("s12", "Thunfisch Maki", 2, [], None)
    assert _spoken_line(line) == "Zweimal Nummer S12 Thunfisch Maki."


def test_the_read_back_keeps_a_plain_and_a_lettered_number():
    assert _spoken_line(SpokenLine("35b", "Bun Cha", 1, [], None)) == (
        "Einmal Nummer 35b Bun Cha."
    )


def test_the_allergen_answer_says_the_prefix_in_capitals():
    assert "Nummer S12 Thunfisch Maki: Fisch." in say_allergens(
        "s12", "Thunfisch Maki", ["Fisch"]
    )


def test_the_core_price_sentence_says_the_prefix_in_capitals():
    say = guards._price_say(
        {"name": "Thunfisch Maki", "number": "sm1", "price_cents": 450}
    )
    assert say.startswith("Die SM1, Thunfisch Maki, kostet")


def _hit(number: str, name: str) -> MenuHit:
    return MenuHit(
        menu_item_id=uuid.uuid4(),
        number=number,
        name=name,
        price_cents=450,
        sold_out=False,
    )


def test_what_was_understood_says_the_prefix_in_capitals():
    say = say_understood(
        [
            ("exact_number", _hit("s12", "Thunfisch Maki"), None),
            ("alias", _hit("sm1", "Lachs Maki"), None),
            ("exact_number", _hit("25g", "Soße Chop Suey"), None),
        ],
        "Die S12, Lachs Maki und die 25g",
    )
    assert say is not None
    assert "Nummer S12, Nummer SM1 Lachs Maki und Nummer 25g" in say
