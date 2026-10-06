"""Austauschbare Modell-Anbindung (docs/11 §agent).

T-2.1 legt nur die Schnittstelle und ein Testdouble an. Ein echtes Modell mit
Token-Zählung kommt mit T-2.4 gegen dieselbe `LLMClient`-Schnittstelle, damit
`agent/loop.py` sich dann nicht ändern muss.

T-2.4: `ChatCompletionsLLM` is that real model. It counts the tokens it uses
(`Usage`); `cost_cents` prices them for the call log.
"""

import json
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class Usage:
    """What one client has used since it was created. One client serves one
    call, so this is the usage of that call. `model` is None for a stand-in
    that is no model. `unmetered` counts the requests whose tokens are unknown:
    a failed one, since what a provider bills for it is open, and an answer
    without usable token numbers. The token sums cover the other requests."""

    model: str | None = None
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    unmetered: int = 0


def cost_cents(usage: Usage) -> int | None:
    """Model cost of a call in whole cents, or None when it is unknown: no
    model, no request, a price missing in the settings, or one request whose
    tokens are unknown - the sum is then a part, and a part priced as the whole
    would understate the call (Codex PR #211, P1). Unknown is never 0.

    Rounded up, so a sum over calls never undercuts a budget.
    ponytail: whole cents per call overstate a cheap model (0.3 cents count as
    1). The exact tokens are in the log line "model usage"; a token column on
    `calls` once a KPI needs the exact cost (T-8.3)."""
    price_in = settings.llm_input_cents_per_mtok
    price_out = settings.llm_output_cents_per_mtok
    if usage.model is None or price_in is None or price_out is None:
        return None
    if not usage.requests or usage.unmetered:
        return None
    total = usage.prompt_tokens * price_in + usage.completion_tokens * price_out
    return -(-total // 1_000_000)


class _Envelope(BaseModel):
    say: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    slots: dict[str, Any] | None = None
    not_understood: str | None = None


def parse_turn(content: str) -> LLMTurn:
    """The model's answer as an `LLMTurn`, or `LLMError`.

    A sentence next to a tool call: the tool call wins and the sentence is
    dropped (Maxi, 06.10.2026). A real model does both on its first turn
    ("Gerne prüfe ich das" plus `get_service_status`) whatever the format
    says, and rejecting it ended every call in a handover. The model speaks
    again once it has the tool result. The case this was strict about, a
    readback with `confirm` in the same breath, is stopped by the core:
    `guards.py` lets `confirm` through only after a yes to a draft that was
    read back in an earlier turn (CLAUDE.md §2 rule 3).

    An empty string counts as absent; models fill unused fields with "".
    Tool name and arguments are not checked here: `dispatch.py` validates them
    and answers the model with `invalid_input`."""
    try:
        envelope = _Envelope.model_validate_json(content)
        tool = (envelope.tool or "").strip()
        return LLMTurn(
            say=None if tool else (envelope.say or "").strip() or None,
            tool_call=ToolCall(name=tool, args=envelope.args or {}) if tool else None,
            state_patch=_named(envelope.slots),
            understanding_failure=(envelope.not_understood or "").strip() or None,
        )
    except ValueError as exc:  # pydantic's ValidationError is one, LLMTurn raises one
        raise LLMError("model answer outside the contract") from exc


def _named(slots: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only what the guest named. A model that fills fields it has nothing for
    (`"phone": null`, `"guest_name": ""`) would erase what is already known,
    the caller ID first (Codex PR #208, P2). 0 and false are answers."""
    named = {
        key: value
        for key, value in (slots or {}).items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }
    return named or None


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
        self.usage = Usage(model=model)
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, model: str | None = None) -> "ChatCompletionsLLM":
        """`model` names another model on the server of the settings, as
        `--model` does in the text phone and the eval runner."""
        model = model or settings.llm_model
        if not settings.llm_base_url or not model:
            raise LLMError("LLM_BASE_URL and LLM_MODEL must be set to use a model")
        return cls(
            settings.llm_base_url,
            model,
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
            # A model that runs on is cut off here instead of being read to the
            # guest; the cut answer is no valid JSON and ends as `LLMError`.
            "max_tokens": settings.llm_max_output_tokens,
            # The same input gives the same turn, as far as the model allows:
            # an eval that is red must be red again on the next run.
            "temperature": 0,
        }
        if settings.llm_reasoning_effort:
            # A reasoning model thinks until the output limit and answers
            # nothing unless told otherwise (docs/18 §5). Only sent when set: a
            # server that does not know the parameter may reject the request.
            body["reasoning_effort"] = settings.llm_reasoning_effort
        # Unmetered until the answer brings its token numbers (`_count`).
        self.usage = replace(
            self.usage,
            requests=self.usage.requests + 1,
            unmetered=self.usage.unmetered + 1,
        )
        try:
            response = self._http.post("chat/completions", json=body)
            response.raise_for_status()
            payload = response.json()
            # Before the answer is judged: one outside the contract cost the same.
            self._count(payload)
            content = payload["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, LookupError, TypeError) as exc:
            raise LLMError(f"model call failed: {type(exc).__name__}") from exc
        if not isinstance(content, str):
            raise LLMError("model answer without text")
        return parse_turn(content)

    def _count(self, payload: object) -> None:
        """Adds the tokens the server reports. Both numbers or nothing: half a
        count would read like a whole one. Missing or odd numbers add nothing,
        they are never estimated, and the request stays unmetered.

        Also takes the model name the server answered with: a hosted endpoint
        resolves an alias to a dated model or routes to a fallback, and the
        call log should name what really ran (Codex PR #211, P2).
        ponytail: the last answer wins and the price stays the configured one;
        per-model prices when a provider really mixes models within a call."""
        answered = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(answered, str) and answered:
            self.usage = replace(self.usage, model=answered)
        reported = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(reported, dict):
            return
        tokens = (reported.get("prompt_tokens"), reported.get("completion_tokens"))
        if not all(type(n) is int and n >= 0 for n in tokens):
            return
        self.usage = replace(
            self.usage,
            prompt_tokens=self.usage.prompt_tokens + tokens[0],
            completion_tokens=self.usage.completion_tokens + tokens[1],
            unmetered=self.usage.unmetered - 1,
        )
