"""What the parser tells the core about the guest's allergy (T-11.1, L13, D24).

The core reads the allergy from the guest's own sentence (`agent/allergies.py`)
and needs three things from the parser: the ingredient, whether it is another
adult's (the caller's "ja" counts for themselves and their own child, never
for another adult), and the answer to "Wogegen?". Pure functions, no database.
Caller sentences in German: the parser reads German.
"""

import pytest

from api.domain.menu.numberwords import NO_PREFIXES
from api.domain.menu.wishes import (
    WITHDRAWN,
    classify_wish,
    mentions_allergy,
    names_an_order,
    of_another_adult,
    only_denies,
    removal_with,
    which_answer,
)

REASONED = "WICHTIG: Keine Erdnuss. Grund: Allergie"


@pytest.mark.parametrize(
    "said",
    [
        "meine Frau hat eine Erdnussallergie",
        "mein Mann ist allergisch gegen Sesam",
        "ein Freund von mir verträgt keine Nüsse",
        "er hat eine Nussallergie",
        "sie ist allergisch gegen Erdnüsse",
        "wir haben eine Nussallergie",
        "unsere Oma verträgt keine Milch",
    ],
)
def test_an_adult_other_than_the_caller_is_another_adult(said):
    assert of_another_adult(said)


@pytest.mark.parametrize(
    "said",
    [
        "ich habe eine Erdnussallergie",
        "mein Sohn ist allergisch gegen Sesam",
        "meine Tochter hat eine Nussallergie",
        "wegen meiner Erdnussallergie",
        # "Sie haben" addresses the restaurant, it names nobody else.
        "Sie haben doch die 23, ich bin allergisch gegen Erdnüsse",
        "Erdnussallergie",
    ],
)
def test_the_caller_and_their_own_child_are_no_other_adult(said):
    assert not of_another_adult(said)


def test_the_caller_s_own_allergy_is_not_flagged():
    wish = classify_wish("ich habe eine Erdnussallergie", [])
    assert wish is not None
    assert (wish.kind, wish.ingredient, wish.text) == ("allergy", "Erdnuss", REASONED)
    assert not wish.another_adult


def test_another_adult_s_allergy_is_flagged_and_never_sent():
    wish = classify_wish("ohne Zwiebeln, meine Frau hat eine Erdnussallergie", [])
    assert wish is not None
    assert (wish.ingredient, wish.another_adult) == ("Erdnuss", True)
    # The removal said with it is repeated to the guest as before.
    assert removal_with(wish) == "ohne Zwiebeln"
    # For the core only: the search result the model reads does not carry it.
    assert "another_adult" not in wish.model_dump()


def test_another_adult_without_an_ingredient_is_flagged_too():
    wish = classify_wish("mein Mann hat eine Allergie", [])
    assert wish is not None
    assert (wish.kind, wish.ingredient, wish.another_adult) == ("allergy", None, True)


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #368, P1: the subject before the parser's phrase counts.
        "er hat eine Nussallergie",
        "sie ist allergisch gegen Nüsse",
        "unsere Oma verträgt keine Nüsse",
        "die 13 bitte, mein Mann hat eine Nussallergie",
        # Codex PR #368, P1: the "ich" of a later request does not count.
        "er hat eine Nussallergie, ich möchte Besteck dazu",
        "meine Frau hat eine Nussallergie. Ich nehme die 23",
    ],
)
def test_another_adult_named_around_the_phrase_is_flagged(said):
    wish = classify_wish(said, [])
    assert wish is not None and wish.kind == "allergy"
    assert wish.another_adult


@pytest.mark.parametrize(
    "said",
    [
        "ohne Zwiebeln für meine Frau, ich habe eine Erdnussallergie",
        # Without a comma: "und ich" opens the caller's own clause.
        "ohne Zwiebeln für meine Frau und ich habe eine Erdnussallergie",
    ],
)
def test_a_person_named_with_the_removal_only_leaves_the_allergy_the_caller_s(said):
    wish = classify_wish(said, [])
    assert wish is not None
    assert wish.ingredient == "Erdnuss"
    assert not wish.another_adult


@pytest.mark.parametrize(
    "said",
    [
        # #361: "vertrage" counted only with a negation right behind it.
        "Pho Bo, Sesam vertrage ich auch nicht",
        "Sesam vertrage ich leider nicht",
        "Milch vertragen wir gar nicht",
    ],
)
def test_a_negation_behind_the_subject_names_an_intolerance(said):
    """Its ingredient stands before the verb and is not read: the guest is
    asked "Wogegen?" rather than the allergen being lost."""
    assert mentions_allergy(said)
    wish = classify_wish(said, [])
    assert wish is not None
    assert (wish.kind, wish.ingredient) == ("allergy", None)


@pytest.mark.parametrize(
    "said", ["ich vertrage alles", "vertrage ich gut", "das vertrage ich schon"]
)
def test_vertrage_without_a_negation_names_none(said):
    assert not mentions_allergy(said)


@pytest.mark.parametrize(
    ("said", "denies"),
    [
        ("Pho Bo, ich habe keine Allergie.", True),
        ("Ich habe keine Allergie und moechte Pho Bo zum Abholen.", True),
        (
            "Ich habe keine Allergien und Unverträglichkeiten gegen Laktose und "
            "möchte Pho Bo zum Abholen.",
            True,
        ),
        ("Pho Bo, ich habe eine Allergie.", False),
        ("Ich habe keine Nussallergie, aber eine Sesamallergie.", False),
        ("Pho Bo bitte.", False),
    ],
)
def test_only_denies(said, denies):
    assert only_denies(said) is denies


@pytest.mark.parametrize(
    ("answer", "ingredient", "removals"),
    [
        ("Erdnüsse.", "Erdnüsse", []),
        ("Gegen Erdnüsse und Sesam.", "Erdnüsse und Sesam", []),
        ("Keine Erdnuesse", "Erdnuesse", []),
        ("Ich bin gegen Sesam allergisch.", "Sesam", []),
        ("Erdnüsse, aber ohne Zwiebeln.", "Erdnüsse", ["ohne Zwiebeln"]),
        ("Ohne Zwiebeln und gegen Erdnuesse", "Erdnuesse", ["Ohne Zwiebeln"]),
        ("Gegen E 621.", "E 621", []),
        # A hesitation in front is no part of the ingredient (#207).
        ("Äh, Erdnüsse.", "Erdnüsse", []),
        ("Hmm, Sesam und Senf.", "Sesam und Senf", []),
        ("Also, gegen Erdnüsse.", "Erdnüsse", []),
        ("Ähm Erdnuss", "Erdnuss", []),
        ("Gegen, äh, Erdnüsse.", "Erdnüsse", []),
        ("Äh, ohne Zwiebeln, gegen Erdnüsse", "Erdnüsse", ["ohne Zwiebeln"]),
    ],
)
def test_the_answer_to_wogegen_gives_its_ingredient(answer, ingredient, removals):
    read, said = which_answer(answer)
    assert read is not None and read != WITHDRAWN
    assert read.ingredient == ingredient
    assert said == removals


@pytest.mark.parametrize(
    "answer",
    ["Keine.", "Gar keine.", "Gegen nichts.", "Ich habe doch keine Allergie."],
)
def test_an_answer_that_names_nothing_withdraws_the_allergy(answer):
    assert which_answer(answer)[0] == WITHDRAWN


@pytest.mark.parametrize(
    "answer",
    [
        "Weiß ich nicht.",
        "Nein.",
        # Whose and for which dish is not clear (#207, Codex PR #361).
        "Keine, aber ich bin gegen Sesam allergisch.",
        # A sentence of its own is read up to its end, never as an allergy
        # against all of it (T-11.1).
        "Moment, da muss ich kurz überlegen.",
        "Erdnüsse, glaube ich.",
        # Issue #207, P1: an unsure answer was the ingredient on the note.
        "Schwer zu sagen.",
        "Puh, gute Frage.",
        "Das ist schwer zu sagen.",
        "Mal sehen.",
        "Das kommt drauf an.",
        "Gegen vieles.",
        "Alles Mögliche.",
        "Hm.",
        "Äh.",
        "Tja.",
        "Ehm.",
        "Mhm.",
        "Mmh.",
        # A question back, or no ingredient at all.
        "Wie bitte?",
        "Was?",
        "Erdnüsse?",
        "Nee.",
        "Nix.",
        "Unterschiedliches.",
    ],
)
def test_an_answer_without_an_ingredient_leaves_the_question_open(answer):
    assert which_answer(answer)[0] is None


@pytest.mark.parametrize(
    ("answer", "order"),
    [
        ("Eine Cola bitte.", True),
        ("Nummer 12.", True),
        ("Die 23 noch, ich habe keine Allergie.", True),
        ("Erdnüsse.", False),
        ("Vitamin B12", False),
    ],
)
def test_an_order_is_no_answer_to_wogegen(answer, order):
    assert names_an_order(answer, NO_PREFIXES, frozenset({"12", "23"})) is order


@pytest.mark.parametrize(
    ("answer", "only"),
    [
        ("Erdnüsse.", True),
        ("Ich bin gegen Sesam allergisch.", True),
        ("Gegen Erdnüsse und Sesam.", True),
        ("Ich habe doch keine Allergie.", True),
        ("Keine.", True),
        # Codex PR #368, P1: more than the answer goes to the model.
        ("Erdnüsse, aber bitte erst übermorgen.", False),
        ("Erdnüsse und bitte um 20 Uhr.", False),
        # Codex PR #368, P1 4382914460: a time or a day read as part of the
        # ingredient let the held draft go out with the old time.
        ("Erdnüsse, um 20:00.", False),
        ("Erdnüsse, um acht.", False),
        ("Erdnüsse, aber erst morgen.", False),
        ("Gegen E 621.", True),
        ("Erdnüsse, aber ohne Zwiebeln.", False),
        ("Weiß ich nicht.", False),
        # A hesitation says nothing more (#207).
        ("Ähm, Erdnüsse.", True),
        ("Hm.", False),
    ],
)
def test_an_answer_says_only_its_ingredient(answer, only):
    from api.domain.menu.wishes import answers_only

    assert answers_only(answer) is only


@pytest.mark.parametrize(
    "answer", ["Erdnüsse, um 20:00.", "Erdnüsse, um acht.", "Sesam, für vier Personen."]
)
def test_a_time_or_a_number_is_never_part_of_an_ingredient(answer):
    """Codex PR #368: "Erdnüsse, um 20 00" was the ingredient."""
    assert which_answer(answer)[0] is None


@pytest.mark.parametrize(
    ("answer", "code"), [("Gegen E 621.", "E 621"), ("Vitamin B12.", "Vitamin B12")]
)
def test_an_additive_code_is_an_ingredient(answer, code):
    read, _ = which_answer(answer)
    assert read is not None and read != WITHDRAWN
    assert read.ingredient == code
