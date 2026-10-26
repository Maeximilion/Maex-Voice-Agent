"""The allergy note and the caller's consent (T-11.1, legal check L13, D24).

The kitchen note keeps the fixed wording "WICHTIG: Keine <Zutat>. Grund:
Allergie" only with the caller's explicit "ja"; without it only "ohne <Zutat>"
is stored. The caller's "ja" counts for themselves and their own child, never
for another adult: that allergy is a plain removal from the start. Pure
functions, no database. Caller sentences in German: the parser reads German.
"""

import pytest

from api.domain.menu.wishes import (
    asks_consent,
    classify_wish,
    of_another_adult,
    removal_with,
    unread_allergy,
    with_consent,
    without_reason,
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


def test_the_caller_s_own_allergy_keeps_the_fixed_wording():
    wish = classify_wish("ich habe eine Erdnussallergie", [])
    assert wish is not None
    assert (wish.kind, wish.ingredient, wish.text) == ("allergy", "Erdnuss", REASONED)


def test_the_own_child_s_allergy_keeps_the_fixed_wording():
    wish = classify_wish("meine Tochter hat eine Erdnussallergie", [])
    assert wish is not None
    assert wish.text == REASONED


def test_another_adult_s_allergy_is_a_plain_removal():
    wish = classify_wish("meine Frau hat eine Erdnussallergie", [])
    assert wish is not None
    assert (wish.kind, wish.ingredient, wish.text) == (
        "allergy",
        "Erdnuss",
        "ohne Erdnuss",
    )
    # Repeated whole to the guest ("Nummer 13 Pho Bo, ohne Erdnuss").
    assert removal_with(wish) == "ohne Erdnuss"


def test_another_adult_s_allergy_keeps_the_removal_said_with_it():
    wish = classify_wish("ohne Zwiebeln, meine Frau hat eine Erdnussallergie", [])
    assert wish is not None
    assert wish.text == "ohne Erdnuss, ohne Zwiebeln"


def test_a_person_named_with_the_removal_only_leaves_the_allergy_the_caller_s():
    wish = classify_wish(
        "ohne Zwiebeln für meine Frau, ich habe eine Erdnussallergie", []
    )
    assert wish is not None
    assert wish.text == f"{REASONED}. Ohne Zwiebeln für meine Frau"


def test_with_consent_the_fixed_wording_stays():
    assert with_consent(REASONED, consented=True) == REASONED
    assert with_consent(f"{REASONED}. Ohne Zwiebeln", consented=True) == (
        f"{REASONED}. Ohne Zwiebeln"
    )


def test_without_consent_only_the_removal_is_stored():
    assert with_consent(REASONED, consented=False) == "ohne Erdnuss"
    assert with_consent(f"{REASONED}. Ohne Zwiebeln", consented=False) == (
        "ohne Erdnuss, ohne Zwiebeln"
    )


@pytest.mark.parametrize(
    ("note", "consented", "stored"),
    [
        # As the guest said it: brought into one of the two forms.
        ("Erdnussallergie", True, REASONED),
        ("Erdnussallergie", False, "ohne Erdnuss"),
        ("Tochter hat eine Erdnussallergie", True, REASONED),
        # Another adult's: never with the reason, also after a "ja".
        ("meine Frau hat eine Erdnussallergie", True, "ohne Erdnuss"),
        # No ingredient: nothing for the kitchen to leave out, nothing stored.
        ("Allergie", True, None),
        ("ich habe eine Allergie", False, None),
    ],
)
def test_a_free_allergy_note_takes_one_of_the_two_forms(note, consented, stored):
    assert with_consent(note, consented) == stored


@pytest.mark.parametrize(
    "note",
    [
        "ohne Koriander",
        "mit Hochstuhl",
        # A denial names no allergy of the caller.
        "keine Allergie",
    ],
)
def test_a_note_without_an_allergy_stays_as_it_is(note):
    assert with_consent(note, consented=False) == note


def test_the_fixed_wording_without_an_ingredient_carries_no_reason():
    """A broken copy of the wording names nothing to leave out: whatever is
    left of it, the allergy is not stored."""
    broken = "WICHTIG: Keine . Grund: Allergie"
    assert not asks_consent(broken)
    assert "Allergie" not in (with_consent(broken, consented=True) or "")


@pytest.mark.parametrize(
    ("note", "asks"),
    [
        (REASONED, True),
        (f"{REASONED}. Ohne Zwiebeln", True),
        ("Erdnussallergie", True),
        ("meine Tochter verträgt keine Milch", True),
        ("meine Frau hat eine Erdnussallergie", False),
        ("ohne Erdnuss", False),
        ("Allergie", False),
        ("keine Allergie", False),
    ],
)
def test_only_a_note_that_would_carry_the_reason_asks_for_consent(note, asks):
    assert asks_consent(note) is asks


SESAME = "WICHTIG: Keine Sesam. Grund: Allergie"


@pytest.mark.parametrize(
    ("note", "consented", "stored"),
    [
        # Codex PR #368, P1: two notices in one note keep both ingredients.
        (f"{REASONED}, {SESAME}", True, f"{REASONED}. {SESAME}"),
        (f"{REASONED}, {SESAME}", False, "ohne Erdnuss, ohne Sesam"),
        (
            f"{REASONED}. Ohne Zwiebeln. {SESAME}",
            False,
            ("ohne Erdnuss, ohne Zwiebeln, ohne Sesam"),
        ),
        # A notice next to the guest's own words.
        (f"{REASONED}, Sesamallergie", True, f"{REASONED}. {SESAME}"),
        (f"Sesamallergie, {REASONED}", False, "ohne Sesam, ohne Erdnuss"),
    ],
)
def test_every_allergen_of_a_note_is_kept(note, consented, stored):
    assert with_consent(note, consented) == stored


@pytest.mark.parametrize(
    "said",
    [
        # Codex PR #368, P1: the subject before the parser's phrase counts.
        "er hat eine Nussallergie",
        "sie ist allergisch gegen Nüsse",
        "unsere Oma verträgt keine Nüsse",
        "die 13 bitte, mein Mann hat eine Nussallergie",
    ],
)
def test_another_adult_named_before_the_phrase_gets_no_reason(said):
    wish = classify_wish(said, [])
    assert wish is not None
    assert wish.kind == "allergy"
    assert wish.text.startswith("ohne ")
    assert "Grund" not in wish.text


def test_a_clause_with_its_own_subject_is_read_alone():
    """Without a comma: "und ich" opens the caller's own clause."""
    wish = classify_wish(
        "ohne Zwiebeln für meine Frau und ich habe eine Erdnussallergie", []
    )
    assert wish is not None
    assert wish.text.startswith(REASONED)


def test_without_reason_lowers_the_removal():
    assert without_reason("Erdnuss", "Keine Zwiebeln") == "ohne Erdnuss, keine Zwiebeln"
    assert without_reason("Erdnuss", None) == "ohne Erdnuss"


@pytest.mark.parametrize(
    "note",
    [
        # Codex PR #368 (P1): more follows than the parser reads (#361).
        "Ich bin gegen Erdnüsse allergisch und gegen Sesam",
        "Erdnussallergie und Sesam auch nicht",
        # The removal is read, the allergy behind it is not: only "ohne
        # Zwiebeln" would be stored and both allergens lost.
        "ohne Zwiebeln, gegen Erdnüsse allergisch und gegen Sesam",
        "Ich habe eine Allergie",
    ],
)
def test_an_allergy_without_a_readable_ingredient_is_unread(note):
    assert unread_allergy(note)


@pytest.mark.parametrize(
    "note",
    [
        "Erdnussallergie",
        "Nussallergie, ohne Zwiebeln",
        "WICHTIG: Keine Erdnuss. Grund: Allergie",
        "meine Frau hat eine Nussallergie",
        "Allergien habe ich keine",
        "ohne Zwiebeln",
    ],
)
def test_a_read_allergy_or_none_is_not_unread(note):
    assert not unread_allergy(note)


@pytest.mark.parametrize(
    ("note", "consented", "stored"),
    [
        # Codex PR #368: the cutlery request was dropped with the rewording.
        (
            "Erdnussallergie. Bitte Besteck dazu",
            True,
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Bitte Besteck dazu",
        ),
        (
            "Erdnussallergie. Bitte Besteck dazu",
            False,
            "ohne Erdnuss, bitte Besteck dazu",
        ),
        (
            "Bitte Besteck dazu. Erdnussallergie",
            True,
            "WICHTIG: Keine Erdnuss. Grund: Allergie. Bitte Besteck dazu",
        ),
        (
            "Erdnussallergie, bitte Besteck dazu",
            False,
            "ohne Erdnuss, bitte Besteck dazu",
        ),
        ("Erdnussallergie und extra scharf", False, "ohne Erdnuss, extra scharf"),
        # What the rewording keeps already is not written twice.
        (
            "Nussallergie, ohne Zwiebeln",
            True,
            "WICHTIG: Keine Nuss. Grund: Allergie. Ohne Zwiebeln",
        ),
        # An allergen list stays one allergy.
        ("Milch, Ei und Nussallergie", False, "ohne Milch, Ei und Nuss"),
    ],
)
def test_the_rest_of_a_note_survives_the_allergy_wording(note, consented, stored):
    assert with_consent(note, consented=consented) == stored
