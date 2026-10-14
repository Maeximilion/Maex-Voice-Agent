"""The telephony port: the one seam between a voice platform and the agent (docs/11 §telephony).

Two directions, two protocols:

- `CallEvents` is what the platform reports to us. It is implemented once and
  provider-neutral, by `telephony/handler.py`.
- `TelephonyPort` is what we make the platform do. Each adapter in `adapters/`
  implements it with its provider's API.

An adapter turns its provider's webhooks into `CallEvents` calls and implements
`TelephonyPort`. Changing the provider changes one adapter file and `.env`, nothing
else (docs/12 S7). Calls are named by the platform's own session id, the same value
that `calls.external_session_id` stores.
"""

from typing import Protocol


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

    def transfer(self, session_id: str, target: str) -> None:
        """Hand the live call to `target` (E.164). After this the line is no
        longer ours. Raises when the platform refuses."""
        ...

    def hangup(self, session_id: str) -> None:
        """End the call after everything said so far was spoken."""
        ...

    def start_recording(self, session_id: str) -> None:
        """Part of the port so an adapter can be tested against it. Nothing calls
        it before the legal check in docs/09 is ticked (CLAUDE.md §9)."""
        ...


class CallEvents(Protocol):
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

    def on_voice_failure(self, session_id: str) -> None:
        """The voice layer itself failed on this call: the ear or the mouth
        raised, or the ear makes nothing of the caller's speech. A platform
        used to hide this; with our own layer the core has to send the call to
        the team (docs/20 §4, CLAUDE.md §2 rule 5)."""
        ...

    def on_call_ended(self, session_id: str) -> None:
        """The line is gone: the caller hung up, or the platform closed it after
        our hangup or transfer."""
        ...
