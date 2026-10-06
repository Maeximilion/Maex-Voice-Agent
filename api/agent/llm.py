"""Austauschbare Modell-Anbindung (docs/11 §agent).

T-2.1 legt nur die Schnittstelle und ein Testdouble an. Ein echtes Modell mit
Token-Zählung kommt mit T-2.4 gegen dieselbe `LLMClient`-Schnittstelle, damit
`agent/loop.py` sich dann nicht ändern muss.

T-2.4, first part: `ChatCompletionsLLM` is that real model. Token counting and
cost per call follow in the next part.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from api.config import settings


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMTurn:
    """Ein Modell-Zug ist genau eins von beidem: ein Satz an den Kunden (`say`)
    oder ein Tool-Aufruf. Nie beides, nie keins — sonst weiß `loop.py` nicht,
    ob es auf den Kunden wartet oder weitermacht.

    `state_patch` ist davon unabhängig und darf bei beidem mitkommen: gewöhnliche
    Gesprächsdetails (Name, Datum, Personenzahl, ...), die das Modell aus dem
    Kundenzug herausgehört hat. Ohne ihn hat das Modell keine Möglichkeit, so
    etwas in den kompakten Zustand (docs/05 §5) zu schreiben — es sieht beim
    nächsten Zug nur `state.to_prompt_json()` und den neuen Zug, nie den Verlauf,
    und würde sonst verstreute Angaben aus früheren Zügen wieder verlieren.

    `understanding_failure` ist ebenfalls unabhängig: der Feldname (z. B.
    `party_size`, `reserved_for`), an dem der Kunde gerade nicht verstanden
    wurde. `loop.py` zählt das über `agent/ladder.py` (docs/05 §2) und eskaliert
    selbst, wenn dieselbe Information dreimal die Stufe wechselt, ohne dass das
    Modell das selbst nachhalten müsste."""

    say: str | None = None
    tool_call: ToolCall | None = None
    state_patch: dict[str, Any] | None = None
    understanding_failure: str | None = None

    def __post_init__(self) -> None:
        if (self.say is None) == (self.tool_call is None):
            raise ValueError("LLMTurn braucht genau eins: say oder tool_call")


class LLMClient(Protocol):
    def next_turn(
        self, system_prompt: str, state_json: dict[str, Any], input_text: str
    ) -> LLMTurn: ...


class FakeLLM:
    """Testdouble für `loop.py`: spielt eine vorbereitete Zug-Liste ab, unabhängig
    vom Inhalt des Prompts. `sim/` (T-2.3) und `evals/` (T-5.1) bekommen bei Bedarf
    eigene, skriptfähige Fakes; dieser hier dient den Loop-Unit-Tests."""

    def __init__(self, turns: list[LLMTurn]):
        self._turns = list(turns)
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def next_turn(
        self, system_prompt: str, state_json: dict[str, Any], input_text: str
    ) -> LLMTurn:
        self.calls.append((system_prompt, state_json, input_text))
        if not self._turns:
            raise AssertionError("FakeLLM: keine weiteren Züge vorbereitet")
        return self._turns.pop(0)


class LLMError(Exception):
    """The model delivered no usable turn: unreachable, too slow, an HTTP error,
    or an answer outside the contract. `loop.py` hands the call to the team on
    it (CLAUDE.md §2 rule 5). The message never carries the model's text: it
    can hold what a guest said, and it ends up in the log."""


# The wire contract of `ChatCompletionsLLM`, appended to the system prompt on
# every call. German because the model reads it as part of the prompt, next to
# `prompts/system_vN.md`. `parse_turn` is its other half: change both together.
OUTPUT_FORMAT = """# Antwortformat
Antworte immer mit genau einem JSON-Objekt, ohne Text davor oder danach:
{"say": null, "tool": null, "args": {}, "slots": {}, "not_understood": null}
- Genau eins von beiden: `say` (dein Satz an den Gast) oder `tool` (der Name des Tools) mit `args`. Nie beides in einer Antwort.
- `slots`: was der Gast in diesem Zug genannt hat, nur Neues oder Geändertes. Im nächsten Zug steht es im Zustand unter `slots`; was du nicht einträgst, ist verloren.
- `not_understood`: der Name der Angabe, die du gerade nicht verstanden hast (zum Beispiel `party_size`), sonst null.
Die Eingabe ist entweder das, was der Gast gesagt hat, oder das Ergebnis deines letzten Tool-Aufrufs als JSON mit dem Feld `tool`."""


class _Envelope(BaseModel):
    say: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    slots: dict[str, Any] | None = None
    not_understood: str | None = None


def parse_turn(content: str) -> LLMTurn:
    """The model's answer as an `LLMTurn`, or `LLMError`. Strict on purpose: an
    answer with a sentence and a tool call is rejected, not picked from, because
    the sentence may be the readback and the tool `confirm` (CLAUDE.md §2 rule
    3). An empty string counts as absent; models fill unused fields with "".
    Tool name and arguments are not checked here: `dispatch.py` validates them
    and answers the model with `invalid_input`."""
    try:
        envelope = _Envelope.model_validate_json(content)
        tool = (envelope.tool or "").strip()
        return LLMTurn(
            say=(envelope.say or "").strip() or None,
            tool_call=ToolCall(name=tool, args=envelope.args or {}) if tool else None,
            state_patch=envelope.slots or None,
            understanding_failure=(envelope.not_understood or "").strip() or None,
        )
    except ValueError as exc:  # pydantic's ValidationError is one, LLMTurn raises one
        raise LLMError("model answer outside the contract") from exc


class ChatCompletionsLLM:
    """`LLMClient` over the chat completions API that local model servers and
    the hosted providers share. Which model answers is `LLM_BASE_URL`,
    `LLM_MODEL` and `LLM_API_KEY`: a local model on the workbench for tests, a
    hosted one in the EU for operation (docs/13 §0), without a code change.

    Stateless like the interface: every call sends the system prompt, the
    compact state and the one input, never a transcript (docs/05 §5)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,  # replaceable for tests
    ):
        self._model = model
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls) -> "ChatCompletionsLLM":
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMError("LLM_BASE_URL and LLM_MODEL must be set to use a model")
        return cls(
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
        )

    def next_turn(
        self, system_prompt: str, state_json: dict[str, Any], input_text: str
    ) -> LLMTurn:
        state = json.dumps(state_json, ensure_ascii=False, default=str)
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": f"{system_prompt}\n\n{OUTPUT_FORMAT}"},
                {"role": "user", "content": f"Zustand: {state}\nEingabe: {input_text}"},
            ],
            "response_format": {"type": "json_object"},
            # The same input gives the same turn, as far as the model allows:
            # an eval that is red must be red again on the next run.
            "temperature": 0,
        }
        try:
            response = self._http.post("chat/completions", json=body)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, LookupError, TypeError) as exc:
            raise LLMError(f"model call failed: {type(exc).__name__}") from exc
        if not isinstance(content, str):
            raise LLMError("model answer without text")
        return parse_turn(content)
