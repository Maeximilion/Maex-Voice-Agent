"""Austauschbare Modell-Anbindung (docs/11 §agent).

T-2.1 legt nur die Schnittstelle und ein Testdouble an. Ein echtes Modell mit
Token-Zählung kommt mit T-2.4 gegen dieselbe `LLMClient`-Schnittstelle, damit
`agent/loop.py` sich dann nicht ändern muss.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMTurn:
    """Ein Modell-Zug ist genau eins von beidem: ein Satz an den Kunden (`say`)
    oder ein Tool-Aufruf. Nie beides, nie keins — sonst weiß `loop.py` nicht,
    ob es auf den Kunden wartet oder weitermacht."""

    say: str | None = None
    tool_call: ToolCall | None = None

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
