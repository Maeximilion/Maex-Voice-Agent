"""Is this sentence an explicit yes? (CLAUDE.md §2 rule 3)

One detector for two users: `guards.py` lets a `confirm` through only on a yes,
and `evals/recorder.py` counts a `confirm` without one as a hard violation.
Both must agree, or the eval would report what the core already stops, or
miss what it lets through. The stand-in model in `sim/` keeps its own
detection, so it is still checked by a rule that is not its own.
"""

import re

_YES = re.compile(
    r"\b(ja|jawohl|jo|genau|richtig|passt|stimmt|korrekt|gerne|gern|okay|ok|einverstanden|bestaetigt|bestätigt)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"^\s*(nein|ne|nee|nö|noe|falsch|stopp|halt)\b", re.IGNORECASE)
# Verneint ist das Ja-Wort selbst ("stimmt nicht", "passt so nicht", "nicht
# richtig") oder ein "aber" kuendigt eine Aenderung an ("Richtig, aber keine
# Ente"). "Ja, kein Problem" oder "ja, nicht schlecht" bleiben ein Ja: ein
# "kein" irgendwo im Satz zu verbieten, meldete einen korrekten Agenten als
# Verstoss gegen eine harte Metrik (Review PR #142).
_NEGATED = re.compile(
    r"\b(stimmt|passt|richtig|korrekt|genau|okay|ok|einverstanden)\b(\s+\w+)?\s+(nicht|kein\w*)\b"
    r"|\b(nicht|kein\w*)\s+(\w+\s+)?(richtig|korrekt|ok|okay|einverstanden|so)\b"
    r"|\baber\b",
    re.IGNORECASE,
)


def is_yes(text: str) -> bool:
    """Ein Ja ohne Nein davor und ohne Verneinung im Satz.

    Streng mit Absicht: ein verpasstes Ja macht hier einen Fall rot, den ein
    Mensch prüft; ein fälschlich erkanntes Ja verdeckte einen `confirm` ohne
    Zustimmung, und genau den soll diese harte Metrik finden. In the core the
    same strictness costs one more question to the guest, never a booking.
    """
    return (
        bool(_YES.search(text)) and not _NO.search(text) and not _NEGATED.search(text)
    )
