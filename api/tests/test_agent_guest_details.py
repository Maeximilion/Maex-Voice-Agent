"""agent/guest_details.py: a name or phone number is taken only when the guest
said it, or when the number is the caller ID (CLAUDE.md §2 rule 2).

No database: the check reads the conversation state. The way through the loop
is in test_agent_loop.py. The guest sentences are German, the detection only
reads what a guest says on the phone.
"""

import time
import uuid

import pytest

from api.agent.guards import (
    HINT_CALLBACK_PHONE,
    HINT_GUEST_NAME,
    HINT_GUEST_PHONE,
    begin_turn,
    note_said,
    refusal,
)
from api.agent.guest_details import (
    name_said,
    names_a_phone_number,
    number_known,
    numbers_said,
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
    """The words of a call as the state keeps them (`note_guest_details`)."""
    words = {}
    for sentence in sentences:
        for word, count in words_said(sentence).items():
            words[word] = max(words.get(word, 0), count)
    return words


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
        # Separators where no two words meet (Codex PR #244, P2).
        "---Müller---",
        "-Müller",
        "Müller-",
        "Müller,,",
        "'Müller'",
        "Müller--Müller",
        # Blanks the tools would store as written (the review after it).
        "Müller\n\n  Müller",
        "Müller\nMüller",
        "Müller\tMüller",
        "Müller  Müller",
        # Said once, written twice (Codex PR #244, P2).
        "Müller Müller",
        "Müller, Müller",
        "Müller-Müller",
        # A period that ends nothing but the name (Codex PR #244, P2).
        "Müller.",
        "Auf Müller.",
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


def test_name_with_a_typographic_apostrophe_is_taken():
    """Codex PR #244, P2: refused on every try, the guest could not get out of
    it by saying the name again."""
    typographic = "O\N{RIGHT SINGLE QUOTATION MARK}Brien"
    words = said(f"Auf {typographic} bitte")

    assert name_said(typographic, words) is True
    assert name_said("O'Brien", words) is True
    assert (
        name_said(
            "O\N{RIGHT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}Brien",
            words,
        )
        is False
    )


def test_letter_that_is_no_part_of_the_spelling_is_not_joined_to_it():
    """Codex PR #244, P2: the a of the card number 23a made "ameier" of the
    name, so "Meier" was refused and "Ameier" taken."""
    for sentence in (
        "Ich nehme die 23a. Mein Name ist M E I E R.",
        "Einmal die 23 a bitte, auf M E I E R",
        "S 12 und auf den Namen M, E, I, E, R",
        # The review after it: a blank or an apostrophe instead of the digit.
        "Ich nehme die 23 a. M E I E R",
        "Die 23 a, M E I E R",
        "Die 23 a M E I E R",
        "Ich bin's, M E I E R",
        "Ich buchstabier's: M E I E R",
        "So schreibt man's: M-E-I-E-R",
    ):
        assert name_said("Meier", said(sentence)) is True
        assert name_said("Ameier", said(sentence)) is False
        assert name_said("Smeier", said(sentence)) is False


@pytest.mark.parametrize(
    "sentence",
    [
        # Review of 31a3a81: periods between the letters.
        "M.E.I.E.R.",
        "M. E. I. E. R.",
        "M wie Martha. E wie Emil. I wie Ida. E wie Emil. R wie Richard.",
        # Digits next to the row are no part of it and take no letter away.
        "M E I E R 19 Uhr",
        "M E I E R 4 Personen",
        "Für 4 M E I E R",
        "Tisch 5, M E I E R",
    ],
)
def test_spelled_name_keeps_its_letters(sentence):
    assert name_said("Meier", said(sentence)) is True


def test_two_names_spelled_in_one_sentence_are_two():
    words = said("Vorname A N N A und Nachname M E I E R")

    assert name_said("Anna Meier", words) is True
    assert name_said("Annameier", words) is False


def test_word_counts_as_often_as_one_sentence_held_it():
    """Codex PR #244, P2. A word the guest said in two turns is still said
    once: repeating a name is no name with a repetition."""
    assert name_said("Li Li", said("Auf Li Li bitte")) is True
    assert name_said("Li Li", said("Auf Li", "Li, wie gesagt")) is False
    assert name_said("Li Li Li", said("Auf Li Li bitte")) is False


def test_letter_with_a_mark_of_its_own_is_the_same_letter():
    """Codex PR #244, P2: an "ü" that arrives as u plus a combining mark cut
    the name in two, and the guest could repeat it without end."""
    decomposed = "Mu\N{COMBINING DIAERESIS}ller"

    assert name_said("Müller", said(f"Auf {decomposed} bitte")) is True
    assert name_said(decomposed, said("Auf Müller bitte")) is True
    assert name_said(decomposed, said(f"Auf {decomposed} bitte")) is True


def test_spelled_name_counts_only_within_one_sentence():
    """Two sentences with letters are two things that were spelled."""
    assert name_said("Meier", said("M E I", "E R")) is False


# --- The number: the caller ID, or one run of digits the guest said ---


@pytest.mark.parametrize(
    "written",
    [
        "+491712345678",
        "01712345678",
        "0171 2345678",
        "0171/234-5678",
        "00491712345678",
        "(0171) 2345678",
    ],
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


@pytest.mark.parametrize(
    "sentence",
    [
        "Einen Tisch am 01 10 2026 auf Müller",
        "Am 1 10 2026 bitte",
        "Am 01 10 26",
        "Tisch für vier am 09 12 2026",
    ],
)
def test_date_written_with_blanks_is_no_phone_number(sentence):
    """Codex PR #244, P1: without the periods "01 10 2026" passed the date
    check and became +491102026."""
    said_numbers = numbers(sentence)

    assert said_numbers == set()
    for written in ("01 10 2026", "01102026", "+491102026", "01 10 26", "+4911026"):
        assert number_known(written, None, said_numbers) is False


def test_trunk_zero_in_brackets_keeps_the_country_code():
    """Review of 31a3a81: read at the bracket as two, the tail of the Austrian
    number became a German one."""
    assert numbers("Erreichbar unter +43 (0)664 1234567") == {"+436641234567"}
    assert number_known("+496641234567", None, numbers("+43 (0)664 1234567")) is False


def test_three_groups_that_are_no_date_stay_a_number():
    """There is no month 21: a number someone writes in groups of two."""
    assert numbers("Unter 07 21 5551 erreichbar") == {"+497215551"}


@pytest.mark.parametrize("written", ["5551234", "555 1234", "+5551234"])
def test_number_without_an_area_code_is_no_phone_number(written):
    """`normalize_phone` would store it as an international one. Refused, the
    model asks for the number with its area code."""
    assert number_known(written, None, numbers("Meine Nummer ist 555 1234")) is False


@pytest.mark.parametrize(
    "sentence",
    [
        "Meine Nummer ist 0171 2345678, bitte.",
        "0171 2345678 - danke",
        "Unter 0171/234-5678 erreichbar",
        "Erreichbar unter (0171) 2345678",
        "Tel.: (0171) 234 56 78, und wir sind zu viert",
        "+49 (0)171 2345678",
        # Digits before the number are other digits: a count, a date, a dish.
        "Am 16.09. 0171 2345678 bitte",
        "Für 4, 0171 2345678",
        "Tisch für 4 am 16.09. um 19 Uhr, meine Nummer ist 0171 2345678",
        "Die 23 bitte, auf Müller, 0171 2345678",
        # A date with blanks and a full year is no head either (Codex PR #244, P2).
        "Am 01 10 2026, meine Nummer ist 0171 2345678",
        # A hyphen or a slash inside the number, with enough digits behind it.
        "0171-2345678",
        "0171/234-5678",
        # An hour with a leading zero is no head of a number (review of 8835fa3).
        "Morgen um 09:30 Uhr, erreichbar unter 0171 2345678",
        "Um 08 Uhr, meine Nummer ist 0171 2345678",
        "Am 05.10. um 08:00 Uhr auf Müller, 0171 2345678",
        "Um 00:30 Uhr, 0171 2345678",
        # The trunk zero in brackets belongs to the number it stands in.
        "+49 (0) 171 2345678",
        "0049 (0)171 2345678",
    ],
)
def test_last_digits_of_a_turn_are_the_number(sentence):
    assert numbers(sentence) == {"+491712345678"}


@pytest.mark.parametrize(
    "sentence",
    [
        # Read as one, a number nobody said (Codex PR #244, P1).
        "Meine Nummer ist 0171 2345678 - 19 Uhr passt",
        "Meine Nummer ist 0171 2345678. 19 Uhr passt.",
        "0171 2345678, 4 Personen",
        "Für 4. 0171 2345678. 2 Kinder sind dabei.",
        "Erreichbar unter 0171 2345678 / 4 Personen",
        "0171 2345678 (19 Uhr)",
        # Read in parts, a number with digits missing (the review after it).
        "Meine Nummer ist 0171 234. 5678",
        "0171 234... 5678",
        "0171 234 \u2013 5678",
        "0171 234 \u2014 5678",
        "0171 234 äh 5678",
        "0721 555 und dann 1234",
        "0171 - 234 - 5678",
        "0171 234 - 5678",
        "0171 234 -5678",
        "0721 555, 1234",
        "Unter 0721 - 5551234 bitte",
        "Unter 0721 / 5551234 bitte",
        # A date written with blanks, with other digits behind it.
        "Am 01 10 2026 um 19 Uhr",
        # The last digits are a valid number, and only the tail of the one
        # the guest said (review of 31a3a81).
        "Meine Nummer ist 0171 20, 034567",
        "0171 20 - 034567",
        "0171 20. 034567",
        "0171 20 äh 034567",
        "0043, 0664 1234567",
        "0033, 06 12 34 56 78",
        "49 171 20 äh 034567",
        "+43, 0664 1234567",
        # Said in pairs, the first half looks like a date (review of 8835fa3).
        "Meine Nummer ist 02 11 55, 01 23 45",
        "03 01 23, 04 56 78",
        # One or two digits behind a hyphen, a slash or a bracket, after a
        # complete number: an extension or an hour (Codex PR #244, P1).
        "Meine Nummer ist 0171 2345678-19 Uhr passt",
        "0171 2345678/19 Uhr",
        "(Tel. 0171 2345678) 19 Uhr",
        "0721 5551234-0",
        # Two numbers: which of them is meant is not for the core to pick.
        "Die alte war 0172 3456789, die neue ist 0171 2345678",
        "0172 3456789 - 0171 2345678",
    ],
)
def test_digits_followed_by_more_digits_are_no_number(sentence):
    """The number may go on behind them, or it ended and they are something
    else: the digits do not say which. Neither reading is known, nor is the
    number that may have been meant; the guest is asked again."""
    said_numbers = numbers(sentence)

    assert said_numbers == set()
    for written in (
        "+49171234567819",
        "+4917123456784",
        "+49171234",
        "+49721555",
        "+491102026",
        "+491712345678",
        "+497215551234",
        "+4934567",
        "+496641234567",
        "+49612345678",
        "+4912345",
        "+4945678",
    ):
        assert number_known(written, None, said_numbers) is False


@pytest.mark.parametrize("written", ["0", "07", "0171", "0171 2", "0" * 30])
def test_run_too_short_or_too_long_to_be_a_phone_number_is_none(written):
    """Own review of PR #244: kept by its digits, "um 0 Uhr" was a number a
    model could put into `slots` in the place of the caller ID."""
    said_numbers = numbers(f"Um 0 Uhr, die 07, unter 0171 2, oder {written}")

    assert said_numbers == set()
    assert number_known(written, CALLER, said_numbers) is False


def test_patterns_on_guest_text_stay_linear():
    """Guest text has no length limit on the text phone. The same inputs as
    for the menu patterns (test_domain_menu_regex_cost.py)."""
    started = time.perf_counter()
    for text in (
        " " * 20_000,
        "1" * 20_000,
        "+" * 20_000,
        "a" * 20_000,
        "1 a" * 10_000,
        "+1(" * 10_000,
        "1. " * 10_000,
        "1 - " * 5_000,
        "1" + " " * 20_000 + "-",
        "1, " * 10_000,
        "a, " * 10_000,
        "a's " * 10_000,
        "a " * 20_000,
    ):
        words_said(text)
        numbers_said(text)
        names_a_phone_number(text)
        name_said(text, {})

    assert time.perf_counter() - started < 2


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
    state = heard("Auf Zacharias, zu meiner alten Bestellung 0171 2345678")

    prompt = str(state.to_prompt_json()).lower()

    assert "zacharias" not in prompt
    assert "2345678" not in prompt
    assert not {"said_words", "said_numbers", "caller_phone", "asked_for_phone"} & set(
        state.to_prompt_json()
    )


def test_patch_with_what_the_guest_said_is_taken():
    state = heard("Auf Müller, und bitte unter 0171 2345678", caller=CALLER)
    patch = {"guest_name": "Müller", "phone": "0171 2345678", "party_size": 4}
    taken = {**patch, "phone": "+491712345678"}

    assert apply_state_patch(state, patch) == taken
    assert state.slots == taken


@pytest.mark.parametrize("written", ["0", "07", "0171 2"])
def test_short_run_the_guest_said_never_replaces_the_caller_id(written):
    state = heard("Um 0 Uhr die 07, erreichbar unter 0171 2", caller=CALLER)

    assert apply_state_patch(state, {"phone": written}) == {}
    assert state.slots["phone"] == CALLER


def test_name_is_kept_without_the_blanks_around_it():
    state = heard("Auf Müller bitte")

    assert apply_state_patch(state, {"guest_name": "  Müller\n"}) == {
        "guest_name": "Müller"
    }
    assert state.slots == {"guest_name": "Müller"}


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


def test_number_the_guest_gave_stands_in_the_state_without_the_model():
    """Codex PR #244, P2: the guest names another number, the model leaves
    `phone` out of its answer and books under the caller ID. The core wrote
    the number down itself, so that call is refused."""
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)

    assert state.slots["phone"] == "+491712345678"
    assert refused(state, order(name="Müller", phone=OTHER)) is None
    assert refused(state, order(name="Müller", phone=CALLER)).hint == HINT_GUEST_PHONE
    assert refused(state, callback(CALLER)).hint == HINT_GUEST_PHONE


@pytest.mark.parametrize(
    ("draft", "intent"),
    [("reservation_id", "reservation"), ("order_id", "pickup")],
)
def test_number_changed_after_the_readback_drops_the_draft(draft, intent):
    """The draft was made under the caller ID and read out without it. A yes
    in a later turn must not book it under the number the guest replaced."""
    state = heard("Auf Müller bitte", caller=CALLER)
    state.stage, state.intent = "readback_pending", intent
    setattr(state, draft, uuid.uuid4())

    note_guest_details(state, "Ach, meine Nummer ist jetzt 0171 2345678")

    assert state.slots["phone"] == "+491712345678"
    assert (state.stage, state.intent) == ("collecting", intent)
    assert state.reservation_id is None and state.order_id is None
    assert begin_turn(state).readback_id is None


def test_same_number_after_the_readback_keeps_the_draft():
    state = heard("Auf Müller bitte", caller=CALLER)
    state.stage, state.reservation_id = "readback_pending", uuid.uuid4()

    note_guest_details(state, "Meine Nummer ist die 07221 5551234, richtig?")
    apply_state_patch(state, {"phone": CALLER})

    assert state.stage == "readback_pending"


def test_model_that_changes_the_number_after_the_readback_drops_the_draft():
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)
    state.stage, state.reservation_id = "readback_pending", uuid.uuid4()

    apply_state_patch(state, {"phone": CALLER})

    assert state.slots["phone"] == CALLER
    assert state.stage == "collecting" and state.reservation_id is None


def test_model_may_go_back_to_the_number_the_guest_calls_from():
    """ "Nehmen Sie doch die Nummer, von der ich anrufe" names no digits: that
    reading is the model's, and the caller ID is a number the core knows."""
    state = heard(NAMES_ANOTHER_NUMBER, caller=CALLER)

    assert apply_state_patch(state, {"phone": CALLER}) == {"phone": CALLER}
    assert refused(state, order(name="Müller", phone=CALLER)) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "Meine Nummer ist 0171 2345678",
        "Sie erreichen mich unter 0171 2345678",
        "Bitte unter 0171 2345678",
        "Handy 0171 2345678",
        "Tel. 0171 2345678",
        "Bitte um Rückruf: 0171 2345678",
        "Können Sie mich zurückrufen? 0171 2345678",
        "Telefonisch am besten 0171 2345678",
    ],
)
def test_number_the_guest_names_as_one_to_call_is_taken(sentence):
    state = heard(sentence, caller=CALLER)

    assert names_a_phone_number(sentence) is True
    assert state.said_numbers == {"+491712345678"}
    assert state.slots["phone"] == "+491712345678"


@pytest.mark.parametrize(
    "sentence",
    [
        # Codex PR #244, P1: a reference that looks like a phone number.
        "Zu meiner alten Bestellung 0171 2345678 habe ich eine Frage",
        "Die Bestellnummer war 0171 2345678",
        "Kundennummer 0171 2345678",
        "Ich rufe an wegen meiner Bestellung 0171 2345678",
        "Auf Müller, 0171 2345678",
        "0171 2345678",
    ],
)
def test_digits_nobody_called_a_phone_number_are_none(sentence):
    state = heard(sentence, caller=CALLER)

    assert names_a_phone_number(sentence) is False
    assert state.said_numbers == set()
    assert state.slots["phone"] == CALLER
    assert apply_state_patch(state, {"phone": "0171 2345678"}) == {}
    assert refused(state, callback("+491712345678")).hint == HINT_GUEST_PHONE


def test_answer_to_the_question_for_the_number_needs_no_word_for_it():
    """The agent asked, the guest answers with the digits alone."""
    state = heard("Auf Müller, 0171 2345678")
    assert state.said_numbers == set()

    note_said(state, "Unter welcher Telefonnummer sind Sie erreichbar?")
    note_guest_details(state, "0171 2345678")

    assert state.slots["phone"] == "+491712345678"


def test_question_for_something_else_makes_no_digits_a_number():
    state = heard("Guten Tag")

    note_said(state, "Auf welchen Namen darf ich reservieren?")
    note_guest_details(state, "Müller, 0171 2345678")

    assert "phone" not in state.slots


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
    # A callback under a value that is no number counts as one without a
    # number: refused, and the loop tries the team first (Codex PR #347).
    assert refused(state, callback(written)).hint in (
        HINT_GUEST_PHONE,
        HINT_CALLBACK_PHONE,
    )


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
        # A name or a customer that is missing is for the validation in
        # dispatch.py, which names the field. A missing number gets the phone
        # hint (test_agent_guards.py): the validation's text alone kept a real
        # model sending the same call without one.
        ToolCall("draft_order", {"type": "pickup", "items": []}),
        ToolCall("draft_order", {"type": "pickup", "customer": "Müller", "items": []}),
        # Tools that carry no guest detail.
        ToolCall("check_slot", {"party_size": 4}),
        ToolCall("search_menu", {"query": "Müller 0171 2345678"}),
    ],
)
def test_call_without_the_detail_is_left_to_the_validation(call):
    state = heard("Auf Müller bitte")

    assert refused(state, call) is None


# --- The open threads of PR #244, replayed on main 828e6ee ---


@pytest.mark.parametrize(
    "say",
    [
        # The menu's own questions, word for word.
        "Das habe ich auf der Karte nicht gefunden. Können Sie mir die Nummer sagen?",
        "Die Nummer 23 habe ich nicht auf der Karte. Können Sie mir die Nummer sagen?",
        "Welche Nummer meinen Sie? Bitte sagen Sie mir nur die eine Nummer.",
        # Every readback names a card number.
        "Einmal Nummer 13 Pho Bo. Macht 11 Euro 90. Passt das so?",
        "Ihre Nummer 23 ist notiert.",
        "Ihre Nummer s12 ist notiert.",
        "Ihre Nummer S 12 ist notiert.",
        "Nummer 13, 14 und 15 sind notiert.",
        "Soll das unter Nummer 13 stehen?",
    ],
)
def test_a_menu_number_in_the_agents_sentence_asks_for_no_phone_number(say):
    """Codex PR #244, P1: the bare word armed the answer, and a reference the
    guest read out next stood in `slots` as the number to book under."""
    state = initial_state(uuid.uuid4(), uuid.uuid4(), caller_id=CALLER)
    note_said(state, say)
    assert state.asked_for_phone is False

    note_guest_details(
        state, "Zu meiner alten Bestellung 0171 2345678 habe ich eine Frage"
    )

    assert state.slots["phone"] == CALLER
    assert state.said_numbers == set()


@pytest.mark.parametrize(
    "say",
    [
        "Unter welcher Telefonnummer sind Sie erreichbar?",
        "Die Rufnummer brauche ich bitte mit Vorwahl. "
        "Unter welcher Nummer sind Sie erreichbar?",
        "Unter welcher Nummer?",
        "Ihre  Nummer bitte.",
        "Wie lautet Ihre Nummer?",
        "Können Sie mir Ihre Nummer sagen?",
        "Darf ich Ihre Handynummer notieren?",
        "Bitte nennen Sie mir die Nummer mit Vorwahl.",
        # The agent reads a number out: the guest may answer with another.
        "Darf ich Sie unter 0721 5551234 notieren?",
        # Codex PR #452, P2: digits like these stand on no card.
        "Ist Ihre Nummer 0721 5551234 noch aktuell?",
        "Ist die Nummer 030 5551234 richtig?",
        "Ist die Nummer +49 721 5551234 richtig?",
    ],
)
def test_the_agents_question_for_the_number_arms_the_answer(say):
    state = initial_state(uuid.uuid4(), uuid.uuid4(), caller_id=CALLER)
    note_said(state, say)
    assert state.asked_for_phone is True

    note_guest_details(state, "0171 2345678")

    assert state.slots["phone"] == "+491712345678"


@pytest.mark.parametrize(
    "say",
    ["Können Sie die Nummer bitte wiederholen?", "Wie war die Nummer noch mal?"],
)
def test_the_bare_word_goes_on_with_the_question_for_the_number(say):
    """Review of bb3601f: the guest repeats the number the agent did not
    catch, and alone a bare "Nummer" reads like one on the card."""
    state = initial_state(uuid.uuid4(), uuid.uuid4())
    note_said(state, "Unter welcher Telefonnummer sind Sie erreichbar?")
    note_said(state, say)
    assert state.asked_for_phone is True

    note_guest_details(state, "0171 2345678")

    assert state.slots["phone"] == "+491712345678"


def test_a_card_number_ends_the_question_for_the_number():
    state = initial_state(uuid.uuid4(), uuid.uuid4())
    note_said(state, "Unter welcher Telefonnummer sind Sie erreichbar?")

    note_said(state, "Einmal Nummer 13 Pho Bo. Passt das so?")

    assert state.asked_for_phone is False


@pytest.mark.parametrize(
    ("sentence", "kept"),
    [
        # Codex PR #244, P2: the a belongs to the dish, not to the name.
        ("Die 23 a, M E I E R", "A Meier"),
        ("Die 23a, M E I E R", "A Meier"),
        ("Die S 12, M E I E R", "S Meier"),
        ("Ich bin's, Meier", "S Meier"),
    ],
)
def test_a_letter_that_hangs_on_something_else_is_no_word_of_a_name(sentence, kept):
    assert name_said("Meier", said(sentence)) is True
    assert name_said(kept, said(sentence)) is False


@pytest.mark.parametrize(
    ("sentence", "name"),
    [
        # The letter was also said as a word of the name.
        ("A Meier, die 23 a", "A Meier"),
        # Initials are a row of letters, and a name.
        ("Hier J. R. Müller", "J. R. Müller"),
        # Review of bb3601f: the digits behind the letter are no card number.
        ("Hier ist Frau M 0171 2345678", "Frau M"),
        # Codex PR #452, P2: in shorter groups they are a number all the same.
        ("Hier ist Frau M 030 5551234", "Frau M"),
        ("Hier ist Frau M 01 71 23 45 678", "Frau M"),
    ],
)
def test_a_letter_the_guest_said_as_a_name_stays(sentence, name):
    assert name_said(name, said(sentence)) is True


@pytest.mark.parametrize(
    "sentence",
    [
        # Codex PR #244, P2: an hour, in the form that has no separator.
        "Um 0900 Uhr, meine Nummer ist 0171 2345678",
        "um 0830 uhr, meine Nummer ist 0171 2345678",
        "Um 09 00 Uhr, meine Nummer ist 0171 2345678",
        "0000 Uhr passt, meine Nummer ist 0171 2345678",
    ],
)
def test_a_compact_clock_time_is_no_head_of_the_number(sentence):
    assert numbers_said(sentence) == {"+491712345678"}


@pytest.mark.parametrize(
    "sentence",
    [
        # Without "Uhr" the four digits may still be the head.
        "0900, meine Nummer ist 0171 2345678",
        # Behind the number the hour is what stands last.
        "Meine Nummer ist 0171 2345678, um 0900 Uhr",
        # Digits in front of it make it the tail of a number.
        "0171 234 0900 Uhr",
    ],
)
def test_four_digits_that_are_no_lone_clock_time_keep_the_number_out(sentence):
    assert numbers_said(sentence) == set()
