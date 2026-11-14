"""What the parser reads of the guest's allergy (T-11.1, D24).

The menu search splits an allergy said behind a dish off as its wish, and the
stand-in model reads the answer to its own "Wogegen?" with `which_answer`.
Since 15.11.2026 the model writes the allergy note; the core no longer reads
whose allergy it is (Maxi). Pure functions, no database. Caller sentences in
German: the parser reads German.
"""

import pytest

from api.domain.menu.numberwords import NO_PREFIXES
from api.domain.menu.wishes import (
    WITHDRAWN,
    classify_wish,
    mentions_allergy,
    names_an_order,
    only_denies,
    removal_with,
    which_answer,
)

REASONED = "WICHTIG !!! Allergie: Erdnuss"


def test_the_caller_s_own_allergy_is_read_with_its_ingredient():
    wish = classify_wish("ich habe eine Erdnussallergie", [])
    assert wish is not None
    assert (wish.kind, wish.ingredient, wish.text) == ("allergy", "Erdnuss", REASONED)


def test_another_person_s_allergy_keeps_the_removal_said_with_it():
    wish = classify_wish("ohne Zwiebeln, meine Frau hat eine Erdnussallergie", [])
    assert wish is not None
    assert wish.ingredient == "Erdnuss"
    # The removal said with it is repeated to the guest as before.
    assert removal_with(wish) == "ohne Zwiebeln"


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
