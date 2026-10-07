"""Gemeinsame Mechanik von `cli.py` und `replay.py`: Anruf öffnen, Züge fahren, schließen.

Der Unterschied zwischen Terminal und Transkript ist nur, woher die Kundensätze
kommen. Alles andere - Anruf-Zeile, Gesprächszustand, Tool-Protokoll, Abschluss -
ist identisch und steht deshalb hier, statt zweimal.
"""

import argparse
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.agent.llm import ChatCompletionsLLM, LLMClient, Usage, cost_cents
from api.agent.loop import ConversationLoop
from api.agent.outcome import CLOSING_STAGES, call_outcome
from api.agent.prompt import build_system_prompt
from api.agent.state import initial_state
from api.core.errors import NotFound
from api.core.logging import get_logger, log
from api.core.time import utcnow
from api.domain.calls import end_call, start_call
from api.domain.menu.items import active_numbers
from api.models import Call, Tenant
from api.schemas.calls import CallEnded, EndCallRequest, StartCallRequest
from sim.scripted_llm import ScriptedLLM
from sim.scripted_order import MenuNumbers

logger = get_logger("sim.session")


@dataclass
class Turn:
    """Ein Zug samt Zustand **nach** ihm. Der Zustand wird als Kopie festgehalten,
    nicht als Verweis: `ConversationState` ist ein Objekt, das der Loop weiter
    veraendert, und eine spaeter gedruckte Zeile zeigte sonst den Stand vom
    Gespraechsende statt den von damals."""

    customer: str
    say: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    ended: bool = False


def resolve_tenant(session: Session, name: str | None) -> Tenant:
    """Mandant über den Namen, sonst der einzige vorhandene. Ohne Mandant hat der
    Anruf keinen Betrieb, zu dem er gehört - dann fehlt der Seed (`make seed`)."""
    if name:
        tenant = session.scalar(select(Tenant).where(Tenant.name == name))
        if tenant is None:
            raise NotFound(f"Mandant '{name}' nicht gefunden")
        return tenant
    tenants = session.scalars(select(Tenant).order_by(Tenant.created_at)).all()
    if not tenants:
        raise NotFound(
            "Kein Mandant in der Datenbank: zuerst 'make seed' laufen lassen"
        )
    return tenants[0]


def menu_numbers(session: Session, tenant_id: uuid.UUID) -> MenuNumbers:
    """The card numbers of the active menu for the scripted model: it tells an
    order from an ingredient by the menu, not by the import grammar (Codex PR
    #155, P2)."""
    return MenuNumbers.from_items(active_numbers(session, tenant_id))


SCRIPTED = "scripted"


def call_time(text: str) -> datetime:
    """`--now` of the text phone. Without an offset it is refused here, at the
    argument, instead of failing inside the first turn (`to_local`)."""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("offset missing, e.g. 2026-09-15T18:00+02:00")
    return value


def make_llm(
    model: str, *, now: datetime, timezone: str, menu: Callable[[], MenuNumbers]
) -> LLMClient:
    """`scripted` is the rule-based stand-in. Any other name is a model on the
    server of `LLM_BASE_URL` (`--model qwen3:14b`); without a server that is an
    `LLMError`, never a silent fall back to the script."""
    if model == SCRIPTED:
        return ScriptedLLM(now=now, timezone=timezone, menu=menu)
    return ChatCompletionsLLM.from_settings(model)


class SimCall:
    """Ein Anruf im Text-Telefon: hält Anruf-Zeile, Zustand und Loop zusammen."""

    def __init__(
        self,
        session: Session,
        tenant: Tenant,
        *,
        llm: LLMClient | None = None,
        model: str = SCRIPTED,
        now: datetime | None = None,
        external_session_id: str | None = None,
        caller_id: str | None = None,
    ):
        self._session = session
        # Fester Zeitpunkt (`--now`) haelt einen Lauf reproduzierbar; ohne ihn laeuft
        # das Gespraech auf der echten Uhr und dauert so lange, wie es dauert.
        self._fixed_now = now
        self._now = now or utcnow()
        self._tenant = tenant
        # Before the call row is written: a model that is not usable raises
        # here and must not leave an open call behind.
        self._llm = llm or make_llm(
            model,
            now=self._now,
            timezone=tenant.timezone,
            menu=lambda: menu_numbers(session, tenant.id),
        )
        started = start_call(
            session,
            StartCallRequest(
                tenant_id=tenant.id,
                external_session_id=external_session_id
                or f"sim-{uuid.uuid4().hex[:8]}",
                caller_id=caller_id,
            ),
            now=self._now,
        )
        self.call_id = started.call_id
        self.state = initial_state(self.call_id, tenant.id, caller_id=caller_id)
        # With the tool reference: a real model learns the tools from the
        # prompt, the stand-in reads none of it.
        self._loop = ConversationLoop(
            session, self._llm, build_system_prompt(tools=True), now=self._now
        )
        self._logged = 0
        self._usage_logged = False

    def say(self, text: str) -> Turn:
        result = self._loop.run_turn(self.state, text)
        return Turn(
            customer=text,
            say=result.say,
            tools=self._new_tool_calls(),
            state=self.state.to_prompt_json(),
            ended=result.ended or self.state.stage in CLOSING_STAGES,
        )

    def finish(self) -> CallEnded:
        """Ende ist jetzt, nicht der Gespraechsbeginn: mit dem Startzeitpunkt stuenden
        in jedem Anruf aus dem Terminal 0 Sekunden und die Gespraechsdauer waere als
        Kennzahl wertlos (Codex-Review PR #104, P2)."""
        outcome, intent = call_outcome(self.state)
        usage = self.usage
        cost = cost_cents(usage)
        # Once per call: a repeated `finish` writes nothing (`end_call` is
        # idempotent), and a second line would double the tokens of this call
        # for whoever adds the lines up (Codex PR #211, P2).
        if usage.model is not None and not self._usage_logged:
            self._usage_logged = True
            log(
                logger,
                logging.INFO,
                "model usage",
                call_id=str(self.call_id),
                **asdict(usage),
                cost_cents=cost,
            )
        return end_call(
            self._session,
            EndCallRequest(
                call_id=self.call_id,
                tenant_id=self._tenant.id,
                outcome=outcome,
                intent=intent,
                cost_cents=cost,
                model=usage.model,
            ),
            now=self._fixed_now or utcnow(),
        )

    @property
    def usage(self) -> Usage:
        """What the model of this call used. Only a real model counts tokens
        (`ChatCompletionsLLM.usage`); a stand-in has none, and its call gets
        neither a model nor a cost."""
        usage: Usage = getattr(self._llm, "usage", None) or Usage()
        if not usage.requests:
            # The caller hung up before the model was asked once: the call log
            # must not name a model that never ran (Codex PR #211, P2).
            return Usage()
        return usage

    def usage_line(self) -> str | None:
        """Model, requests, tokens and cost for the terminal, or None on the
        stand-in. The text phone configures no logging, so the log line of
        `finish` is not shown there (Codex PR #211, P2)."""
        usage = self.usage
        if usage.model is None:
            return None
        cost = cost_cents(usage)
        requests = f"{usage.requests} request{'s' if usage.requests != 1 else ''}"
        unmetered = (
            f", {usage.unmetered} without token numbers" if usage.unmetered else ""
        )
        return (
            f"model {usage.model}: {requests}{unmetered}, "
            f"{usage.prompt_tokens} + {usage.completion_tokens} tokens, "
            + ("cost unknown" if cost is None else f"{cost} cents")
        )

    def _new_tool_calls(self) -> list[dict[str, Any]]:
        """Die Einträge kommen aus `calls.tool_calls`, nicht aus einer eigenen Zählung:
        angezeigt wird damit genau das, was auch im Anruf-Log steht (docs/04 §Gemeinsame
        Regeln). Spaltenabfrage statt `session.get`, damit der per SQL angehängte
        Eintrag nicht aus dem Identity Map kommt und veraltet ist."""
        entries = (
            self._session.execute(
                select(Call.tool_calls).where(Call.id == self.call_id)
            ).scalar_one()
            or []
        )
        fresh = entries[self._logged :]
        self._logged = len(entries)
        return fresh


def render_turn(turn: Turn) -> str:
    """Eine Zeile je Ereignis: Kundensatz, Tool-Aufrufe mit Dauer, Antwort, Zustand."""
    lines = [f"Kunde: {turn.customer}"]
    lines += [
        "  tool {name} {ok} {duration_ms} ms{error}".format(
            name=entry.get("name"),
            ok="ok" if entry.get("ok") else "fehler",
            duration_ms=entry.get("duration_ms"),
            error=f" [{entry['error_code']}]" if entry.get("error_code") else "",
        )
        for entry in turn.tools
    ]
    lines += [f"Agent: {say}" for say in turn.say]
    lines.append(f"  Zustand: {turn.state}")
    return "\n".join(lines)
