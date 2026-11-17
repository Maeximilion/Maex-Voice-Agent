"""Display of the tab "Daten" (#571, T-11.8, docs/06 §4): German words, dates
and the typed fields of its forms.

The rules are in `domain/customers/requests.py` (legal check U2): who a person
is, what a request's deadline is, what an answer starts. This file formats what
they return and reads what the owner typed. Nothing here is logged: the fields
hold a guest's name, address and numbers.
"""

import json
import re
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import AppError, InvalidInput
from api.core.time import to_local
from api.domain.customers import requests
from api.domain.customers.phone import normalize_phone
from api.gui.calls_view import OUTCOMES as CALL_OUTCOMES
from api.gui.calls_view import REASONS, duration, euro
from api.gui.figures_view import DASH
from api.gui.orders_view import TYPE_LABELS
from api.models import DataSubjectRequest, Tenant

KINDS = {"access": "Auskunft", "erasure": "Löschung", "objection": "Widerspruch"}
CHANNELS = {
    "phone": "Telefon",
    "mail": "E-Mail oder Brief",
    "in_person": "persönlich",
}
OUTCOMES = {
    "done": "erledigt",
    "not_verified": "nicht bestätigt",
    "refused": "abgelehnt",
    "withdrawn": "zurückgezogen",
}
FAILED = "Das hat nicht geklappt. Bitte nochmal tippen."
ANSWERED = "Diese Anfrage ist schon beantwortet."
NO_EXTENSION = (
    "Die Frist dieser Anfrage lässt sich hier nicht verlängern: das geht einmal "
    "und nur im ersten Monat."
)
BAD_DAY = "Bitte jeden Tag als Datum angeben."
# The new request's inputs: by the domain's name of a fault (`requests`), and
# by their id and words in the list of faults.
REQUEST_INPUTS = {"note": "notiz", "received": "eingang"}
REQUEST_FIELDS = {
    "notiz": ("f-notiz", "Angaben des Gastes"),
    "eingang": ("f-eingang", "Eingegangen am"),
}
BAD_PHONE = "Eine der Rufnummern ist nicht lesbar. Bitte mit Vorwahl eintragen."
TOO_MUCH = "Ein Feld ist zu lang oder es sind zu viele Angaben."
BAD_SIGN = "Ein Feld enthält ein Zeichen, das hier nicht vorkommen kann."
# What one field of the forms may hold; a name or a street is never longer.
FIELD_MAX = 120
DAY_FIELDS = ("tag-1", "tag-2", "tag-3")
PHONES_MAX = 5
CLAIMS_MAX = 100
CLAIM = "claim-"
DETAIL_FIELDS = ("name", *DAY_FIELDS, "strasse", "hausnummer", "plz", "nummern")
DAY = "%d.%m.%Y"
MOMENT = "%d.%m.%Y %H:%M"
# The codes of a status column in the team's words; an unknown one is shown as
# it is stored.
STATUSES = {
    "draft": "Entwurf",
    "confirmed": "bestätigt",
    "approved": "angenommen",
    "handed_over": "übergeben",
    "cancelled": "storniert",
    "open": "offen",
    "done": "erledigt",
}
ANSWERS = {"yes": "ja", "no": "nein", "unclear": "unklar"}
CODES = {
    "type": TYPE_LABELS,
    "status": STATUSES,
    "reason": REASONS,
    "outcome": CALL_OUTCOMES,
    "answer": ANSWERS,
}
CALLBACK_FIELDS = (
    ("created_at", "Angelegt"),
    ("reason", "Anlass"),
    ("summary", "Anliegen"),
    ("phone", "Telefon"),
    ("status", "Zustand"),
)
CALL_FIELDS = (
    ("started_at", "Beginn"),
    ("caller_id", "Nummer"),
    ("duration_seconds", "Dauer"),
    ("outcome", "Ergebnis"),
)
# The parts of the access copy (`requests.access_copy`), in the order the page
# shows them: key, heading, then each field with its label.
COPY = (
    (
        "customers",
        "Kundendaten",
        (
            ("name", "Name"),
            ("phone", "Telefon"),
            ("last_order_at", "Letzte Bestellung"),
            ("last_contact_at", "Letzter Kontakt"),
        ),
    ),
    (
        "addresses",
        "Adressen",
        (
            ("street", "Straße"),
            ("house_number", "Hausnummer"),
            ("postal_code", "PLZ"),
            ("city", "Ort"),
            ("floor_note", "Hinweis zur Adresse"),
        ),
    ),
    (
        "orders",
        "Bestellungen",
        (
            ("created_at", "Aufgenommen"),
            ("type", "Art"),
            ("status", "Zustand"),
            ("customer_name", "Name"),
            ("phone", "Telefon"),
            ("note", "Hinweis"),
            ("ready_at", "Fertig um"),
            ("total_cents", "Summe"),
            ("items", "Gerichte"),
        ),
    ),
    (
        "reservations",
        "Reservierungen",
        (
            ("reserved_for", "Termin"),
            ("status", "Zustand"),
            ("guest_name", "Name"),
            ("phone", "Telefon"),
            ("party_size", "Personen"),
            ("note", "Hinweis"),
        ),
    ),
    ("callbacks", "Rückrufe", CALLBACK_FIELDS),
    ("calls", "Anrufe", CALL_FIELDS),
    (
        "consent_log",
        "Einwilligungen",
        (
            ("answered_at", "Beantwortet"),
            ("question", "Frage"),
            ("answer", "Antwort"),
        ),
    ),
    (
        "recordings",
        "Aufnahmen",
        (
            ("started_at", "Beginn"),
            ("file_name", "Datei"),
            ("withdrawn_at", "Widerrufen"),
        ),
    ),
    (
        "outbox",
        "Meldungen an Team und Küche",
        (
            ("created_at", "Geschrieben"),
            ("event_type", "Art"),
            ("payload", "Inhalt"),
        ),
    ),
)
OPEN_WORDS = {
    "orders": ("Bestellung", "orders", "created_at"),
    "reservations": ("Reservierung", "reservations", "reserved_for"),
    "callbacks": ("Rückruf", "callbacks", "created_at"),
}


def _days(count: int) -> str:
    return f"{count} {'Tag' if count == 1 else 'Tage'}"


def periods() -> list[tuple[str, str]]:
    """The deletion periods as the daily job applies them (docs/03
    "Löschkonzept"): every number is a setting, read here from the same place."""
    s = settings
    allergy = "am Ende des Betriebstags"
    if s.retention_allergy_note_days:
        allergy = f"{_days(s.retention_allergy_note_days)} nach dem Betriebstag"
    return [
        (
            "Aufnahme eines Anrufs",
            f"{_days(s.retention_recording_days)} nach dem Anruf",
        ),
        (
            "Rufnummer in der Anrufliste",
            f"{_days(s.retention_caller_id_days)} nach dem Anruf",
        ),
        (
            "Name, Telefon, Adresse und Hinweise an Bestellungen und Reservierungen",
            f"{_days(s.retention_guest_details_days)} nach dem Termin",
        ),
        ("Hinweis auf eine Allergie", allergy),
        (
            "Nummer und Anliegen eines Rückrufs",
            f"{_days(s.retention_callback_days)} nach „Erledigt“",
        ),
        (
            "Kunden mit ihren Adressen",
            f"{_days(s.retention_customer_days)} nach dem letzten Kontakt",
        ),
        (
            "Antworten auf die Frage nach der Aufnahme",
            f"bis zum Ende des {s.retention_consent_log_years}. Kalenderjahres "
            "nach der Antwort",
        ),
        (
            "Protokoll der Änderungen am Tablet",
            f"{_days(s.retention_audit_log_days)}",
        ),
        (
            "Anfragen von Gästen",
            f"die Angaben zur Person {s.retention_request_details_months} Monate "
            f"nach der Antwort, die Zeile bis zum Ende des "
            f"{s.retention_request_log_years}. Kalenderjahres nach der Antwort",
        ),
        (
            "Bestellungen ohne persönliche Angaben",
            "bleiben für die Buchhaltung",
        ),
    ]


def _left(deadline: date, today: date) -> tuple[str, bool]:
    days = (deadline - today).days
    if days < 0:
        return f"seit {_days(-days)} überfällig", True
    if days == 0:
        return "läuft heute ab", True
    return f"noch {_days(days)}", days <= 7


def page(session: Session, tenant: Tenant, now: datetime) -> dict[str, Any]:
    """The page's context: the periods, the open requests by deadline, the
    newest answers. No verification note: that is shown with its request."""
    zone = tenant.timezone
    today = to_local(now, zone).date()
    open_rows = []
    for row in requests.open_requests(session, tenant.id):
        left, soon = _left(row.deadline, today)
        open_rows.append(
            {
                "reference": row.reference,
                "kind": KINDS.get(row.kind, row.kind),
                "received": f"{to_local(row.received_at, zone):{DAY}}",
                "deadline": f"{row.deadline:{DAY}}",
                "left": left,
                "soon": soon,
            }
        )
    answered = [
        {
            "reference": row.reference,
            "kind": KINDS.get(row.kind, row.kind),
            "outcome": OUTCOMES.get(row.outcome or "", DASH),
            "answered": f"{to_local(row.answered_at, zone):{DAY}}"
            if row.answered_at
            else DASH,
            "delete_after": f"{row.delete_after:{DAY}}" if row.delete_after else DASH,
        }
        for row in requests.answered_requests(session, tenant.id)
    ]
    return {
        "periods": periods(),
        "open": open_rows,
        "answered": answered,
        "seen": requests.log_token(session, tenant.id),
        "kinds": list(KINDS.items()),
        "channels": list(CHANNELS.items()),
        "today": today.isoformat(),
        "note_max": requests.NOTE_MAX,
        "note_months": settings.retention_request_details_months,
    }


def request_context(
    session: Session, tenant: Tenant, row: DataSubjectRequest, now: datetime
) -> dict[str, Any]:
    """One request as its sheet shows it, with what can still be done to it."""
    zone = tenant.timezone
    left, soon = _left(row.deadline, to_local(now, zone).date())
    return {
        "reference": row.reference,
        "kind_code": row.kind,
        "kind": KINDS.get(row.kind, row.kind),
        "channel": CHANNELS.get(row.channel, row.channel),
        "received": f"{to_local(row.received_at, zone):{DAY}}",
        "deadline": f"{row.deadline:{DAY}}",
        "left": left,
        "soon": soon,
        "note": row.verification_note,
        "is_open": row.answered_at is None,
        "outcome": OUTCOMES.get(row.outcome or "", DASH),
        "can_extend": requests.can_extend(row, zone, now),
        "runs_on_server": row.kind in requests.RUN_ACTIONS,
        "has_run": requests.has_run(session, row),
        "outcomes": list(OUTCOMES.items()),
        "day_fields": DAY_FIELDS,
    }


def typed(form: dict[str, str]) -> tuple[requests.Details, list[uuid.UUID]]:
    """The details the owner typed for one person, and the rows the guest
    confirmed (the ticked boxes). `InvalidInput` with its sentence for a field
    nobody can read; who the person is, is decided by `requests.verify`."""
    if any(len(form.get(name, "")) > FIELD_MAX for name in DETAIL_FIELDS):
        raise InvalidInput("a field is too long", say=TOO_MUCH)
    if any(requests.NUL in form.get(name, "") for name in DETAIL_FIELDS):
        # The postal code is compared in the database, which takes no NUL.
        raise InvalidInput("a field holds a NUL", say=BAD_SIGN)
    try:
        days = tuple(
            date.fromisoformat(text)
            for text in (form.get(name, "").strip() for name in DAY_FIELDS)
            if text
        )
    except ValueError:
        raise InvalidInput("a day is no date", say=BAD_DAY) from None
    phones = tuple(
        part.strip()
        for part in re.split(r"[,;\n]+", form.get("nummern", ""))
        if part.strip()
    )
    ticked = [key[len(CLAIM) :] for key in form if key.startswith(CLAIM)]
    if len(phones) > PHONES_MAX or len(ticked) > CLAIMS_MAX:
        raise InvalidInput("too many numbers or rows", say=TOO_MUCH)
    try:
        for phone in phones:
            normalize_phone(phone)
    except InvalidInput:
        raise InvalidInput("a number is unreadable", say=BAD_PHONE) from None
    try:
        claimed = [uuid.UUID(text) for text in ticked]
    except ValueError:
        raise InvalidInput("a claim is no id", say=requests.SAY_CLAIM) from None
    details = requests.Details(
        name=form.get("name", ""),
        days=days,
        street=form.get("strasse", "").strip() or None,
        house_number=form.get("hausnummer", "").strip() or None,
        postal_code=form.get("plz", "").strip() or None,
        phones=phones,
    )
    return details, claimed


def sent_back(form: dict[str, str]) -> dict[str, str]:
    """The typed details for the form shown again, each cut to what its field
    holds. Only into the answer of this request, which no cache keeps."""
    return {name: form.get(name, "")[:FIELD_MAX] for name in DETAIL_FIELDS}


def request_errors(faults: dict[str, str]) -> dict[str, str]:
    """The domain's faults of a new request by the inputs of its form."""
    return {REQUEST_INPUTS.get(key, key): say for key, say in faults.items()}


def words(exc: AppError) -> str:
    """A refusal in the owner's words; never the message, which is for a log."""
    return exc.say or FAILED


def _text(key: str, value: Any, zone: str) -> str:
    if value is None or value == "" or value == []:
        return DASH
    if isinstance(value, datetime):
        return f"{to_local(value, zone):{MOMENT}}"
    if isinstance(value, date):
        return f"{value:{DAY}}"
    if key.endswith("_cents") and isinstance(value, int):
        return euro(value)
    if key == "duration_seconds" and isinstance(value, int):
        return duration(value)
    if key == "items":
        return "; ".join(_item(line) for line in value)
    if key in CODES:
        return CODES[key].get(str(value), str(value))
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _item(line: dict[str, Any]) -> str:
    text = f"{line.get('quantity')} x {line.get('number') or ''} {line.get('dish')}"
    extras = [
        json.dumps(line["options"], ensure_ascii=False, default=str)
        if line.get("options")
        else "",
        str(line.get("note") or ""),
    ]
    more = ", ".join(extra for extra in extras if extra)
    return " ".join(f"{text} ({more})".split()) if more else " ".join(text.split())


def _labelled(
    row: dict[str, Any], fields: tuple[tuple[str, str], ...], zone: str
) -> list[tuple[str, str]]:
    return [(label, _text(key, row.get(key), zone)) for key, label in fields]


def found(
    copy: dict[str, Any], tenant: Tenant, claimed: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """`requests.access_copy` for the page: the person's rows part by part, the
    rows only the guest can confirm, and what stops an erasure. `claimed` are
    the rows the guest confirmed: they count as the person's now, and the page
    keeps their boxes ticked, so the next check sends them again and the owner
    sees which ones to name to the operator."""
    zone = tenant.timezone
    person = copy["copy"]
    sections = [
        {
            "title": title,
            "rows": [_labelled(row, fields, zone) for row in person[key]],
        }
        for key, title, fields in COPY
        if person.get(key)
    ]
    ask = copy["ask_the_guest"]
    ask_calls = [
        {
            "id": str(row["id"]),
            "fields": _labelled(row, CALL_FIELDS, zone),
            "recorded": bool(row.get("recorded")),
        }
        for row in ask["calls"]
    ]
    ask_callbacks = [
        {
            "id": str(row["id"]),
            "fields": _labelled(row, CALLBACK_FIELDS, zone),
        }
        for row in ask["callbacks"]
    ]
    confirmed = [
        {"id": str(row["id"]), "word": word, "fields": _labelled(row, fields, zone)}
        for key, word, fields in (
            ("calls", "Anruf", CALL_FIELDS),
            ("callbacks", "Rückruf", CALLBACK_FIELDS),
        )
        for row in person[key]
        if str(row["id"]) in claimed
    ]
    blocking = []
    for row in copy["open_rows"]:
        word, part, moment = OPEN_WORDS.get(row["table"], (row["table"], "", ""))
        match = next(
            (r for r in person.get(part, []) if r.get("id") == row["row_id"]), None
        )
        when = _text(moment, match.get(moment), zone) if match else DASH
        blocking.append(f"{word} vom {when}" if match else word)
    return {
        "sections": sections,
        "ask_calls": ask_calls,
        "ask_callbacks": ask_callbacks,
        "confirmed": confirmed,
        "objections": list(ask["recognition_objections"]),
        "blocking": blocking,
    }
