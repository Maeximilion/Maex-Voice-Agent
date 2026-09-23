"""Tool-Name → `domain`-Funktion, mit Zeitmessung (docs/11 §agent).

Die direkte Entsprechung zu `api/tools/*.py`, aber ohne HTTP-Umweg: `agent/` darf
`domain/` benutzen, nie `tools/` (docs/11 §2). Validierung, Fehlerübersetzung und
der Eintrag in `calls.tool_calls` (docs/04 §Gemeinsame Regeln) laufen deshalb hier
noch einmal, statt über FastAPI und `core/tool_log.py`s Middleware zu gehen.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from api.core.errors import AppError, InvalidInput
from api.core.ids import idempotency_key
from api.core.logging import get_logger, log
from api.core.tool_log import append_tool_call
from api.domain.callbacks import create_callback, transfer_to_team
from api.domain.confirm import confirm
from api.domain.menu import get_item_details, search_menu
from api.domain.menu.search import CLEAR_MATCHES, position_parts, say_understood
from api.domain.ordering import draft_order
from api.domain.reservations import check_slot, create_reservation
from api.domain.status import get_service_status
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.menu import ItemDetailsRequest, SearchMenuRequest
from api.schemas.orders import DraftOrderRequest
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


class PositionResult(BaseModel):
    """Ein Teil eines Satzes mit mehreren Positionen, je Teil gesucht."""

    query: str
    ok: bool
    match_type: str | None = None
    results: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    say: str | None = None


class PositionsResult(BaseModel):
    match_type: str = "positions"
    positions: list[PositionResult]
    # Wiederholt sofort, was eindeutig verstanden wurde (Maxi, PR #127). Offene
    # Teile behalten ihr eigenes say, der Agent fragt sie nacheinander.
    say: str | None = None


def _search_menu(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    """Ein Satz, eine Position (docs/04 §search_menu): nennt der Gast mehrere, wird
    der Satz hier zerlegt und je Teil gesucht. Ueber HTTP antwortet search_menu
    darauf mit ambiguous; der Agent bekommt stattdessen alle Teile in einem Zug.
    Ein Teil ohne Treffer bleibt mit error_code und say sichtbar, statt still
    wegzufallen."""
    req = SearchMenuRequest(call_id=call_id, tenant_id=tenant_id, **args)
    parts = position_parts(session, tenant_id, req.query, now=now)
    if len(parts) <= 1:
        found = search_menu(session, tenant_id, req.query, req.max_results, now=now)
        hit = found.results[0] if found.results else None
        if found.say is None and found.match_type in CLEAR_MATCHES and hit:
            echo = say_understood([(found.match_type, hit)], req.query)
            return found.model_copy(update={"say": echo})
        return found
    positions = []
    understood = []
    for part in parts:
        try:
            found = search_menu(session, tenant_id, part, req.max_results, now=now)
        except AppError as exc:
            positions.append(
                PositionResult(query=part, ok=False, error_code=exc.code, say=exc.say)
            )
            continue
        hit = found.results[0] if found.results else None
        if found.match_type in CLEAR_MATCHES and hit and not hit.sold_out:
            understood.append((found.match_type, hit))
        positions.append(
            PositionResult(
                query=part,
                ok=True,
                match_type=found.match_type,
                results=[hit.model_dump(mode="json") for hit in found.results],
                say=found.say,
            )
        )
    return PositionsResult(
        positions=positions, say=say_understood(understood, req.query)
    )


def _item_details(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    req = ItemDetailsRequest(call_id=call_id, tenant_id=tenant_id, **args)
    return get_item_details(
        session, tenant_id, req.menu_item_id, req.allergen_question, now=now
    )


def _draft_order(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    # Wie bei create_reservation: der Schluessel kommt aus den Angaben desselben
    # Anrufs. Ein Modell-Retry mit denselben Positionen legt keinen zweiten
    # Entwurf an; eine Korrektur (andere Menge, andere Option) ergibt einen
    # neuen Entwurf mit neuem readback.
    rest = {k: v for k, v in args.items() if k != "idempotency_key"}
    key = args.get("idempotency_key") or idempotency_key(
        call_id, "draft_order", json.dumps(rest, sort_keys=True, default=str)
    )
    req = DraftOrderRequest(
        call_id=call_id, tenant_id=tenant_id, idempotency_key=key, **rest
    )
    return draft_order(session, req, now=now)


TOOLS: dict[str, Adapter] = {
    "get_service_status": _status,
    "check_slot": _check_slot,
    "create_reservation": _create_reservation,
    "confirm": _confirm,
    "create_callback": _create_callback,
    "transfer_to_team": _transfer,
    "search_menu": _search_menu,
    "get_item_details": _item_details,
    "draft_order": _draft_order,
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
