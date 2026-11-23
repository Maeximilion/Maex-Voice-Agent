"""`LLMClient` over the Messages API of Claude models (T-5.3, docs/18 §3).

The same wire contract as `ChatCompletionsLLM` in `agent/llm.py`: one system
prompt with the answer format, one user message with the compact state and the
one input, a JSON envelope back that `parse_turn` reads. Only the transport
differs, so `agent/loop.py`, the guards and the evals do not see which of the
two answered.

Plain `httpx` like `ChatCompletionsLLM`, not the vendor SDK: the SDK brings
`httpx2`, and once that is installed Starlette's test client switches to it and
changes how every test client keeps cookies (two dashboard tests broke, PR #916).

`client_from_settings` picks the client by the model's name: a `claude-*` model
goes here, every other name to the chat completions server of `LLM_BASE_URL`.
That is how `--model claude-haiku-4-5` works in the text phone and the eval
runner without a second switch.
"""

import json
from dataclasses import replace
from typing import Any

import httpx

from api.agent.llm import (
    ORDER_FORMAT,
    OUTPUT_FORMAT,
    ChatCompletionsLLM,
    LLMError,
    LLMTurn,
    Usage,
    parse_turn,
)
from api.config import settings

API_URL = "https://api.anthropic.com/v1/"
# The version without `temperature`, `top_p` and `top_k`; none is sent here.
API_VERSION = "2026-10-21"
CLAUDE_PREFIX = "claude-"
# `LLM_REASONING_EFFORT` for a Claude model: "none" turns thinking off, a level
# is sent as `output_config.effort`. Haiku 4.5 takes no level (the API answers
# 400), so its runs leave the setting empty or at "none".
NO_THINKING = "none"


class MessagesLLM:
    """A Claude model as the conversation core's model. Stateless like the
    interface: every turn sends the system prompt, the state and the one input,
    never a transcript (docs/05 §5)."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        effort: str = "",
        transport: httpx.BaseTransport | None = None,  # replaceable for tests
    ):
        self._model = model
        self._timeout = timeout
        self._effort = effort
        self.usage = Usage(model=model)
        self._http = httpx.Client(
            base_url=API_URL,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, model: str | None = None) -> "MessagesLLM":
        model = model or settings.llm_model
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY must be set to use a Claude model")
        llm = cls(
            model,
            settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
            effort=settings.llm_reasoning_effort,
        )
        # The prices of the settings belong to `LLM_MODEL` only.
        llm.usage = replace(llm.usage, priced=model == settings.llm_model)
        return llm

    def close(self) -> None:
        self._http.close()

    def next_turn(
        self,
        system_prompt: str,
        state_json: dict[str, Any],
        input_text: str,
        *,
        timeout: float | None = None,
    ) -> LLMTurn:
        # What is left of the guest's wait cuts the request short, never
        # stretches it (#252, as in `ChatCompletionsLLM`).
        within = self._timeout if timeout is None else min(timeout, self._timeout)
        state = json.dumps(state_json, ensure_ascii=False, default=str)
        output_format = ORDER_FORMAT if "cart" in state_json else OUTPUT_FORMAT
        body: dict[str, Any] = {
            "model": self._model,
            # A cut answer is no valid JSON and ends as `LLMError`. Thinking
            # counts against this limit: a run with an effort level needs a
            # higher `LLM_MAX_OUTPUT_TOKENS` than the default.
            "max_tokens": settings.llm_max_output_tokens,
            "system": f"{system_prompt}\n\n{output_format}",
            "messages": [
                {"role": "user", "content": f"Zustand: {state}\nEingabe: {input_text}"}
            ],
        }
        # ponytail: no prompt caching yet, so the eval cost of a run is the
        # plain token price; `cache_control` on the system block once operation
        # needs the lower price and first-token time.
        if self._effort == NO_THINKING:
            body["thinking"] = {"type": "disabled"}
        elif self._effort:
            body["output_config"] = {"effort": self._effort}
        self.usage = replace(
            self.usage,
            requests=self.usage.requests + 1,
            unmetered=self.usage.unmetered + 1,
        )
        try:
            response = self._http.post("messages", json=body, timeout=within)
            response.raise_for_status()
            payload = response.json()
            self._count(payload)
            stop = payload.get("stop_reason")
            text = "".join(
                block["text"]
                for block in payload["content"]
                if block.get("type") == "text"
            )
        except (
            httpx.HTTPError,
            ValueError,
            LookupError,
            TypeError,
            AttributeError,
        ) as exc:
            raise LLMError(f"model call failed: {type(exc).__name__}") from exc
        if stop == "refusal":
            raise LLMError("model declined the turn")
        if not text.strip():
            raise LLMError("model answer without text")
        return parse_turn(_unfenced(text))

    def _count(self, payload: object) -> None:
        """Tokens the API reports and the model that ran. Both numbers or
        nothing, as in `ChatCompletionsLLM._count`: the request stays
        unmetered without them."""
        if not isinstance(payload, dict):
            return
        answered = payload.get("model")
        if isinstance(answered, str) and answered:
            self.usage = replace(self.usage, model=answered)
        reported = payload.get("usage")
        if not isinstance(reported, dict):
            return
        tokens_in = reported.get("input_tokens")
        tokens_out = reported.get("output_tokens")
        if type(tokens_in) is not int or type(tokens_out) is not int:
            return
        if tokens_in < 0 or tokens_out < 0:
            return
        self.usage = replace(
            self.usage,
            prompt_tokens=self.usage.prompt_tokens + tokens_in,
            completion_tokens=self.usage.completion_tokens + tokens_out,
            unmetered=self.usage.unmetered - 1,
        )


def _unfenced(text: str) -> str:
    """The JSON without a Markdown fence around it. Claude models wrap JSON in
    ```json ... ``` now and then although the format asks for the bare object;
    anything else stays for `parse_turn` to judge."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def client_from_settings(model: str | None = None) -> ChatCompletionsLLM | MessagesLLM:
    """The model client for `model`, or for `LLM_MODEL` without one."""
    name = model or settings.llm_model
    if name.startswith(CLAUDE_PREFIX):
        return MessagesLLM.from_settings(name)
    return ChatCompletionsLLM.from_settings(model)
