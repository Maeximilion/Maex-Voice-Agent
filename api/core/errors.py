"""Fehlerklassen der Fachlogik. Werden zentral in die Antwort-Hülle übersetzt (docs/04 §1).

Fachcode wirft diese Klassen, nie HTTPException. Der Agent liest jede Antwort vor,
deshalb tragen Fehler einen Code, eine Meldung fürs Log und optional einen Satz (say).
"""

# The sentence for every failure the guest cannot fix: the agent hands the call
# to the team. Defined once here, so domain code needs no import of the HTTP hull.
SAY_ON_FAILURE = "Bei mir gibt es gerade eine technische Störung. Ich verbinde Sie mit dem Restaurant."

ERROR_CODES = (
    "invalid_input",
    "not_found",
    "ambiguous",
    "closed",
    "out_of_zone",
    "below_minimum",
    "conflict",
    "service_unavailable",
)


class AppError(Exception):
    code: str = "service_unavailable"
    # 200, damit die Voice-Plattform die Antwort dem Agenten vorlegt statt sie zu verwerfen.
    status: int = 200

    def __init__(self, message: str, say: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.say = say
        if status is not None:
            self.status = status


class InvalidInput(AppError):
    code = "invalid_input"


class NotFound(AppError):
    code = "not_found"


class Ambiguous(AppError):
    code = "ambiguous"


class Closed(AppError):
    code = "closed"


class OutOfZone(AppError):
    code = "out_of_zone"


class BelowMinimum(AppError):
    code = "below_minimum"


class Conflict(AppError):
    code = "conflict"


class TeamDecides(Conflict):
    """A table that is free, but larger than the agent books itself
    (`service_config`, #252). No draft: the wish goes to the team as a
    callback. A conflict for every client; the conversation core tells it
    apart and creates that callback itself (`agent/loop.py`)."""


class ServiceUnavailable(AppError):
    code = "service_unavailable"


class Unauthorized(InvalidInput):
    status = 401
