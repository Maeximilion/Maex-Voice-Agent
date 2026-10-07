"""Simulator console in the browser (T-2.5, docs/11 §gui): the text phone of `sim/` as a page.

Mounted only when `ENV=dev` (`mount_gui`). It writes into the same database as the
API, like `sim.cli`, so a booking made here shows up on the tablet. Logic stays in
`sim/session.py`; this module only keeps the open calls and renders them.
"""

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from api.core.errors import AppError
from api.core.logging import get_logger
from api.db import SessionLocal
from api.gui.router import _form, _require_htmx, templates
from sim.session import SimCall, Turn, resolve_tenant

router = APIRouter(prefix="/gui/dev", tags=["gui-dev"])
logger = get_logger("api.gui.dev")

TURN_FAILED = "Der Zug ist fehlgeschlagen, der Anruf wurde beendet. Bitte einen neuen Anruf starten."
NO_CALL = "Dieser Anruf ist nicht mehr offen. Bitte einen neuen Anruf starten."


@dataclass
class OpenCall:
    session: Session
    call: SimCall
    tenant_name: str
    turns: list[Turn] = field(default_factory=list)
    # A sentence and a hang-up can arrive together; they share one session.
    lock: threading.Lock = field(default_factory=threading.Lock)


# ponytail: open calls live in this process only, one worker, dev tool. A restart
# drops them (the call row stays open); move to the database if it ever matters.
OPEN_CALLS: dict[str, OpenCall] = {}
# Two tabs starting a call at once must not both pass the clean-up (Codex PR #236).
_START_LOCK = threading.Lock()


def _view(request: Request, entry: OpenCall | None, **extra):
    context = {"entry": entry, "call_id": entry and str(entry.call.call_id), **extra}
    # Always 200: htmx does not swap a 4xx, and the problem text with its
    # "Neuer Anruf" button would never be shown (Codex PR #236).
    return templates.TemplateResponse(request, "fragments/dev_anruf.html", context)


def _finish(call_id: str):
    """Hang up and forget the call; the browser may have lost it (reload, restart).

    The entry stays until the call row is closed: if the database fails here, a
    retry still finds the call and can finish it (Codex PR #236)."""
    entry = OPEN_CALLS[call_id]
    try:
        ended = entry.call.finish()
    except Exception:
        entry.session.rollback()
        raise
    del OPEN_CALLS[call_id]
    entry.session.close()
    return ended


@contextmanager
def _locked(call_id: str):
    """The open call under its own lock, or None. The hang-up may have won the
    lock while this request waited for it, hence the second look."""
    entry = OPEN_CALLS.get(call_id)
    if entry is None:
        yield None
        return
    with entry.lock:
        yield entry if OPEN_CALLS.get(call_id) is entry else None


def _drop(call_id: str) -> None:
    """Forget a call that cannot be finished; the page cannot reach it anyway."""
    OPEN_CALLS.pop(call_id).session.close()


@router.get("/console", response_class=HTMLResponse, include_in_schema=False)
def console(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dev/console.html", {})


@router.post(
    "/console/call",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def start(request: Request) -> HTMLResponse:
    with _START_LOCK:
        # A reloaded page has lost its call id: hang up what is still open instead
        # of keeping an unreachable call and its session (one user at a time).
        for stale, old in list(OPEN_CALLS.items()):
            with old.lock:
                if OPEN_CALLS.get(stale) is old:
                    try:
                        _finish(stale)
                    except Exception:
                        # Unreachable by the page anyway: drop it, or one broken
                        # call would block every new start until a restart.
                        logger.exception("stale console call dropped: %s", stale)
                        _drop(stale)
        session = SessionLocal()
        try:
            tenant = resolve_tenant(session, None)
            call = SimCall(session, tenant)
        except Exception as exc:
            session.close()
            if isinstance(exc, AppError):
                return _view(request, None, problem=exc.message)
            raise
        entry = OPEN_CALLS[str(call.call_id)] = OpenCall(session, call, tenant.name)
    return _view(request, entry)


@router.post(
    "/console/call/{call_id}/say",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def say(
    request: Request, call_id: str, form: dict[str, str] = Depends(_form)
) -> HTMLResponse:
    text = form.get("text", "").strip()
    with _locked(call_id) as entry:
        if entry is None:
            return _view(request, None, problem=NO_CALL)
        if not text:
            return _view(request, entry)
        try:
            turn = entry.call.say(text)
        except Exception:
            # A turn that failed half way may have committed tools and moved the
            # state: a retry would start from a state the guest never reached
            # (Codex PR #236). End the call instead and say so.
            logger.exception("console turn failed, ending call %s", call_id)
            entry.session.rollback()
            try:
                _finish(call_id)
            except Exception:
                logger.exception("console call could not be ended: %s", call_id)
                _drop(call_id)
            return _view(request, None, problem=TURN_FAILED)
        entry.turns.append(turn)
        # A closing stage is the end of the call: no sentence after the goodbye.
        return _view(request, entry, ended=_finish(call_id) if turn.ended else None)


@router.post(
    "/console/call/{call_id}/end",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def end(request: Request, call_id: str) -> HTMLResponse:
    with _locked(call_id) as entry:
        if entry is None:
            return _view(request, None, problem=NO_CALL)
        return _view(request, entry, ended=_finish(call_id))
