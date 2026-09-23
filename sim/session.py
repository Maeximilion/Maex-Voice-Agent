"""Gemeinsame Mechanik von `cli.py` und `replay.py`: Anruf öffnen, Züge fahren, schließen.

Der Unterschied zwischen Terminal und Transkript ist nur, woher die Kundensätze
kommen. Alles andere - Anruf-Zeile, Gesprächszustand, Tool-Protokoll, Abschluss -
ist identisch und steht deshalb hier, statt zweimal.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.agent.llm import LLMClient
from api.agent.loop import ConversationLoop
from api.agent.prompt import build_system_prompt
from api.agent.state import initial_state
from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.calls import end_call, start_call
from api.models import Call, Tenant
from api.schemas.calls import (
    CallEnded,
    EndCallRequest,
    Intent,
    Outcome,
    StartCallRequest,
)
from sim.scripted_llm import ScriptedLLM

# Der Ausgang des Anrufs folgt dem Gesprächszustand, nicht dem Gefühl des Modells
# (docs/03 §calls). Alles, was weder bestätigt noch übergeben noch als Rückruf
# notiert wurde, ist ein abgebrochener Anruf.
OUTCOME_BY_STAGE: dict[str, Outcome] = {
    "confirmed": "completed",
    "transferred": "transferred",
    "callback": "callback",
}
DEFAULT_OUTCOME: Outcome = "abandoned"
INTENTS = frozenset(get_args(Intent))

# Nach diesen Zuständen ist das Gespräch zu Ende; weiterreden hieße, den Kunden
# nach der Verabschiedung noch einmal anzusprechen.
CLOSING_STAGES = frozenset({"confirmed", "transferred", "callback", "ended"})


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


class SimCall:
    """Ein Anruf im Text-Telefon: hält Anruf-Zeile, Zustand und Loop zusammen."""

    def __init__(
        self,
        session: Session,
        tenant: Tenant,
        *,
        llm: LLMClient | None = None,
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
        self._loop = ConversationLoop(
            session,
            llm or ScriptedLLM(now=self._now, timezone=tenant.timezone),
            build_system_prompt(),
            now=self._now,
        )
        self._logged = 0

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
        outcome = OUTCOME_BY_STAGE.get(self.state.stage, DEFAULT_OUTCOME)
        intent = self.state.intent if self.state.intent in INTENTS else None
        return end_call(
            self._session,
            EndCallRequest(
                call_id=self.call_id,
                tenant_id=self._tenant.id,
                outcome=outcome,
                intent=intent,
            ),
            now=self._fixed_now or utcnow(),
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
