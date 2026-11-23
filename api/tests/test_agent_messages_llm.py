"""agent/messages_llm.py: a Claude model behind the same `LLMClient` contract (T-5.3)."""

import json

import httpx
import pytest

from api.agent.llm import ORDER_FORMAT, OUTPUT_FORMAT, ChatCompletionsLLM, LLMError
from api.agent.messages_llm import MessagesLLM, client_from_settings
from api.config import settings


def _message(text: str, *, stop: str = "end_turn", model: str = "claude-haiku-4-5"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 40},
    }


def _client(handler, *, effort: str = "", timeout: float = 15.0) -> MessagesLLM:
    return MessagesLLM(
        "claude-haiku-4-5",
        "test-key",
        timeout=timeout,
        effort=effort,
        transport=httpx.MockTransport(handler),
    )


def _answering(text: str, seen: list, **message):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_message(text, **message))

    return handler


def test_a_sentence_comes_back_as_say_and_the_tokens_are_counted():
    seen: list = []
    llm = _client(
        _answering('{"say": "Gerne, für wie viele?", "slots": {"party_size": 2}}', seen)
    )

    turn = llm.next_turn("SYSTEM", {"stage": "greeting"}, "Ein Tisch bitte")

    assert turn.say == "Gerne, für wie viele?"
    assert turn.state_patch == {"party_size": 2}
    body = json.loads(seen[0].content)
    assert body["system"] == f"SYSTEM\n\n{OUTPUT_FORMAT}"
    assert body["messages"] == [
        {
            "role": "user",
            "content": 'Zustand: {"stage": "greeting"}\nEingabe: Ein Tisch bitte',
        }
    ]
    assert body["max_tokens"] == settings.llm_max_output_tokens
    # Current Claude models refuse any temperature; nothing about thinking
    # is sent without an effort setting.
    assert "temperature" not in body
    assert "thinking" not in body and "output_config" not in body
    assert seen[0].headers["x-api-key"] == "test-key"
    assert seen[0].headers["anthropic-version"] == "2026-10-21"
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert llm.usage.requests == 1
    assert llm.usage.prompt_tokens == 1200
    assert llm.usage.completion_tokens == 40
    assert llm.usage.unmetered == 0
    assert llm.usage.model == "claude-haiku-4-5"


def test_an_order_gets_the_order_format_and_a_tool_call_comes_back():
    seen: list = []
    llm = _client(_answering('{"tool": "search_menu", "args": {"query": "25A"}}', seen))

    turn = llm.next_turn("SYSTEM", {"cart": []}, "Einmal die 25A")

    assert turn.tool_call is not None
    assert turn.tool_call.name == "search_menu"
    assert turn.tool_call.args == {"query": "25A"}
    assert json.loads(seen[0].content)["system"].endswith(ORDER_FORMAT)


def test_json_in_a_markdown_fence_is_read_like_the_bare_object():
    llm = _client(_answering('```json\n{"say": "Hallo"}\n```', []))

    assert llm.next_turn("S", {}, "x").say == "Hallo"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("none", {"thinking": {"type": "disabled"}}),
        ("low", {"output_config": {"effort": "low"}}),
    ],
)
def test_the_effort_setting_reaches_the_request(effort, expected):
    seen: list = []
    llm = _client(_answering('{"say": "Hallo"}', seen), effort=effort)

    llm.next_turn("S", {}, "x")

    body = json.loads(seen[0].content)
    for key, value in expected.items():
        assert body[key] == value


def test_a_refusal_is_a_model_error_and_still_counted():
    llm = _client(_answering("", [], stop="refusal"))

    with pytest.raises(LLMError, match="declined"):
        llm.next_turn("S", {}, "x")
    assert llm.usage.unmetered == 0
    assert llm.usage.prompt_tokens == 1200


def test_an_answer_outside_the_contract_is_a_model_error():
    llm = _client(_answering("Gerne helfe ich Ihnen.", []))

    with pytest.raises(LLMError, match="outside the contract"):
        llm.next_turn("S", {}, "x")


def test_a_server_error_is_a_model_error_without_the_guest_text():
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            529,
            json={
                "type": "error",
                "error": {"type": "overloaded_error", "message": "busy"},
            },
        )

    llm = _client(broken)

    with pytest.raises(LLMError) as raised:
        llm.next_turn("S", {}, "meine Adresse ist Hauptstraße 5")
    assert "Hauptstraße" not in str(raised.value)
    # One request, nothing billed known: the call's cost stays unknown.
    assert llm.usage.requests == 1
    assert llm.usage.unmetered == 1


def test_an_unreachable_server_is_a_model_error():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="ConnectError"):
        _client(down).next_turn("S", {}, "x")


def test_the_guests_remaining_wait_cuts_the_request_short():
    seen: list = []
    llm = _client(_answering('{"say": "Hallo"}', seen), timeout=15.0)

    llm.next_turn("S", {}, "x", timeout=2.5)
    llm.next_turn("S", {}, "x", timeout=40.0)

    assert seen[0].extensions["timeout"]["read"] == 2.5
    # Never longer than the client's own limit.
    assert seen[1].extensions["timeout"]["read"] == 15.0


def test_a_claude_name_picks_the_messages_client(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "claude-haiku-4-5")
    monkeypatch.setattr(settings, "llm_base_url", "")

    configured = client_from_settings()
    named = client_from_settings("claude-haiku-5-5")

    assert isinstance(configured, MessagesLLM)
    assert configured.usage.priced is True
    # Another model than `LLM_MODEL`: the prices of the settings are not its own.
    assert isinstance(named, MessagesLLM)
    assert named.usage.model == "claude-haiku-5-5"
    assert named.usage.priced is False


def test_a_claude_model_without_a_key_refuses_to_start(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        client_from_settings("claude-haiku-4-5")


def test_any_other_name_stays_on_the_chat_completions_server(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen3:14b")

    assert isinstance(client_from_settings(), ChatCompletionsLLM)
    assert isinstance(client_from_settings("mistral-small"), ChatCompletionsLLM)
