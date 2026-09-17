"""Tool-Name → `domain`-Funktion, mit Zeitmessung (docs/11 §agent).

Die direkte Entsprechung zu `api/tools/*.py`, aber ohne HTTP-Umweg: `agent/` darf
`domain/` benutzen, nie `tools/` (docs/11 §2). Validierung, Fehlerübersetzung und
der Eintrag in `calls.tool_calls` (docs/04 §Gemeinsame Regeln) laufen deshalb hier
noch einmal, statt über FastAPI und `core/tool_log.py`s Middleware zu gehen.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from api.core.errors import AppError, InvalidInput
from api.core.ids import idempotency_key
from api.core.logging import get_logger, log
from api.core.tool_log import append_tool_call
from api.domain.callbacks import create_callback, transfer_to_team
from api.domain.confirm import confirm
from api.domain.reservations import check_slot, create_reservation
from api.domain.status import get_service_status
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.reservations import CheckSlotRequest, CreateReservationRequest
from api.schemas.transfer import TransferToTeamRequest

logger = get_logger("api.agent.dispatch")

Adapter = Callable[
    [Session, uuid.UUID, uuid.UUID, dict[str, Any], datetime | None], BaseModel
]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    say: str | None = None
    error_code: str | None = None


def _status(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    return get_service_status(session, tenant_id, now=now)


def _check_slot(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    req = CheckSlotRequest(call_id=call_id, tenant_id=tenant_id, **args)
    return check_slot(session, req.tenant_id, req.reserved_for, req.party_size, now=now)


def _create_reservation(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    # Der Schlüssel entscheidet Code, nie das Modell (CLAUDE.md §2 Regel 1): ohne
    # eigenen Schlüssel vom Aufrufer aus denselben Eingaben desselben Anrufs
    # abgeleitet, damit ein Modell-Retry mit identischen Angaben nicht doppelt bucht.
    key = args.get("idempotency_key") or idempotency_key(
        call_id,
        "create_reservation",
        args.get("guest_name"),
        args.get("phone"),
        args.get("party_size"),
        args.get("reserved_for"),
    )
    rest = {k: v for k, v in args.items() if k != "idempotency_key"}
    req = CreateReservationRequest(
        call_id=call_id, tenant_id=tenant_id, idempotency_key=key, **rest
    )
    return create_reservation(session, req, now=now)


def _confirm(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    key = args.get("idempotency_key") or idempotency_key(
        call_id, "confirm", args.get("entity"), args.get("entity_id")
    )
    rest = {k: v for k, v in args.items() if k != "idempotency_key"}
    req = ConfirmRequest(
        call_id=call_id, tenant_id=tenant_id, idempotency_key=key, **rest
    )
    return confirm(session, req)


def _create_callback(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    req = CreateCallbackRequest(call_id=call_id, tenant_id=tenant_id, **args)
    return create_callback(session, req, now=now)


def _transfer(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    req = TransferToTeamRequest(call_id=call_id, tenant_id=tenant_id, **args)
    return transfer_to_team(session, req, now=now)


TOOLS: dict[str, Adapter] = {
    "get_service_status": _status,
    "check_slot": _check_slot,
    "create_reservation": _create_reservation,
    "confirm": _confirm,
    "create_callback": _create_callback,
    "transfer_to_team": _transfer,
}


def dispatch(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str,
    args: dict[str, Any],
    *,
    now: datetime | None = None,
) -> ToolResult:
    started = time.perf_counter()
    adapter = TOOLS.get(name)
    try:
        if adapter is None:
            raise InvalidInput(f"unbekanntes Tool: {name}")
        try:
            result = adapter(session, call_id, tenant_id, args, now)
        except ValidationError as exc:
            raise InvalidInput(str(exc)) from exc
        outcome = ToolResult(
            ok=True,
            data=result.model_dump(mode="json"),
            say=getattr(result, "say", None),
        )
        error_code: str | None = None
    except AppError as exc:
        # Wie `api/db.py`s `get_db` bei einem HTTP-Fehler: die Sitzung lebt hier über
        # den ganzen Anruf weiter statt je Tool-Aufruf frisch zu sein, ein Rollback
        # verhindert, dass ein nicht committeter Rest eines fehlgeschlagenen
        # Fach-Aufrufs beim nächsten Tool-Aufruf mit hochgezogen wird.
        session.rollback()
        outcome = ToolResult(ok=False, say=exc.say, error_code=exc.code)
        error_code = exc.code

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    entry = {"name": name, "duration_ms": duration_ms, "ok": outcome.ok}
    if error_code:
        entry["error_code"] = error_code
    try:
        append_tool_call(session, str(call_id), str(tenant_id), entry)
    except Exception:  # noqa: BLE001 - Protokoll darf die Antwort nie kippen
        log(
            logger,
            logging.WARNING,
            "tool_calls nicht geschrieben",
            call_id=str(call_id),
        )
    return outcome
