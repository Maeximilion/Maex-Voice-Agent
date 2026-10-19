"""Fehlerklassen der Fachlogik. Werden zentral in die Antwort-Hülle übersetzt (docs/04 §1).

Fachcode wirft diese Klassen, nie HTTPException. Der Agent liest jede Antwort vor,
deshalb tragen Fehler einen Code, eine Meldung fürs Log und optional einen Satz (say).
"""

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


class ServiceUnavailable(AppError):
    code = "service_unavailable"


class Unauthorized(InvalidInput):
    status = 401
