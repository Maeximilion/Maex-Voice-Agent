"""agent/llm.py: LLMTurn erzwingt genau ein Ergebnis, FakeLLM spielt Skripte ab."""

import json

import httpx
import pytest

from api.agent.llm import (
    OUTPUT_FORMAT,
    ChatCompletionsLLM,
    FakeLLM,
    LLMError,
    LLMTurn,
    ToolCall,
    Usage,
    cost_cents,
    parse_turn,
)
from api.config import settings


def test_turn_braucht_genau_eins_von_say_oder_tool_call():
    with pytest.raises(ValueError):
        LLMTurn()
    with pytest.raises(ValueError):
        LLMTurn(say="Hallo", tool_call=ToolCall(name="get_service_status"))


def test_understanding_failure_ist_unabhaengig_von_say_oder_tool_call():
    turn = LLMTurn(say="Wie bitte?", understanding_failure="party_size")
    assert turn.understanding_failure == "party_size"


def test_state_patch_ist_unabhaengig_von_say_oder_tool_call():
    with_say = LLMTurn(say="Hallo", state_patch={"guest_name": "Müller"})
    with_tool = LLMTurn(
        tool_call=ToolCall(name="get_service_status"), state_patch={"party_size": 4}
    )

    assert with_say.state_patch == {"guest_name": "Müller"}
    assert with_tool.state_patch == {"party_size": 4}


def test_fake_llm_spielt_zuege_der_reihe_nach_ab():
    fake = FakeLLM([LLMTurn(say="eins"), LLMTurn(say="zwei")])

    assert fake.next_turn("prompt", {}, "erster") == LLMTurn(say="eins")
    assert fake.next_turn("prompt", {}, "zweiter") == LLMTurn(say="zwei")
    assert fake.calls == [("prompt", {}, "erster"), ("prompt", {}, "zweiter")]


def test_fake_llm_ohne_weitere_zuege_meldet_sich_klar():
    fake = FakeLLM([])
    with pytest.raises(AssertionError):
        fake.next_turn("prompt", {}, "input")


# --- ChatCompletionsLLM (T-2.4): a real model behind the same interface ---------

SYSTEM = "Du nimmst Anrufe entgegen."
STATE = {"stage": "collecting", "open": [], "slots": {"party_size": 4}}


def recorded(content, status=200):
    """A chat completions answer as a local or hosted server sends it."""
    return httpx.Response(
        status,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12},
        },
    )


def client_with(handler, **kwargs):
    return ChatCompletionsLLM(
        "http://model.test/v1",
        "test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_parse_turn_reads_a_sentence_for_the_guest():
    turn = parse_turn('{"say": "Für wie viele Personen?", "tool": null}')

    assert turn == LLMTurn(say="Für wie viele Personen?")


def test_parse_turn_reads_a_tool_call_with_slots_and_a_failed_field():
    turn = parse_turn(
        json.dumps(
            {
                "say": "",
                "tool": "check_slot",
                "args": {"party_size": 4, "reserved_for": "2026-09-15T19:00:00+02:00"},
                "slots": {"party_size": 4},
                "not_understood": "guest_name",
            }
        )
    )

    assert turn.say is None
    assert turn.tool_call == ToolCall(
        name="check_slot",
        args={"party_size": 4, "reserved_for": "2026-09-15T19:00:00+02:00"},
    )
    assert turn.state_patch == {"party_size": 4}
    assert turn.understanding_failure == "guest_name"


def test_parse_turn_drops_empty_slot_values():
    """A model that fills fields it has nothing for must not erase what is
    known: `phone: null` would wipe the caller ID (Codex PR #208, P2)."""
    turn = parse_turn(
        json.dumps(
            {
                "say": "Auf welchen Namen?",
                "slots": {
                    "phone": None,
                    "guest_name": "",
                    "note": " ",
                    "party_size": 4,
                },
            }
        )
    )

    assert turn.state_patch == {"party_size": 4}


def test_parse_turn_with_only_empty_slot_values_has_no_patch():
    turn = parse_turn('{"say": "Gern.", "slots": {"phone": null, "guest_name": ""}}')

    assert turn.state_patch is None


def test_parse_turn_keeps_a_zero_and_a_false_slot_value():
    turn = parse_turn('{"say": "Gern.", "slots": {"children": 0, "delivery": false}}')

    assert turn.state_patch == {"children": 0, "delivery": False}


def test_parse_turn_takes_a_tool_call_without_args():
    assert parse_turn('{"tool": "get_service_status"}').tool_call == ToolCall(
        name="get_service_status"
    )


@pytest.mark.parametrize(
    "content",
    [
        "Guten Tag!",  # no JSON at all
        '["say", "Hallo"]',  # JSON, but not an object
        "{}",  # neither a sentence nor a tool
        '{"say": "  ", "tool": null}',  # blank counts as nothing
        # A sentence next to a tool call could be a readback with `confirm` in
        # the same breath (CLAUDE.md §2 rule 3): rejected, never picked from.
        '{"say": "Passt das so?", "tool": "confirm", "args": {}}',
        '{"tool": "check_slot", "args": "party_size=4"}',
        '{"say": 5}',
        '{"say": "Hallo", "slots": ["party_size"]}',
    ],
)
def test_parse_turn_rejects_an_answer_outside_the_contract(content):
    with pytest.raises(LLMError):
        parse_turn(content)


def test_client_sends_prompt_state_and_input_and_returns_the_turn():
    seen = []

    def handler(request):
        seen.append(request)
        return recorded('{"say": "Guten Tag, hier ist der KI-Assistent."}')

    turn = client_with(handler, api_key="secret").next_turn(SYSTEM, STATE, "Hallo")

    assert turn == LLMTurn(say="Guten Tag, hier ist der KI-Assistent.")
    (request,) = seen
    assert request.url == "http://model.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["response_format"] == {"type": "json_object"}
    # A turn is a sentence or two: a model that runs on is cut off, and the cut
    # answer is no valid turn (Codex PR #208, P2).
    assert body["max_tokens"] == settings.llm_max_output_tokens
    system, user = body["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith(SYSTEM)
    assert OUTPUT_FORMAT in system["content"]
    assert user["role"] == "user"
    assert '"party_size": 4' in user["content"]
    assert user["content"].endswith("Hallo")


def test_client_keeps_umlauts_readable_for_the_model():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return recorded('{"say": "Gern."}')

    client_with(handler).next_turn(
        SYSTEM, {"slots": {"guest_name": "Müller"}}, "Frühlingsrollen"
    )

    assert "Müller" in seen[0]["messages"][1]["content"]


def test_client_without_a_key_sends_no_authorization_header():
    seen = []

    def handler(request):
        seen.append(request)
        return recorded('{"say": "Gern."}')

    client_with(handler).next_turn(SYSTEM, STATE, "Hallo")

    assert "authorization" not in seen[0].headers


def timeout(request):
    raise httpx.ReadTimeout("too slow", request=request)


def refused(request):
    raise httpx.ConnectError("no server", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        timeout,
        refused,
        lambda request: recorded("irrelevant", status=500),
        lambda request: httpx.Response(200, text="<html>gateway</html>"),
        lambda request: httpx.Response(200, json={"choices": []}),
        lambda request: recorded(None),  # e.g. a native tool call, no content
        lambda request: recorded("Guten Tag!"),
    ],
    ids=[
        "timeout",
        "unreachable",
        "http-500",
        "not-json",
        "no-choice",
        "no-content",
        "content-outside-contract",
    ],
)
def test_client_reports_every_failure_as_llm_error(handler):
    with pytest.raises(LLMError):
        client_with(handler).next_turn(SYSTEM, STATE, "Hallo")


def test_from_settings_builds_the_client_from_the_environment(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_model", "local-model")

    assert isinstance(ChatCompletionsLLM.from_settings(), ChatCompletionsLLM)


def test_client_counts_the_tokens_of_every_answer():
    llm = client_with(lambda request: recorded('{"say": "Gern."}'))
    assert llm.usage == Usage(model="test-model")

    llm.next_turn(SYSTEM, STATE, "Hallo")
    llm.next_turn(SYSTEM, STATE, "Einen Tisch bitte")

    assert llm.usage == Usage(
        model="test-model", requests=2, prompt_tokens=160, completion_tokens=24
    )


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": "80", "completion_tokens": None},
        {"prompt_tokens": -5, "completion_tokens": 12.5},
        {"prompt_tokens": True, "completion_tokens": True},
        "80 tokens",
    ],
)
def test_answer_without_usable_token_numbers_counts_as_a_request_only(usage):
    """Not every server sends the block. Unknown stays unknown, never estimated."""

    def handler(request):
        body = {"choices": [{"message": {"content": '{"say": "Gern."}'}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    llm = client_with(handler)

    assert llm.next_turn(SYSTEM, STATE, "Hallo") == LLMTurn(say="Gern.")
    assert llm.usage == Usage(model="test-model", requests=1)


def test_failed_request_counts_as_a_request_without_tokens():
    llm = client_with(timeout)

    with pytest.raises(LLMError):
        llm.next_turn(SYSTEM, STATE, "Hallo")

    assert llm.usage == Usage(model="test-model", requests=1)


def test_answer_outside_the_contract_still_costs_its_tokens():
    llm = client_with(lambda request: recorded("Guten Tag!"))

    with pytest.raises(LLMError):
        llm.next_turn(SYSTEM, STATE, "Hallo")

    assert llm.usage.prompt_tokens == 80
    assert llm.usage.completion_tokens == 12


def prices(monkeypatch, per_mtok_in, per_mtok_out):
    monkeypatch.setattr(settings, "llm_input_cents_per_mtok", per_mtok_in)
    monkeypatch.setattr(settings, "llm_output_cents_per_mtok", per_mtok_out)


@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens", "cents"),
    [
        (0, 0, 0),
        (80, 12, 1),  # 0.00192 cents: any use at all is at least one cent
        (1_000_000, 0, 15),
        (1_000_000, 1_000_000, 75),
        (1_000_001, 0, 16),  # always up, a budget is never undercut by rounding
        (40_000, 3_000, 1),  # a long call on a small model
    ],
)
def test_cost_is_whole_cents_rounded_up(
    monkeypatch, prompt_tokens, completion_tokens, cents
):
    prices(monkeypatch, 15, 60)
    usage = Usage(
        model="test-model",
        requests=3,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    assert cost_cents(usage) == cents


def test_cost_of_a_free_local_model_is_zero(monkeypatch):
    prices(monkeypatch, 0, 0)

    assert cost_cents(Usage(model="local", requests=4, prompt_tokens=9000)) == 0


@pytest.mark.parametrize(("per_mtok_in", "per_mtok_out"), [(None, None), (15, None)])
def test_cost_without_both_prices_is_unknown_not_zero(
    monkeypatch, per_mtok_in, per_mtok_out
):
    prices(monkeypatch, per_mtok_in, per_mtok_out)

    assert cost_cents(Usage(model="test-model", prompt_tokens=9000)) is None


def test_cost_without_a_model_is_unknown(monkeypatch):
    """The scripted stand-in is no model: its calls get no price, not 0 cents."""
    prices(monkeypatch, 15, 60)

    assert cost_cents(Usage()) is None


@pytest.mark.parametrize("missing", ["llm_base_url", "llm_model"])
def test_from_settings_without_address_or_model_says_so(monkeypatch, missing):
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_model", "local-model")
    monkeypatch.setattr(settings, missing, "")

    with pytest.raises(LLMError, match="LLM_BASE_URL"):
        ChatCompletionsLLM.from_settings()
