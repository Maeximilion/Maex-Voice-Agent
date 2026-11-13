"""A form of address is no part of the answer (#648).

"Ja, junge Frau" still says yes and "Nein, junger Mann" still says no: the
five forms the agent knows (`escalation.addresses_a_person`) are cut before
the yes and no readers look at the words. Caller sentences in German: the
readers parse German.
"""

import pytest

from api.agent import consent, escalation
from api.agent.consent import (
    bare_answer,
    bare_recording_answer,
    consent_answer,
    is_yes,
    recording_answer,
)

FORMS = ["junge Frau", "junger Mann", "Fräulein", "gute Frau", "guter Mann"]


@pytest.mark.parametrize("form", FORMS)
def test_every_form_is_one_the_escalation_reads_too(form):
    """One list in two files: the truthful sentence is said for exactly the
    forms the readers cut."""
    assert escalation.addresses_a_person(form)
    assert consent._ADDRESS.search(form.lower())


@pytest.mark.parametrize(
    "text", ["Frau Müller", "eine junge Dame", "ein junger Hund", "meine Frau"]
)
def test_no_other_phrase_is_an_address(text):
    assert not escalation.addresses_a_person(text)
    assert not consent._ADDRESS.search(text.lower())


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(
    "said", ["Ja, {}", "Ja gerne, {}.", "{}, ja bitte", "Ja, das passt so, {}"]
)
def test_a_yes_with_an_address_is_a_yes(said, form):
    text = said.format(form)
    assert is_yes(text)
    assert consent_answer(text) == "yes"
    assert bare_answer(text)
    assert recording_answer(text) == "yes"
    assert bare_recording_answer(text)


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("said", ["Nein, {}", "Nein danke, {}.", "{}, lieber nicht"])
def test_a_no_with_an_address_is_a_no(said, form):
    text = said.format(form)
    assert not is_yes(text)
    assert consent_answer(text) == "no"
    assert bare_answer(text)
    assert recording_answer(text) == "no"
    assert bare_recording_answer(text)


@pytest.mark.parametrize(
    "text",
    ["JA, JUNGER MANN", "Ja, Fraeulein", "Ja, junge\u00a0Frau", "Ja, Fra\u0308ulein"],
)
def test_other_spellings_of_the_form_are_cut_too(text):
    assert is_yes(text)
    assert consent_answer(text) == "yes"


@pytest.mark.parametrize(
    "text",
    [
        "Junge Frau",  # an address alone answers nothing
        "Danke, junge Frau",
        "Ja, junge Frau, und noch eine Cola",  # more than the yes
        "Ja, junger Mann, 2",  # a digit changes a quantity
        "Ja, junge Frau, ist das richtig?",  # a question
        "Ja, Frau Müller",  # a name is no form of address
    ],
)
def test_the_cut_makes_no_yes_out_of_more_than_a_yes(text):
    assert not is_yes(text)
    assert consent_answer(text) != "yes"
    assert not bare_answer(text)
    assert recording_answer(text) != "yes"


def test_a_question_about_the_recording_stays_a_question_behind_the_address():
    assert recording_answer("Bin ich damit einverstanden, junge Frau") == "unclear"
