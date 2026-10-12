"""agent/guest_details.py: a name or phone number is taken only when the guest
said it, or when the number is the caller ID (CLAUDE.md §2 rule 2).

No database: the check reads the conversation state. The way through the loop
is in test_agent_loop.py. The guest sentences are German, the detection only
reads what a guest says on the phone.
"""

import uuid

import pytest

from api.agent.guards import HINT_GUEST_NAME, HINT_GUEST_PHONE, begin_turn, refusal
from api.agent.guest_details import (
    name_said,
    number_known,
    numbers_said,
    same_number,
    words_said,
)
from api.agent.llm import ToolCall
from api.agent.state import apply_state_patch, initial_state, note_guest_details

CALLER = "+4972215551234"


def heard(*sentences, caller=None):
    """A call in which the guest said these sentences."""
    state = initial_state(uuid.uuid4(), uuid.uuid4(), caller_id=caller)
    for sentence in sentences:
        note_guest_details(state, sentence)
    return state


def said(*sentences):
    return set().union(*(words_said(sentence) for sentence in sentences))


def numbers(*sentences):
    return set().union(*(numbers_said(sentence) for sentence in sentences))


# --- The name: every word of it was said, or it was spelled ---


@pytest.mark.parametrize(
    ("sentence", "name"),
    [
        ("Auf den Namen Müller bitte.", "Müller"),
        ("auf müller", "Müller"),
        # Which spelling of an umlaut arrives is chance.
        ("Mueller ist mein Name", "Müller"),
        ("Hier ist Weiß", "Weiss"),
        ("Auf Müller-Lüdenscheidt", "Müller-Lüdenscheidt"),
        ("Anna Müller, guten Tag", "Müller, Anna"),
        ("Auf von der Heide bitte", "von der Heide"),
    ],
)
def test_name_the_guest_said_is_taken(sentence, name):
    assert name_said(name, said(sentence)) is True


def test_first_and_last_name_from_two_sentences_are_one_name():
    words = said("Auf Müller bitte", "Der Vorname ist Anna")

    assert name_said("Anna Müller", words) is True


@pytest.mark.parametrize(
    ("sentence", "name"),
    [
        # Rung 3 of the understanding ladder: the guest spells the name.
        ("M E I E R", "Meier"),
        ("Das schreibt man M, A, I, E, R.", "Maier"),
        ("M wie Martha, A wie Anton, I wie Ida, E wie Emil, R wie Richard", "Maier"),
        ("M Ü L L E R", "Müller"),
        ("m ü l l e r", "Mueller"),
    ],
)
def test_name_the_guest_spelled_is_taken(sentence, name):
    assert name_said(name, said(sentence)) is True


@pytest.mark.parametrize(
    "name",
    [
        # Seen on a real model, sent to get draft_order through (07.10.2026).
        "Max Mustermann",
        "Gast",
        "Kunde",
        "Unbekannt",
        # One word said, one added.
        "Anna Müller",
        "Herr Müller",
        # Part of a word, or a word that only begins like one that was said.
        "Müll",
        "Abhol",
        # The word that was said, with something added that is no word: the
        # tool would store it as written (Codex PR #244, P2).
        "Müller 999",
        "Müller2",
        "Müller!!!",
        "Müller (Stammgast)",
        "Müller_",
        # No name at all.
        "",
        "   ",
        "123",
        "-",
    ],
)
def test_name_nobody_said_is_not_taken(name):
    words = said("Guten Tag, ich möchte etwas zum Abholen bestellen", "Auf Müller")

    assert name_said(name, words) is False


@pytest.mark.parametrize("name", ["O'Brien", "Müller, Anna", "Dr. Müller", " Müller "])
def test_what_stands_between_the_words_of_a_name_is_part_of_it(name):
    words = said("Auf Doktor Anna Müller, Dr. Müller, oder O'Brien")

    assert name_said(name, words) is True


def test_spelled_name_counts_only_within_one_sentence():
    """Two sentences with letters are two things that were spelled."""
    assert name_said("Meier", said("M E I", "E R")) is False


# --- The number: the caller ID, or one run of digits the guest said ---


@pytest.mark.parametrize(
    "written",
    ["+491712345678", "01712345678", "0171 2345678", "0171/234-5678", "00491712345678"],
)
def test_number_the_guest_said_is_known_in_any_spelling(written):
    said_numbers = numbers("Meine Nummer ist 0171 2345678.")

    assert number_known(written, None, said_numbers) is True


@pytest.mark.parametrize("written", [CALLER, "07221 5551234", "07221/555-1234"])
def test_caller_id_is_known_in_any_spelling(written):
    assert number_known(written, CALLER, set()) is True


@pytest.mark.parametrize(
    "written",
    [
        # Seen on a real model: the caller ID came back with digits missing.
        "+497221555123",
        "+49722155512",
        "+4972215551243",
        "+49722155512345",
        # Invented.
        "+4915112345678",
        "0123 4567890",
        # No number at all.
        "unbekannt",
        "",
        "keine",
        # No text: the request schemas take a number as text only.
        None,
        4972215551234,
    ],
)
def test_number_nobody_said_is_not_known(written):
    said_numbers = numbers("Einen Tisch für 4 am 16.09. um 19 Uhr bitte")

    assert number_known(written, CALLER, said_numbers) is False


@pytest.mark.parametrize("written", ["2345678", "0171", "0171 234567", "1712345678"])
def test_part_of_a_number_the_guest_said_is_not_known(written):
    """A number with digits missing reaches someone else."""
    said_numbers = numbers("Meine Nummer ist 0171 2345678.")

    assert number_known(written, None, said_numbers) is False


def test_digits_from_two_places_of_a_sentence_are_two_numbers():
    said_numbers = numbers("Die 23 und zweimal die 47, meine Nummer ist 0171 2345678")

    assert number_known("0171 2345678", None, said_numbers) is True
    assert number_known("23470171 2345678", None, said_numbers) is False
    assert number_known("2347", None, said_numbers) is False


@pytest.mark.parametrize(
    ("sentence", "written"),
    [
        # Codex PR #244, P1: `normalize_phone` makes +16092026 of this date.
        ("Einen Tisch am 16.09.2026 auf Müller", "16.09.2026"),
        ("Einen Tisch am 16.09.2026 auf Müller", "16092026"),
        ("Einen Tisch am 16.09.2026 auf Müller", "+16092026"),
        # A day below ten begins like an area code.
        ("Am 01.10.2026 um 19 Uhr", "01.10.2026"),
        ("Am 01.10.2026 um 19 Uhr", "+491102026"),
        ("Am 01/10/2026 bitte", "01/10/2026"),
        ("Am 01-10-2026 bitte", "+491102026"),
        ("Am 2026-10-01 bitte", "2026-10-01"),
        ("Am 2026-10-01 bitte", "+20261001"),
        ("Um 09.30 Uhr für 12 Personen", "09.30"),
    ],
)
def test_date_or_time_the_guest_said_is_no_phone_number(sentence, written):
    assert number_known(written, None, numbers(sentence)) is False


@pytest.mark.parametrize("written", ["5551234", "555 1234", "+5551234"])
def test_number_without_an_area_code_is_no_phone_number(written):
    """`normalize_phone` would store it as an international one. Refused, the
    model asks for the number with its area code."""
    assert number_known(written, None, numbers("Meine Nummer ist 555 1234")) is False


def test_same_number_in_two_spellings():
    assert same_number("07221 5551234", CALLER) is True
    assert same_number("+497221555123", CALLER) is False
    assert same_number("unbekannt", "unbekannt") is False
    assert same_number(None, None) is False


def test_number_too_short_to_be_one_stays_known_as_the_guest_said_it():
    """The tool then says that the number was not understood, in its own
    sentence. Dropped here, the booking would go on under the caller ID while
    the guest believes to have given another number."""
    said_numbers = numbers("Unter 0171 2 bitte")

    assert number_known("0171 2", CALLER, said_numbers) is True
    assert number_known("01712", CALLER, said_numbers) is True
    assert number_known("0171 3", CALLER, said_numbers) is False


# --- The state: what a model writes into the slots ---


def test_state_keeps_the_caller_id_apart_from_the_slot():
    state = heard(caller="07221 5551234")

    assert state.caller_phone == CALLER
    assert state.slots == {"phone": CALLER}


def test_suppressed_caller_id_is_no_number():
    assert heard(caller=None).caller_phone is None
    assert heard(caller="anonymous").caller_phone is None


def test_what_the_guest_said_never_goes_into_the_prompt():
    """It is memory of the core. The model gets the compact state, never the
    words of earlier turns (docs/05 §5)."""
    state = heard("Auf Zacharias, meine Nummer ist 0171 2345678")

    prompt = str(state.to_prompt_json()).lower()

    assert "zacharias" not in prompt
    assert "2345678" not in prompt


def test_patch_with_what_the_guest_said_is_taken():
    state = heard("Auf Müller, und bitte unter 0171 2345678", caller=CALLER)
    patch = {"guest_name": "Müller", "phone": "0171 2345678", "party_size": 4}

    assert apply_state_patch(state, patch) == patch
    assert state.slots == patch


def test_patch_with_an_invented_name_is_dropped():
    state = heard("Einmal die 23 zum Abholen bitte", caller=CALLER)

    applied = apply_state_patch(state, {"guest_name": "Max Mustermann"})

    assert applied == {}
    assert "guest_name" not in state.slots


def test_invented_name_does_not_replace_the_one_the_guest_said():
    state = heard("Auf Müller bitte")
    apply_state_patch(state, {"guest_name": "Müller"})

    apply_state_patch(state, {"guest_name": "Gast"})

    assert state.slots["guest_name"] == "Müller"


@pytest.mark.parametrize("mangled", ["+497221555123", "+4915112345678", "unbekannt"])
def test_patch_never_replaces_the_caller_id_with_a_number_nobody_said(mangled):
    state = heard("Einmal die 23 zum Abholen bitte", caller=CALLER)

    applied = apply_state_patch(state, {"phone": mangled, "party_size": 2})

    assert applied == {"party_size": 2}
    assert state.slots["phone"] == CALLER


def test_patch_takes_another_number_the_guest_names():
    """docs/05 §3: a guest who names another number than the one they call
    from is reached under that one."""
    state = heard("Bitte unter 0171 2345678 zurückrufen", caller=CALLER)

    apply_state_patch(state, {"phone": "+491712345678"})

    assert state.slots["phone"] == "+491712345678"


@pytest.mark.parametrize("name", [None, 4, ["Müller"], {"name": "Müller"}])
def test_name_that_is_no_text_is_dropped(name):
    state = heard("Auf Müller bitte")

    assert apply_state_patch(state, {"guest_name": name}) == {}


# --- The guards: what a model passes to the tools that book ---


def reservation(**details):
    args = {"party_size": 4, "reserved_for": "2026-09-16T19:00:00+02:00", **details}
    return ToolCall("create_reservation", args)


def order(**customer):
    return ToolCall(
        "draft_order", {"type": "pickup", "customer": customer, "items": []}
    )


def callback(phone):
    return ToolCall(
        "create_callback",
        {"phone": phone, "reason": "out_of_scope", "summary": "Frage zur Karte."},
    )


def refused(state, call):
    return refusal(state, begin_turn(state), "", call)


OTHER = "0171 2345678"
NAMES_ANOTHER_NUMBER = "Auf Müller, erreichbar unter 0171 2345678"


@pytest.mark.parametrize(
    "call",
    [
        reservation(guest_name="Müller", phone=CALLER),
        reservation(guest_name="Müller", phone="07221 5551234"),
        order(name="Müller", phone=CALLER),
        callback(CALLER),
    ],
)
def test_call_under_the_number_in_the_state_passes(call):
    state = heard("Auf Müller bitte", caller=CALLER)

    assert refused(state, call) is None


@pytest.mark.parametrize(
    "call",
    [
        reservation(guest_name="Müller", phone=OTHER),
        order(name="Müller", phone="+491712345678"),
        callback("0171/2345678"),
    ],
)
def test_call_under_another_number_the_guest_named_passes(call):
    """The model wrote it into `slots` first, as the answer format asks."""
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)
    apply_state_patch(state, {"phone": OTHER})

    assert refused(state, call) is None


@pytest.mark.parametrize(
    "call",
    [
        reservation(guest_name="Müller", phone=CALLER),
        order(name="Müller", phone=CALLER),
        callback(CALLER),
    ],
)
def test_call_under_the_caller_id_after_the_guest_named_another_is_refused(call):
    """Codex PR #244, P2: the model puts the new number into `slots` and books
    under the number the guest calls from. Both were known in the call."""
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)
    apply_state_patch(state, {"phone": OTHER})

    assert refused(state, call).hint == HINT_GUEST_PHONE


def test_number_the_guest_named_counts_once_it_stands_in_the_state():
    """The other way round: the number in the call, the old one in `slots`.
    Which of the two the guest wants is not for the core to pick."""
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)

    assert refused(state, order(name="Müller", phone=OTHER)).hint == HINT_GUEST_PHONE


def test_without_a_number_in_the_state_one_the_guest_said_passes():
    state = heard(NAMES_ANOTHER_NUMBER)

    assert refused(state, order(name="Müller", phone="+491712345678")) is None


@pytest.mark.parametrize("written", ["16.09.2026", "16092026", "+16092026"])
def test_date_the_guest_said_books_nothing_as_a_phone_number(written):
    """Codex PR #244, P1, with a suppressed caller ID."""
    state = heard("Einen Tisch am 16.09.2026 auf Müller")

    assert apply_state_patch(state, {"phone": written}) == {}
    call = reservation(guest_name="Müller", phone=written)
    assert refused(state, call).hint == HINT_GUEST_PHONE
    assert refused(state, callback(written)).hint == HINT_GUEST_PHONE


@pytest.mark.parametrize(
    "call",
    [
        reservation(guest_name="Max Mustermann", phone=CALLER),
        reservation(guest_name="Gast", phone=CALLER),
        order(name="Max Mustermann", phone=CALLER),
        order(name="", phone=CALLER),
    ],
)
def test_call_with_an_invented_name_is_refused(call):
    state = heard("Einmal die 23 zum Abholen bitte", caller=CALLER)

    result = refused(state, call)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert result.hint == HINT_GUEST_NAME


@pytest.mark.parametrize(
    "call",
    [
        reservation(guest_name="Müller", phone="+4915112345678"),
        reservation(guest_name="Müller", phone="+497221555123"),
        order(name="Müller", phone="+497221555123"),
        order(name="Müller", phone="unbekannt"),
        callback("+4915112345678"),
        callback("+497221555123"),
    ],
)
def test_call_with_a_number_nobody_said_is_refused(call):
    state = heard("Auf Müller bitte", caller=CALLER)

    result = refused(state, call)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "invalid_input"
    assert result.hint == HINT_GUEST_PHONE


def test_number_is_refused_without_a_caller_id_too():
    state = heard("Auf Müller bitte")

    assert refused(state, order(name="Müller", phone=CALLER)).hint == HINT_GUEST_PHONE


@pytest.mark.parametrize(
    "call",
    [
        # A detail that is missing is for the validation in dispatch.py, which
        # names the field.
        reservation(),
        reservation(guest_name="Müller"),
        ToolCall("draft_order", {"type": "pickup", "items": []}),
        ToolCall("draft_order", {"type": "pickup", "customer": "Müller", "items": []}),
        ToolCall("create_callback", {"reason": "out_of_scope"}),
        # Tools that carry no guest detail.
        ToolCall("check_slot", {"party_size": 4}),
        ToolCall("search_menu", {"query": "Müller 0171 2345678"}),
    ],
)
def test_call_without_the_detail_is_left_to_the_validation(call):
    state = heard("Auf Müller bitte")

    assert refused(state, call) is None
