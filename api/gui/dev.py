"""Simulator console in the browser (T-2.5, docs/11 §gui): the text phone of `sim/` as a page.

Mounted only when `ENV=dev` (`mount_gui`). It writes into the same database as the
API, like `sim.cli`, so a booking made here shows up on the tablet. Logic stays in
`sim/session.py`; this module only keeps the open calls and renders them.
"""

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from api.core.errors import AppError
from api.db import SessionLocal
from api.gui.router import _form, _require_htmx, templates
from sim.session import SimCall, Turn, resolve_tenant

router = APIRouter(prefix="/gui/dev", tags=["gui-dev"])

NO_CALL = "Dieser Anruf ist nicht mehr offen. Bitte einen neuen Anruf starten."


@dataclass
class OpenCall:
    session: Session
    call: SimCall
    tenant_name: str
    turns: list[Turn] = field(default_factory=list)


# ponytail: open calls live in this process only, one worker, dev tool. A restart
# drops them (the call row stays open); move to the database if it ever matters.
OPEN_CALLS: dict[str, OpenCall] = {}


def _view(request: Request, entry: OpenCall | None, *, status: int = 200, **extra):
    context = {"entry": entry, "call_id": entry and str(entry.call.call_id), **extra}
    return templates.TemplateResponse(
        request, "fragments/dev_anruf.html", context, status
    )


def _entry(call_id: str) -> OpenCall:
    entry = OPEN_CALLS.get(call_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=NO_CALL)
    return entry


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
    session = SessionLocal()
    try:
        tenant = resolve_tenant(session, None)
        call = SimCall(session, tenant)
    except AppError as exc:
        session.close()
        return _view(request, None, status=503, problem=exc.message)
    except Exception:
        session.close()
        raise
    OPEN_CALLS[str(call.call_id)] = OpenCall(session, call, tenant.name)
    return _view(request, OPEN_CALLS[str(call.call_id)])


@router.post(
    "/console/call/{call_id}/say",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def say(
    request: Request, call_id: str, form: dict[str, str] = Depends(_form)
) -> HTMLResponse:
    entry = _entry(call_id)
    text = form.get("text", "").strip()
    if text:
        entry.turns.append(entry.call.say(text))
    return _view(request, entry)


@router.post(
    "/console/call/{call_id}/end",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(_require_htmx)],
)
def end(request: Request, call_id: str) -> HTMLResponse:
    entry = _entry(call_id)
    try:
        ended = entry.call.finish()
    finally:
        OPEN_CALLS.pop(call_id, None)
        entry.session.close()
    return _view(request, entry, ended=ended)
