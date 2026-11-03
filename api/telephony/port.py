"""The telephony port: the one seam between the phone line and the agent (docs/11 §telephony).

Two directions, two protocols:

- `CallEvents` is what the line reports to us. It is implemented once and
  provider-neutral, by `telephony/handler.py`.
- `TelephonyPort` is what we make the line do. The call sessions implement it
  (`telephony/session.py`), the fake adapter does for the tests.

The media adapter turns what the line sends into a call session per call, and
the session turns that into `CallEvents` calls. Changing the provider changes one
adapter file and `.env`, nothing else (docs/12 S7). Calls are named by the id the
dialplan gives them, the same value that `calls.external_session_id` stores.
"""

from typing import Protocol

from api.telephony.speech import SpeechUsage


class TelephonyPort(Protocol):
    def caller_id(self, session_id: str) -> str | None:
        """The caller's number as the network delivers it (E.164 or national
        format), None when withheld. A SIP URI is the adapter's to strip."""
        ...

    def say(self, session_id: str, text: str, interruptible: bool = False) -> None:
        """Speak `text` to the caller. In the order of the calls.

        `interruptible`: may the caller talk over this sentence and stop it?
        Never a read-back, the disclosure at the start or a sentence before a
        transfer or hangup: those are played to the end (docs/20 §4)."""
        ...

    def expect(self, session_id: str, terms: list[str]) -> None:
        """Key terms for the speech engine: the dish names in question at this
        point of the call, never the menu (docs/20 §6, CLAUDE.md §2 rule 6).
        They help the engine write a name it would otherwise miss; what the
        guest ordered is still decided by the search, never by them."""
        ...

    def transfer(self, session_id: str, target: str) -> None:
        """Hand the live call to `target` (E.164). After this the line is no
        longer ours. Raises when the line refuses."""
        ...

    def hangup(self, session_id: str) -> None:
        """End the call after everything said so far was spoken."""
        ...

    def start_recording(self, session_id: str, file_name: str) -> None:
        """Record the call from here on, into `file_name` of the recordings'
        folder, up to the handover or its end (`agent/recording.py`). Only after
        the caller's yes, and only with `RECORDING_ENABLED` (docs/09 L2, L11).
        Never raises for a recording that fails: the call goes on without."""
        ...

    def stop_recording(self, session_id: str, *, discard: bool = False) -> None:
        """End it now; `discard` deletes the file too (a withdrawal)."""
        ...

    def record_team_leg(self, session_id: str) -> None:
        """The team leg of the next transfer is recorded by the telephone
        system. A recorded call in mode `shadow`: the AI has no part in it."""
        ...


class CallEvents(Protocol):
    """What the line reports of its calls. The events of one call come one
    after another, never two at once (`CallSession` reports them on the call's
    worker): the handler uses a call's database session on the thread that
    reports. Events of different calls may come at the same time. Not so
    `on_team_missed`: it comes from the dialplan on another thread, also while
    `on_call_ended` of the same call still runs, and uses no session of the
    call."""

    def on_call_started(self, session_id: str) -> None:
        """A call was picked up. Called again for the same session on a retry."""
        ...

    def on_user_turn(self, session_id: str, text: str) -> None:
        """The caller said `text` (speech recognition result, one utterance)."""
        ...

    def on_dtmf(self, session_id: str, digits: str) -> None:
        """The caller pressed keys (docs/05 §2, step 4 of the understanding ladder)."""
        ...

    def on_silence(self, session_id: str) -> None:
        """The caller has said nothing since the last sentence was played to
        the end, for as long as the silence timeout says. Reported once per
        silence; the next one counts from the end of what is said in answer."""
        ...

    def on_talked_over(self, session_id: str, text: str) -> None:
        """While a recording runs: the caller said `text` into a sentence that
        may not be interrupted. No turn; it may be a withdrawal (docs/09 L2)."""
        ...

    def on_voice_failure(self, session_id: str) -> None:
        """The voice layer itself failed on this call: the ear or the mouth
        raised, or the ear makes nothing of the caller's speech. A hosted
        platform used to hide this; with our own layer the core has to send
        the call to the team (docs/20 §4, CLAUDE.md §2 rule 5)."""
        ...

    def on_call_ended(
        self,
        session_id: str,
        speech: SpeechUsage | None = None,
        *,
        caller_hung_up: bool = False,
    ) -> None:
        """The line is gone: the caller hung up, or the line closed it after
        our hangup or transfer. Reported once for every call, also after a
        failure; the core closes its call log on it, with what the call cost
        the ear and the mouth (`speech`, None where not measured).
        `caller_hung_up`: the caller's leg was gone before the line had our
        hangup or transfer, so nothing rings the team for this call."""
        ...

    def on_team_missed(self, session_id: str) -> bool:
        """Nobody answered the team leg of a call we handed on, after its end
        was reported: a callback card for the team (docs/20 §5, T-10.8). True
        only once the card is written, so the caller may hear that the
        restaurant calls back. Never raises."""
        ...

    def on_recording_stopped(self, session_id: str) -> bool:
        """The team stopped the recording of a team leg with the key of their
        telephone (docs/20 §5, T-11.3); the dialplan deleted the file. True when
        a running recording was withdrawn. Never raises. Like `on_team_missed`
        it comes from the dialplan and uses no session of the call."""
        ...
