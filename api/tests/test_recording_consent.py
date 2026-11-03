"""Consent to the recording of a call (T-11.3, legal check L2, L11, L1, U1).

Opt-out is not enough: the recording starts only after an explicit "ja", and a
"nein", silence or anything else leaves the call going on without one. In mode
`shadow` the AI takes no call, so the voice layer asks before the team rings, and
its first sentence still names the AI. A failing question never costs the call
(rule 5): on any error nothing is recorded and the call goes on. Everything stays
off behind `RECORDING_ENABLED`. Caller sentences in German: the detector reads
German.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.agent import recording
from api.agent.consent import (
    is_yes,
    only_stops_recording,
    recording_answer,
    stops_recording,
)
from api.agent.loop import SAY_NOT_A_PERSON, ConversationLoop
from api.agent.recording import (
    RECORDING_WITHDRAWN,
    SAY_HOW_TO_STOP,
    SAY_RECORDING_STOPPED,
    SAY_WHAT_CAN_I_DO,
)
from api.config import Settings, settings
from api.domain.callbacks.transfer import SUMMARY_HUNG_UP
from api.domain.calls import recordings
from api.models import Callback, ConsentLog, Recording, Reservation, ServiceConfig
from api.telephony import handler as handler_module
from api.telephony.adapters.fake import FakeTelephony
from api.telephony.handler import SAY_TO_THE_TEAM
from api.tests import test_telephony
from api.tests.test_telephony import (
    CLOSED,
    NOW,
    TEAM_PHONE,
    BrokenLLM,
    NoLLM,
    _Wire,
    handler_for,
    scripted,
    set_mode,
    the_call,
)
from sim.scripted_llm import ScriptedLLM

session = test_telephony.session
tenant = test_telephony.tenant

LEGAL_CHECK = Path(__file__).resolve().parents[2] / "docs" / "09_OPERATIONS_LEGAL.md"
SID = "1790000000.7"
A_TABLE = "Guten Tag, ich haette gern einen Tisch fuer vier Personen morgen um 19 Uhr."


@pytest.fixture
def switched_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "recording_enabled", True)
    monkeypatch.setattr(recordings, "FOLDER", tmp_path)
    return tmp_path


def on_the_phone(session, tenant, **more):
    port = FakeTelephony()
    port.open(SID, caller_id="+497215551234")
    handler = handler_for(session, tenant, port, **more)
    handler.on_call_started(SID)
    return port, handler


def answers(session) -> list[str]:
    return [row.answer for row in session.scalars(select(ConsentLog))]


def the_recording(session) -> Recording | None:
    return session.scalars(select(Recording)).one_or_none()


# -- the setting ---------------------------------------------------------------


def test_recording_is_off_unless_it_is_switched_on():
    assert Settings.model_fields["recording_enabled"].default is False


def test_nothing_is_asked_while_the_setting_is_off(session, tenant):
    port, handler = on_the_phone(session, tenant)
    assert "aufzeichnen" not in " ".join(port.says(SID))
    assert port.says(SID)[0].endswith("Was kann ich für Sie tun?")

    handler.on_user_turn(SID, "Ja.")

    assert answers(session) == []
    assert the_recording(session) is None
    assert not port.recordings(SID)


def test_shadow_stays_silent_while_the_setting_is_off(session, tenant):
    set_mode(session, tenant.id, "shadow")
    port, _ = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())
    assert port.says(SID) == []
    assert port.transfers(SID) == [TEAM_PHONE]


# -- the words -----------------------------------------------------------------


def test_the_question_is_the_one_of_the_legal_check():
    """U1, word for word, with the period the deletion job keeps."""
    assert recording.question() in LEGAL_CHECK.read_text(encoding="utf-8")


def test_the_question_names_the_period_that_is_kept(monkeypatch):
    monkeypatch.setattr(settings, "retention_recording_days", 14)
    assert "14 Tage" in recording.question()


@pytest.mark.parametrize(
    "said",
    [
        "Ja",
        "Ja.",
        "Ja, gerne.",
        "Ja, bitte.",
        "Einverstanden.",
        "Ich bin einverstanden.",
        "Ja, ich bin damit einverstanden.",
        "Ja, dürfen Sie.",
        "Ja, das können Sie aufzeichnen.",
        "Klar, ja.",
        "Ja, bin ich.",
    ],
)
def test_an_explicit_yes_is_a_yes(said):
    assert recording_answer(said) == "yes"


@pytest.mark.parametrize(
    "said",
    [
        "Nein",
        "Nein, danke.",
        "Lieber nicht.",
        "Bitte nicht.",
        "Nicht aufzeichnen.",
        "Nein, nicht aufnehmen.",
        "Ich bin nicht einverstanden.",
        "Nicht einverstanden.",
        "Aufnahme stoppen",
        # Review of the branch: refusals that read as unclear before.
        "Nein, das möchte ich nicht.",
        "Nein, auf keinen Fall.",
        "Ne, lieber nicht.",
        "Nein, ich bin dagegen.",
    ],
)
def test_a_no_is_a_no(said):
    assert recording_answer(said) == "no"


@pytest.mark.parametrize(
    "said",
    [
        "Ja?",
        "Wie bitte?",
        "Was haben Sie gesagt",
        "Ist das richtig",
        # The question asked back, its mark dropped by the recognizer.
        "Bin ich damit einverstanden",
        "Bin ich einverstanden",
        "Danke, bin ich damit einverstanden",
        "Ja. Bin ich damit einverstanden",
        "Ja, ich bin nicht einverstanden.",
        "Ja, ich hätte gern einen Tisch.",
        "Nein, ich möchte eine Pizza.",
        "Ja, auf keinen Fall.",
        "Zwei",
        "42",
        "",
        A_TABLE,
    ],
)
def test_anything_else_is_unclear_and_no_consent(said):
    assert recording_answer(said) == "unclear"


@pytest.mark.parametrize(
    "said",
    [
        "Aufnahme stoppen",
        "Aufnahme stoppen!",
        "aufnahme stop",
        "Stoppen Sie die Aufnahme.",
        "Bitte die Aufnahme beenden.",
        "Aufzeichnung löschen.",
        "Aufnahme aus.",
        "Ich will nicht aufgezeichnet werden.",
        "Nehmen Sie das bitte nicht auf, keine Aufnahme.",
        "Aufnahme stoppen, und dann noch eine Cola.",
        "Hören Sie auf, mich aufzunehmen.",
        "Hören Sie auf mich aufzunehmen",
        "Hör auf aufzuzeichnen.",
        "Aufnahmestopp",
        "Aufnahmestopp, bitte.",
        "Aufzeichnungsstopp!",
        "Aufnahme-Stopp",
        # Review of the branch: the verb split in a main clause, plurals, and
        # taking the consent back without naming the recording.
        "Nehmen Sie das bitte nicht auf.",
        "Nehmen Sie mich nicht auf.",
        "Zeichnen Sie das nicht auf.",
        "Keine Aufnahmen bitte.",
        "Aufnahmen stoppen.",
        "Ich bin damit nicht mehr einverstanden.",
        "Ich widerrufe meine Einwilligung.",
        "Ich nehme meine Zustimmung zurück.",
    ],
)
def test_a_withdrawal_is_heard_in_many_wordings(said):
    assert stops_recording(said)


@pytest.mark.parametrize(
    "said",
    [
        "Stoppen Sie bitte.",
        "Die 23.",
        "Nicht die 23.",
        "Ja, passt so.",
        "Die Aufnahme?",
        "Hören Sie, ich hätte gern einen Tisch.",
        "Hören Sie auf, die 23 bitte.",
        "Die Aufnahmen?",
        "Nehmen Sie bitte meine Bestellung auf.",
        "Nehmen Sie meine Bestellung auf, ohne Zwiebeln.",
        "Ich bin damit einverstanden.",
    ],
)
def test_a_sentence_without_both_halves_is_no_withdrawal(said):
    assert not stops_recording(said)


def test_a_stop_with_more_in_it_is_not_only_a_stop():
    assert only_stops_recording("Bitte die Aufnahme stoppen.")
    assert only_stops_recording("Hören Sie auf, mich aufzunehmen.")
    assert only_stops_recording("Aufnahmestopp.")
    assert only_stops_recording("Nehmen Sie das bitte nicht auf.")
    assert only_stops_recording("Ich widerrufe meine Einwilligung.")
    assert not only_stops_recording("Aufnahme stoppen, und noch eine Cola.")
    assert not only_stops_recording("Aufnahmestopp, und noch eine Cola.")


def test_the_words_of_a_stop_do_not_change_what_a_yes_is():
    """The yes of a booking stays as strict as it was (rule 3)."""
    assert not is_yes("Ja, ich bin einverstanden, und noch eine Cola.")
    assert not is_yes("Ich bin einverstanden")


# -- the agent asks ------------------------------------------------------------


def test_the_first_sentence_names_the_ai_and_asks(session, tenant, switched_on):
    """L1, tested like the greeting: the code says it first, and nobody talks
    over it. U1: only while recording is on, and the sentence ends on the
    question the answer belongs to."""
    port, _ = on_the_phone(session, tenant)

    first = port.says(SID)
    assert len(first) == 1
    assert first[0].startswith("Guten Tag, hier ist der KI-Assistent von Testbetrieb.")
    assert first[0].endswith(recording.question())
    assert SAY_WHAT_CAN_I_DO not in first[0]
    assert port.interruptible(SID) == []
    assert not port.recordings(SID)


def test_the_privacy_pointer_stays_in_the_first_sentence(session, tenant, switched_on):
    config = session.get(ServiceConfig, tenant.id)
    config.privacy_notice_address = "example.com/datenschutz"
    session.commit()

    port, _ = on_the_phone(session, tenant)

    assert "example.com/datenschutz" in port.says(SID)[0]


def test_a_yes_starts_the_recording_and_the_call_goes_on(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert answers(session) == ["yes"]
    assert [a.value for a in port.recordings(SID)] == [f"{SID}.wav"]
    row = the_recording(session)
    assert row.file_name == f"{SID}.wav"
    assert row.call_id == the_call(session, SID).id
    assert row.withdrawn_at is None
    assert port.says(SID)[1:] == [SAY_HOW_TO_STOP, SAY_WHAT_CAN_I_DO]
    assert not port.transfers(SID)


def test_the_log_holds_the_question_as_it_was_asked(session, tenant, switched_on):
    _, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Nein.")

    logged = session.scalars(select(ConsentLog)).one()
    assert logged.question == recording.question()
    assert logged.call_id == the_call(session, SID).id


def test_a_no_records_nothing_and_the_call_goes_on(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Nein, danke.")

    assert answers(session) == ["no"]
    assert not port.recordings(SID)
    assert the_recording(session) is None
    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]
    assert not port.transfers(SID)


def test_an_unclear_answer_is_the_callers_first_turn(session, tenant, switched_on):
    """Nothing is asked twice, and the guest's sentence is not lost: it goes to
    the core like any first sentence."""
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, A_TABLE)

    assert answers(session) == ["unclear"]
    assert not port.recordings(SID)
    assert SAY_WHAT_CAN_I_DO not in port.says(SID)
    assert len(port.says(SID)) > 1
    handler.on_call_ended(SID)
    assert the_call(session, SID).intent == "reservation"


@pytest.mark.parametrize(
    "refusal", ["Nein danke, nicht aufnehmen.", "Ich will keine Aufnahme."]
)
def test_a_no_with_a_request_in_it_keeps_the_request(
    session, tenant, switched_on, refusal
):
    """Review of the branch: the no is logged and nothing is recorded, and the
    request behind it goes to the core instead of being dropped."""
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, f"{refusal} {A_TABLE}")

    assert answers(session) == ["no"]
    assert not port.recordings(SID)
    assert SAY_WHAT_CAN_I_DO not in port.says(SID)[1:]
    # The core took the table and asks what it needs for it.
    assert "Tisch" in port.says(SID)[-1]


def test_the_way_to_stop_is_played_to_the_end(session, tenant, switched_on):
    """L2 wants the way to stop announced: a caller who starts talking does not
    cut it off while the recording already runs."""
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert SAY_HOW_TO_STOP not in port.interruptible(SID)


def test_silence_is_no_consent_and_the_call_goes_on(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)

    handler.on_silence(SID)

    assert answers(session) == ["unclear"]
    assert not port.recordings(SID)
    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]
    assert not port.hangups(SID)
    # The question is settled: the next silence is the usual one.
    handler.on_silence(SID)
    assert answers(session) == ["unclear"]
    assert port.says(SID)[2:] == ["Sind Sie noch da?"]


def test_a_yes_is_answered_by_the_core_without_the_model(session, tenant, switched_on):
    _, handler = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())
    handler.on_user_turn(SID, "Ja.")
    assert answers(session) == ["yes"]


def test_a_yes_to_the_question_is_no_yes_to_anything_later(
    session, tenant, switched_on
):
    """A draft read out later needs a yes of its own (rule 3)."""
    _, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)
    handler.on_user_turn(SID, "Auf den Namen Mueller.")
    assert session.scalars(select(Reservation.status)).all() == ["draft"]


# -- errors: no recording, and the call goes on --------------------------------


def test_a_consent_log_that_fails_costs_the_recording_not_the_call(
    session, tenant, switched_on, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the row")

    monkeypatch.setattr(recording, "record_consent", broken)
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert not port.recordings(SID)
    assert the_recording(session) is None
    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]
    assert not port.transfers(SID)


def test_a_row_that_fails_costs_the_recording_not_the_call(
    session, tenant, switched_on, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the row")

    monkeypatch.setattr(recording, "open_recording", broken)
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert answers(session) == ["yes"]
    assert not port.recordings(SID)
    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]


MB = 1024 * 1024


def free_on_the_disk(monkeypatch, free: int) -> None:
    """The disk of the recordings' folder, as `shutil.disk_usage` would see it."""
    usage = SimpleNamespace(disk_usage=lambda _: SimpleNamespace(free=free))
    monkeypatch.setattr(recordings, "shutil", usage)


def test_a_nearly_full_disk_costs_the_recording_not_the_call(
    session, tenant, switched_on, monkeypatch
):
    """The folder shares the disk with the database (#252, audit of 02.11.2026
    no. 7): below the floor a yes records nothing, and the guest hears nothing
    of a recording, not even how to stop it."""
    monkeypatch.setattr(settings, "recording_min_free_mb", 2048)
    free_on_the_disk(monkeypatch, 2048 * MB - 1)
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert answers(session) == ["yes"]
    assert the_recording(session) is None
    assert not port.recordings(SID)
    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]
    assert not port.transfers(SID)


def test_a_disk_at_the_floor_still_records(session, tenant, switched_on, monkeypatch):
    monkeypatch.setattr(settings, "recording_min_free_mb", 2048)
    free_on_the_disk(monkeypatch, 2048 * MB)
    port, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert the_recording(session) is not None
    assert [a.value for a in port.recordings(SID)] == [f"{SID}.wav"]


def test_a_process_without_the_folder_does_not_measure_it(
    session, tenant, switched_on, monkeypatch, tmp_path
):
    """`api` and the evals hold no folder: the row is written as before, the
    file is the line's (`voice`, Asterisk), which measures on its own disk."""
    monkeypatch.setattr(recordings, "FOLDER", tmp_path / "not-mounted")
    monkeypatch.setattr(settings, "recording_min_free_mb", 2048)
    _, handler = on_the_phone(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert the_recording(session) is not None


def test_the_floor_is_measured_on_the_folder_itself(switched_on, monkeypatch):
    """A floor of 0 always leaves room; a floor above the disk never does."""
    monkeypatch.setattr(settings, "recording_min_free_mb", 0)
    assert recordings.room_for_a_recording()
    monkeypatch.setattr(settings, "recording_min_free_mb", 2**40)
    assert not recordings.room_for_a_recording()


def test_a_failing_log_on_a_silent_line_costs_nothing(
    session, tenant, switched_on, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the row")

    monkeypatch.setattr(recording, "record_consent", broken)
    port, handler = on_the_phone(session, tenant)

    handler.on_silence(SID)

    assert port.says(SID)[1:] == [SAY_WHAT_CAN_I_DO]
    assert not port.recordings(SID)


def test_a_model_that_fails_after_the_answer_goes_to_the_team(
    session, tenant, switched_on
):
    """Rule 5 as ever; the recording of an AI call never reaches the team, the
    session stops it at the handover."""
    port, handler = on_the_phone(session, tenant, llm_factory=lambda _: BrokenLLM())

    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)

    assert port.transfers(SID) == [TEAM_PHONE]
    assert not any(a.kind == "team_leg" for a in port.actions)


# -- withdrawal in the middle of a call ----------------------------------------


def test_aufnahme_stoppen_stops_and_deletes_and_the_call_goes_on(
    session, tenant, switched_on
):
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")

    handler.on_user_turn(SID, "Aufnahme stoppen.")

    assert answers(session) == ["yes", "no"]
    logged = session.scalars(select(ConsentLog).where(ConsentLog.answer == "no")).one()
    assert logged.question == RECORDING_WITHDRAWN
    assert port.stopped(SID) == ["discard"]
    assert the_recording(session).withdrawn_at is not None
    assert port.says(SID)[-2:] == [SAY_RECORDING_STOPPED, SAY_WHAT_CAN_I_DO]
    assert not port.transfers(SID)


def test_a_withdrawal_repeats_what_the_guest_was_asked(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)
    asked = port.says(SID)[-1]

    handler.on_user_turn(SID, "Aufnahme stoppen.")

    assert port.says(SID)[-2:] == [SAY_RECORDING_STOPPED, asked]


def test_a_withdrawal_at_the_read_back_leaves_it_standing(session, tenant, switched_on):
    """The read-back is said again behind the stop, and a yes still confirms
    it: the sentence that stops the recording changes nothing of the draft."""
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)
    handler.on_user_turn(SID, "Auf den Namen Mueller.")
    read_back = port.says(SID)[-1]
    assert "Mueller" in read_back

    handler.on_user_turn(SID, "Aufnahme stoppen.")
    assert port.says(SID)[-2:] == [SAY_RECORDING_STOPPED, read_back]

    handler.on_user_turn(SID, "Ja, passt so.")
    handler.on_call_ended(SID)
    assert the_call(session, SID).outcome == "completed"


def test_a_withdrawal_with_more_in_it_is_a_turn_as_well(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")

    handler.on_user_turn(SID, f"Aufnahme stoppen. {A_TABLE}")

    assert port.stopped(SID) == ["discard"]
    assert port.says(SID)[3] == SAY_RECORDING_STOPPED
    assert len(port.says(SID)) == 5


def test_a_withdrawal_whose_log_fails_still_stops_the_recording(
    session, tenant, switched_on, monkeypatch
):
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")

    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the row")

    monkeypatch.setattr(recording, "record_consent", broken)
    handler.on_user_turn(SID, "Aufnahme stoppen.")

    assert port.stopped(SID) == ["discard"]
    assert SAY_RECORDING_STOPPED in port.says(SID)
    assert not port.transfers(SID)


def test_a_withdrawal_whose_turn_crashes_still_stops_the_recording(
    session, tenant, switched_on, monkeypatch
):
    """The core raises behind the withdrawal: the call goes to the team, and
    the file is gone all the same."""
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")

    def crash(*_args, **_kwargs):
        raise RuntimeError("the core crashed")

    monkeypatch.setattr(ConversationLoop, "_guest_turn", crash)
    handler.on_user_turn(SID, f"Aufnahme stoppen. {A_TABLE}")

    assert port.stopped(SID) == ["discard"]
    assert port.transfers(SID) == [TEAM_PHONE]


def test_a_stop_without_a_running_recording_stops_nothing(session, tenant, switched_on):
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Nein.")

    handler.on_user_turn(SID, "Aufnahme stoppen.")

    assert answers(session) == ["no"]
    assert port.stopped(SID) == []


def test_a_stop_said_into_a_protected_sentence_counts(session, tenant, switched_on):
    """The session hands it over while a recording runs (`on_talked_over`): no
    turn, the read-back stands, and the withdrawal holds all the same."""
    port, handler = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())
    handler.on_user_turn(SID, "Ja.")

    handler.on_talked_over(SID, "Aufnahme stoppen!")

    assert answers(session) == ["yes", "no"]
    assert port.stopped(SID) == ["discard"]
    assert port.says(SID)[-1] == SAY_RECORDING_STOPPED


def test_an_okay_to_the_stop_notice_confirms_nothing(session, tenant, switched_on):
    """Review of the branch: a stop said into the read-back puts the notice
    behind it, and the guest's "Okay" answers the notice, not the read-back
    (rule 3). The draft is read out again instead."""
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)
    handler.on_user_turn(SID, "Auf den Namen Mueller.")
    assert "Mueller" in port.says(SID)[-1]

    handler.on_talked_over(SID, "Aufnahme stoppen!")
    assert port.says(SID)[-1] == SAY_RECORDING_STOPPED
    handler.on_user_turn(SID, "Okay.")

    assert session.scalars(select(Reservation.status)).all() == ["draft"]
    assert "Mueller" in port.says(SID)[-1]


def test_anything_else_said_into_a_protected_sentence_stays_unheard(
    session, tenant, switched_on
):
    port, handler = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())
    handler.on_user_turn(SID, "Ja.")
    said = list(port.says(SID))

    handler.on_talked_over(SID, "Ja, genau.")

    assert port.says(SID) == said
    assert answers(session) == ["yes"]


# -- a stop while the call ends (#522) -------------------------------------------

ENDS = pytest.mark.parametrize("how", ["farewell", "transfer"])
WIRED = "voice-1"  # the call of `_Wire`
HUMAN = "Ich möchte bitte mit einem Menschen sprechen."


def recorded_on_the_wire(session, tenant) -> _Wire:
    """A call the AI takes, with session and handler, recorded from the yes."""
    wire = _Wire(session, tenant)
    wire.played()
    wire.says("Ja.")
    return wire


def ends(wire: _Wire, how: str) -> None:
    """The core ends the call: after a confirmed booking, or with a transfer.
    Its last sentence still plays."""
    if how == "farewell":
        wire.until_the_read_back()
        wire.played()
        wire.says("Ja, passt so.")
    else:
        wire.played()
        wire.says(HUMAN)
    assert wire.call.outcome == "open"


@ENDS
def test_a_stop_said_while_the_call_ends_withdraws_and_deletes_the_file(
    session, tenant, switched_on, how
):
    """The farewell after a booking and the sentence before a transfer are
    still part of the call, and the way to stop was promised at any time
    (L2). Nothing is said behind them, and the call ends as it would have."""
    wire = recorded_on_the_wire(session, tenant)
    ends(wire, how)
    assert (switched_on / f"{WIRED}.wav").exists()
    said = list(wire.line.played())

    wire.says("Aufnahme stoppen.")

    assert answers(session) == ["yes", "no"]
    assert the_recording(session).withdrawn_at is not None
    assert list(switched_on.iterdir()) == []
    assert wire.line.played() == said
    wire.played()
    assert wire.call.outcome == {"farewell": "hangup", "transfer": "transfer"}[how]
    expected = {"farewell": "completed", "transfer": "transferred"}[how]
    assert the_call(session, WIRED).outcome == expected
    if how == "farewell":
        assert session.scalars(select(Reservation.status)).all() == ["confirmed"]


@ENDS
def test_a_stop_while_an_unrecorded_call_ends_changes_nothing(
    session, tenant, switched_on, how
):
    wire = _Wire(session, tenant)
    wire.played()
    wire.says("Nein.")
    ends(wire, how)

    wire.says("Aufnahme stoppen.")
    wire.played()

    assert answers(session) == ["no"]
    assert wire.call.outcome == {"farewell": "hangup", "transfer": "transfer"}[how]


def test_a_stop_said_twice_while_the_call_ends_is_one_withdrawal(
    session, tenant, switched_on
):
    wire = recorded_on_the_wire(session, tenant)
    ends(wire, "farewell")

    wire.says("Aufnahme stoppen.")
    wire.says("Aufnahme stoppen!")
    wire.played()

    assert answers(session) == ["yes", "no"]
    assert wire.call.outcome == "hangup"


def test_a_stop_after_the_line_is_gone_is_not_heard(session, tenant, switched_on):
    """The leg is gone: the request goes the way of docs/09 §2."""
    wire = recorded_on_the_wire(session, tenant)
    ends(wire, "farewell")
    wire.played()

    wire.says("Aufnahme stoppen.")

    assert answers(session) == ["yes"]
    assert the_recording(session).withdrawn_at is None
    assert (switched_on / f"{WIRED}.wav").exists()


def test_a_stop_reported_after_the_end_of_the_leg_rings_nobody(
    session, tenant, switched_on
):
    """A late report from an adapter is ignored like any other: the call is
    closed, and nothing goes to the team a second time."""
    port, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, HUMAN)
    handler.on_call_ended(SID)

    handler.on_talked_over(SID, "Aufnahme stoppen.")

    assert port.transfers(SID) == [TEAM_PHONE]
    assert answers(session) == ["yes"]


# -- mode shadow: the voice layer asks before the team rings -------------------


@pytest.fixture
def shadow(session, tenant, switched_on):
    set_mode(session, tenant.id, "shadow")


def shadow_call(session, tenant, **more):
    return on_the_phone(session, tenant, llm_factory=lambda _: NoLLM(), **more)


def test_shadow_names_the_ai_and_asks_before_the_team_rings(session, tenant, shadow):
    port, _ = shadow_call(session, tenant)

    assert len(port.says(SID)) == 1
    first = port.says(SID)[0]
    assert first.startswith("Guten Tag, hier ist der KI-Assistent von Testbetrieb.")
    assert first.endswith(recording.question())
    assert port.interruptible(SID) == []
    assert not port.transfers(SID)


def test_shadow_a_yes_rings_the_team_with_a_recorded_leg(session, tenant, shadow):
    port, handler = shadow_call(session, tenant)

    handler.on_user_turn(SID, "Ja, gerne.")

    assert answers(session) == ["yes"]
    assert port.team_leg_recorded(SID)
    assert the_recording(session).file_name == f"{SID}.wav"
    assert port.transfers(SID) == [TEAM_PHONE]
    assert port.says(SID)[1:] == [SAY_HOW_TO_STOP, SAY_TO_THE_TEAM]
    # What `voice` itself writes is the AI's side only: nothing here.
    assert not port.recordings(SID)


def test_shadow_a_stop_before_the_team_rings_keeps_the_leg_off_the_file(
    session, tenant, shadow
):
    """#522: the way to stop is said right before the handover, so a stop is
    likely there; said into it, it holds, and the dialplan records nothing."""
    wire = _Wire(session, tenant)
    wire.played()
    wire.says("Ja, gerne.")
    assert wire.line.played()[-2:] == [SAY_HOW_TO_STOP, SAY_TO_THE_TEAM]

    wire.says("Aufnahme stoppen.")
    wire.played()

    assert answers(session) == ["yes", "no"]
    assert the_recording(session).withdrawn_at is not None
    assert wire.line.actions[-1] == ("transfer", TEAM_PHONE)
    assert ("record_team_leg",) not in wire.line.actions


def test_shadow_a_yes_on_a_nearly_full_disk_rings_the_team_without(
    session, tenant, shadow, monkeypatch
):
    """The dialplan is not told to record, and nobody hears how to stop."""
    monkeypatch.setattr(settings, "recording_min_free_mb", 2048)
    free_on_the_disk(monkeypatch, 0)
    port, handler = shadow_call(session, tenant)

    handler.on_user_turn(SID, "Ja, gerne.")

    assert answers(session) == ["yes"]
    assert not port.team_leg_recorded(SID)
    assert the_recording(session) is None
    assert port.transfers(SID) == [TEAM_PHONE]
    assert port.says(SID)[1:] == [SAY_TO_THE_TEAM]


@pytest.mark.parametrize("said", ["Nein.", "Wie bitte?", A_TABLE])
def test_shadow_a_no_or_anything_else_rings_the_team_without(
    session, tenant, shadow, said
):
    port, handler = shadow_call(session, tenant)

    handler.on_user_turn(SID, said)

    assert not port.team_leg_recorded(SID)
    assert the_recording(session) is None
    assert port.transfers(SID) == [TEAM_PHONE]
    assert port.says(SID)[1:] == [SAY_TO_THE_TEAM]


def test_shadow_answers_the_question_for_a_human_truthfully(session, tenant, shadow):
    """L1: always, also in the one sentence the voice layer says here."""
    port, handler = shadow_call(session, tenant)

    handler.on_user_turn(SID, "Sind Sie ein Mensch?")

    assert port.says(SID)[1:] == [SAY_NOT_A_PERSON, SAY_TO_THE_TEAM]
    assert not port.team_leg_recorded(SID)
    assert port.transfers(SID) == [TEAM_PHONE]


def test_shadow_silence_rings_the_team_without(session, tenant, shadow):
    port, handler = shadow_call(session, tenant)

    handler.on_silence(SID)

    assert answers(session) == ["unclear"]
    assert not port.team_leg_recorded(SID)
    assert port.transfers(SID) == [TEAM_PHONE]


def test_shadow_keys_are_no_ja(session, tenant, shadow):
    port, handler = shadow_call(session, tenant)

    handler.on_dtmf(SID, "1")

    assert answers(session) == ["unclear"]
    assert not port.team_leg_recorded(SID)
    assert port.transfers(SID) == [TEAM_PHONE]


def test_shadow_a_failing_voice_rings_the_team_without(session, tenant, shadow):
    port, handler = shadow_call(session, tenant)

    handler.on_voice_failure(SID)

    assert not port.team_leg_recorded(SID)
    assert port.transfers(SID) == [TEAM_PHONE]
    assert the_recording(session) is None


def test_shadow_a_failing_log_rings_the_team_without(
    session, tenant, shadow, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the row")

    monkeypatch.setattr(recording, "record_consent", broken)
    port, handler = shadow_call(session, tenant)

    handler.on_user_turn(SID, "Ja.")

    assert not port.team_leg_recorded(SID)
    assert the_recording(session) is None
    assert port.transfers(SID) == [TEAM_PHONE]


def test_shadow_a_question_that_cannot_be_said_rings_the_team(
    session, tenant, shadow, monkeypatch
):
    """The line refuses the sentence: the outage path of every call."""

    class Refusing(FakeTelephony):
        def say(self, session_id, text, interruptible=False):
            raise RuntimeError("the line refuses the sentence")

    port = Refusing()
    port.open(SID)
    handler_for(session, tenant, port, llm_factory=lambda _: NoLLM()).on_call_started(
        SID
    )

    assert port.transfers(SID) == [TEAM_PHONE]


def test_a_caller_who_hangs_up_on_the_question_leaves_no_recording(
    session, tenant, shadow
):
    """Decision (e) of T-11.3: nothing was answered, so no consent row."""
    port, handler = shadow_call(session, tenant)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert answers(session) == []
    assert the_recording(session) is None
    assert not port.team_leg_recorded(SID)


# -- mode shadow: a caller who hangs up before the team rang (#252 no. 9) ------


def cards(session) -> list[Callback]:
    return list(session.scalars(select(Callback)))


def test_shadow_a_caller_who_hangs_up_on_the_question_leaves_a_card(
    session, tenant, shadow
):
    """The team never rang: the card is its only notice of the call."""
    port, handler = shadow_call(session, tenant)

    handler.on_call_ended(SID, caller_hung_up=True)

    [card] = cards(session)
    assert card.phone == "+497215551234"
    assert card.status == "open"
    assert card.reason == "out_of_scope"
    assert card.summary.startswith(SUMMARY_HUNG_UP)
    assert the_call(session, SID).outcome == "callback"
    assert not port.transfers(SID)


def test_shadow_a_withheld_number_leaves_no_card(session, tenant, shadow):
    """Nobody could call back: the call stays abandoned, as before."""
    port = FakeTelephony()
    port.open(SID, caller_id=None)
    handler = handler_for(session, tenant, port, llm_factory=lambda _: NoLLM())
    handler.on_call_started(SID)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert cards(session) == []
    assert the_call(session, SID).outcome == "abandoned"


@pytest.mark.parametrize("said", ["Ja, gerne.", "Nein."])
def test_shadow_a_caller_who_answered_and_left_before_the_team_rang_leaves_a_card(
    session, tenant, shadow, said
):
    """The answer is taken and the transfer asked for, but the line still plays
    the sentences before it when the caller's leg goes: the team never rang."""
    _, handler = shadow_call(session, tenant)
    handler.on_user_turn(SID, said)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert len(cards(session)) == 1
    assert the_call(session, SID).outcome == "callback"


def test_shadow_a_call_that_ends_after_the_transfer_leaves_no_card(
    session, tenant, shadow
):
    """The line took the transfer and the team rings; a leg nobody takes is the
    dialplan's report (`on_team_missed`)."""
    _, handler = shadow_call(session, tenant)
    handler.on_user_turn(SID, "Nein.")

    handler.on_call_ended(SID)

    assert cards(session) == []
    assert the_call(session, SID).outcome == "transferred"


def test_shadow_a_hangup_reported_twice_leaves_one_card(session, tenant, shadow):
    _, handler = shadow_call(session, tenant)

    handler.on_call_ended(SID, caller_hung_up=True)
    handler.on_call_ended(SID, caller_hung_up=True)

    assert len(cards(session)) == 1


def test_shadow_a_hangup_outside_the_opening_hours_leaves_a_card_too(
    session, tenant, shadow
):
    """Shadow rings the team at any hour (D25), so its card comes at any hour."""
    _, handler = shadow_call(session, tenant, now=CLOSED)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert len(cards(session)) == 1


def test_shadow_a_card_that_fails_costs_nothing_else(
    session, tenant, shadow, monkeypatch
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("the database refuses the card")

    monkeypatch.setattr(handler_module, "team_missed", broken)
    _, handler = shadow_call(session, tenant)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert cards(session) == []
    assert the_call(session, SID).outcome == "abandoned"


def test_shadow_a_hangup_on_the_fake_line_leaves_a_card(session, tenant, shadow):
    """The fake line reports a caller who hung up as the call session does."""
    port = FakeTelephony()
    handler = handler_for(session, tenant, port, llm_factory=lambda _: NoLLM())
    hangup = {"role": "customer", "hangup": True}

    port.play(
        {"session_id": SID, "caller_id": "+497215551234", "transcript": [hangup]},
        handler,
    )
    port.play(
        {
            "session_id": "answered",
            "caller_id": "+497215551234",
            "transcript": [{"role": "customer", "text": "Nein."}],
        },
        handler,
    )

    assert [the_call(session, sid).outcome for sid in (SID, "answered")] == [
        "callback",
        "transferred",
    ]


@pytest.mark.parametrize("mode", ["primary", "overflow"])
def test_a_call_the_ai_takes_leaves_no_card_on_a_hangup_at_the_question(
    session, tenant, switched_on, mode
):
    set_mode(session, tenant.id, mode)
    _, handler = on_the_phone(session, tenant)

    handler.on_call_ended(SID, caller_hung_up=True)

    assert cards(session) == []
    assert the_call(session, SID).outcome == "abandoned"


def test_shadow_without_the_setting_leaves_no_card(session, tenant):
    """The team rings at once, before the caller could hang up on us."""
    set_mode(session, tenant.id, "shadow")
    port, handler = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())

    handler.on_call_ended(SID, caller_hung_up=True)

    assert port.transfers(SID) == [TEAM_PHONE]
    assert cards(session) == []


def test_paused_never_asks(session, tenant, switched_on):
    """The emergency stop: the AI says nothing at all."""
    set_mode(session, tenant.id, "paused")
    port, _ = on_the_phone(session, tenant, llm_factory=lambda _: NoLLM())
    assert port.says(SID) == []
    assert port.transfers(SID) == [TEAM_PHONE]


def test_a_call_above_the_limit_never_asks(session, tenant, switched_on):
    """The team takes it silently, as ever (D16)."""
    port = FakeTelephony()
    port.open("first")
    port.open("second")
    handler = handler_for(session, tenant, port)
    session.get(ServiceConfig, tenant.id).max_concurrent_calls = 1
    session.commit()

    handler.on_call_started("first")
    handler.on_call_started("second")

    assert port.says("second") == []
    assert port.transfers("second") == [TEAM_PHONE]


# -- a stop on the team leg (the key of the team's telephone) ------------------


def test_the_stop_key_withdraws_the_recording_of_a_finished_leg(
    session, tenant, shadow
):
    _, handler = shadow_call(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_call_ended(SID)

    assert handler.on_recording_stopped(SID) is True

    logged = session.scalars(select(ConsentLog).where(ConsentLog.answer == "no")).one()
    assert logged.question == RECORDING_WITHDRAWN
    assert the_recording(session).withdrawn_at is not None


def test_the_stop_key_is_idempotent(session, tenant, shadow):
    _, handler = shadow_call(session, tenant)
    handler.on_user_turn(SID, "Ja.")
    handler.on_call_ended(SID)
    assert handler.on_recording_stopped(SID) is True

    assert handler.on_recording_stopped(SID) is False

    assert answers(session).count("no") == 1


def test_the_stop_key_on_a_call_without_a_recording_says_so(session, tenant, shadow):
    _, handler = shadow_call(session, tenant)
    handler.on_user_turn(SID, "Nein.")
    handler.on_call_ended(SID)

    assert handler.on_recording_stopped(SID) is False
    assert handler.on_recording_stopped("no-such-call") is False


def test_the_stop_key_never_raises(session, tenant, shadow):
    _, handler = shadow_call(session, tenant)

    def broken():
        raise RuntimeError("the database is down")

    handler._session_factory = broken
    assert handler.on_recording_stopped(SID) is False


# -- the model never decides ---------------------------------------------------


def test_the_state_the_model_gets_says_nothing_of_the_recording(
    session, tenant, switched_on
):
    seen: list[dict] = []

    class Watching(ScriptedLLM):
        def next_turn(self, system_prompt, state_json, input_text, *, timeout=None):
            seen.append(state_json)
            return super().next_turn(
                system_prompt, state_json, input_text, timeout=timeout
            )

    make = scripted(session, tenant)

    def factory(s):
        real = make(s)
        real.__class__ = Watching
        return real

    _, handler = on_the_phone(session, tenant, llm_factory=factory)
    handler.on_user_turn(SID, "Ja.")
    handler.on_user_turn(SID, A_TABLE)

    assert seen
    assert not any("recording" in str(state) for state in seen)


def test_a_recording_row_belongs_to_the_tenant_and_starts_at_the_yes(
    session, tenant, switched_on
):
    _, handler = on_the_phone(session, tenant)
    handler.on_user_turn(SID, "Ja.")

    row = the_recording(session)
    assert row.tenant_id == tenant.id
    assert row.started_at == NOW
