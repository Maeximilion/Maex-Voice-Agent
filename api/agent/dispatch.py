"""Tool name → `domain` function, with timing (docs/11 §agent).

The direct counterpart to `api/tools/*.py`, but without the HTTP detour:
`agent/` may use `domain/`, never `tools/` (docs/11 §2). Validation, error
translation and the entry in `calls.tool_calls` (docs/04 §Gemeinsame Regeln)
therefore run here once more, instead of going through FastAPI and the
middleware in `core/tool_log.py`.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from api.core.errors import AppError, InvalidInput
from api.core.ids import idempotency_key
from api.core.logging import get_logger, log
from api.core.tool_log import append_tool_call
from api.domain.callbacks import create_callback, transfer_to_team
from api.domain.confirm import confirm
from api.domain.customers.phone import normalize_phone
from api.domain.menu import get_item_details, search_menu
from api.domain.menu.items import card_format
from api.domain.menu.search import (
    CLEAR_MATCHES,
    allergy_question,
    position_parts,
    say_understood,
)
from api.domain.ordering import draft_order
from api.domain.reservations import check_slot, create_reservation
from api.domain.status import get_service_status
from api.schemas.callbacks import CreateCallbackRequest
from api.schemas.confirm import ConfirmRequest
from api.schemas.menu import ItemDetailsRequest, SearchMenuRequest, SearchResult
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


# Placeholder until the key from the validated request is known.
_PENDING = "pending"
_KEY_EXCLUDE = {"idempotency_key", "call_id", "tenant_id"}


def _with_derived_key[R: (CreateReservationRequest, DraftOrderRequest)](
    req: R, tool: str
) -> R:
    """The key from the **validated** request of the same call, never from the
    model. Built from every stored field: a changed note ("mit Hochstuhl") is
    a new draft. Built from the canonical form: `options: []` and an omitted
    field are the same, so a model retry does not create a second draft (open
    points from PR #127, T-4.10)."""
    canonical = json.dumps(
        _canonical(req).model_dump(mode="json", exclude=_KEY_EXCLUDE), sort_keys=True
    )
    key = idempotency_key(req.call_id, tool, canonical)
    return req.model_copy(update={"idempotency_key": key})


def _canonical[R: (CreateReservationRequest, DraftOrderRequest)](req: R) -> R:
    """Same details, same form: phone number in E.164, time in UTC - otherwise
    "07221 5551234" would be a second draft (review PR #139)."""
    if isinstance(req, CreateReservationRequest):
        return req.model_copy(
            update={
                "phone": _e164(req.phone),
                "reserved_for": req.reserved_for.astimezone(UTC),
            }
        )
    customer = req.customer.model_copy(update={"phone": _e164(req.customer.phone)})
    return req.model_copy(update={"customer": customer})


def _e164(phone: str) -> str:
    try:
        return normalize_phone(phone)
    except AppError:
        return phone


def _create_reservation(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    # The code decides the key, never the model (CLAUDE.md §2 rule 1): always
    # derived from the same inputs of the same call, so a model retry with
    # identical details does not book twice. There is no key from the model -
    # invented or reused, it would fetch someone else's transaction (Codex PR
    # #127, P1).
    rest = {k: v for k, v in args.items() if k != "idempotency_key"}
    req = _with_derived_key(
        CreateReservationRequest(
            call_id=call_id, tenant_id=tenant_id, idempotency_key=_PENDING, **rest
        ),
        "create_reservation",
    )
    return create_reservation(session, req, now=now)


def _confirm(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    # The key only goes into the log; still from the code, never from the model.
    key = idempotency_key(call_id, "confirm", args.get("entity"), args.get("entity_id"))
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
    # Wish for this part (T-4.10): "ohne Karotten", an option of the menu, ...
    wish: dict[str, Any] | None = None
    error_code: str | None = None
    say: str | None = None


class PositionsResult(BaseModel):
    match_type: str = "positions"
    positions: list[PositionResult]
    # Repeats at once what was clearly understood (Maxi, PR #127). Open parts
    # keep their own say; the agent asks about them one after the other.
    say: str | None = None


def _search_menu(
    session: Session,
    call_id: uuid.UUID,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    now: datetime | None,
) -> BaseModel:
    """One sentence, one position (docs/04 §search_menu): if the guest names
    several, the sentence is split here and each part is searched. Over HTTP,
    search_menu answers that with ambiguous; the agent gets all parts in one go
    instead. A part without a hit stays visible with error_code and say instead
    of silently dropping out."""
    req = SearchMenuRequest(call_id=call_id, tenant_id=tenant_id, **args)
    # Read the menu once per tool call, not per part (code review PR #155).
    card = card_format(session, tenant_id)
    parts = position_parts(session, tenant_id, req.query, now=now, card=card)
    if len(parts) <= 1:
        # Already split: search_menu does not check again (review PR #139).
        found = search_menu(
            session,
            tenant_id,
            req.query,
            req.max_results,
            now=now,
            split_check=False,
            card=card,
        )
        if _repeats(found):
            echo = say_understood(
                [(found.match_type, found.results[0], found.wish)], req.query
            )
            # An allergy is repeated as well; its sentence from the domain follows.
            return found.model_copy(update={"say": _join(echo, found.say)})
        return found
    positions = []
    understood = []
    # Mandatory sentences of the parts: "kann ich nicht anbieten" and the note
    # about the allergy belong in the answer's sentence, otherwise they would
    # be lost in a list (Codex PR #139, P1).
    notices: list[str] = []
    # Dishes for which "Wogegen?" is open: the first one is asked about, the
    # others follow one by one (Codex PR #139, P1).
    allergies: list[str] = []
    for part in parts:
        try:
            found = search_menu(
                session, tenant_id, part, req.max_results, now=now, card=card
            )
        except AppError as exc:
            positions.append(
                PositionResult(query=part, ok=False, error_code=exc.code, say=exc.say)
            )
            continue
        if _repeats(found):
            understood.append((found.match_type, found.results[0], found.wish))
        # Sold out has its own sentence in the part; here only the sentences
        # of the wishes, or "heute aus" would appear twice (review PR #139).
        sold_out = bool(found.results) and found.results[0].sold_out
        # Ambiguous: the choice comes first, asked in the part; the wish counts
        # afterwards. Otherwise the choice would appear twice, or "Wogegen?"
        # next to it (Codex and review PR #139).
        settled = not sold_out and found.match_type in CLEAR_MATCHES
        if (
            settled
            and found.wish is not None
            and found.wish.kind == "allergy"
            and not found.wish.ingredient
        ):
            allergies.append(found.results[0].name)
        elif (
            settled
            and found.wish is not None
            and found.wish.kind in ("unknown", "allergy", "open")
        ):
            notices.append(found.say or "")
        positions.append(
            PositionResult(
                query=part,
                ok=True,
                match_type=found.match_type,
                results=[hit.model_dump(mode="json") for hit in found.results],
                wish=found.wish.model_dump() if found.wish else None,
                say=found.say,
            )
        )
    return PositionsResult(
        positions=positions,
        say=_join(
            say_understood(understood, req.query),
            *notices,
            allergy_question(allergies),
        ),
    )


def _repeats(found: SearchResult) -> bool:
    """Is the hit repeated at once? Only a clear one that is not sold out; a
    wish the menu does not know has its own sentence (D8), an allergy is
    repeated as well (T-4.10)."""
    if found.match_type not in CLEAR_MATCHES or not found.results:
        return False
    if found.results[0].sold_out:
        return False
    if found.wish is not None:
        return found.wish.kind != "unknown"
    return found.say is None


def _join(*parts: str | None) -> str | None:
    joined = " ".join(p for p in parts if p)
    return joined or None


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
    # As with create_reservation: the key comes from the details of the same
    # call. A model retry with the same positions does not create a second
    # draft; a correction (other quantity, other option) gives a new draft
    # with a new readback.
    rest = {k: v for k, v in args.items() if k != "idempotency_key"}
    # As with create_reservation, never the model's key (Codex PR #127).
    req = _with_derived_key(
        DraftOrderRequest(
            call_id=call_id, tenant_id=tenant_id, idempotency_key=_PENDING, **rest
        ),
        "draft_order",
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
        # Like `get_db` in `api/db.py` on an HTTP error: here the session lives
        # for the whole call instead of being fresh per tool call, and a
        # rollback keeps an uncommitted remainder of a failed domain call from
        # being carried into the next tool call.
        session.rollback()
        outcome = ToolResult(ok=False, say=exc.say, error_code=exc.code)
        error_code = exc.code

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    entry = {"name": name, "duration_ms": duration_ms, "ok": outcome.ok}
    if error_code:
        entry["error_code"] = error_code
    try:
        append_tool_call(session, str(call_id), str(tenant_id), entry)
    except Exception:  # noqa: BLE001 - logging must never break the answer
        log(
            logger,
            logging.WARNING,
            "tool_calls nicht geschrieben",
            call_id=str(call_id),
        )
    return outcome
