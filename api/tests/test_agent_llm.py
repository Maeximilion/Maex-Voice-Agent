"""agent/llm.py: LLMTurn erzwingt genau ein Ergebnis, FakeLLM spielt Skripte ab."""

import pytest

from api.agent.llm import FakeLLM, LLMTurn, ToolCall


def test_turn_braucht_genau_eins_von_say_oder_tool_call():
    with pytest.raises(ValueError):
        LLMTurn()
    with pytest.raises(ValueError):
        LLMTurn(say="Hallo", tool_call=ToolCall(name="get_service_status"))


def test_fake_llm_spielt_zuege_der_reihe_nach_ab():
    fake = FakeLLM([LLMTurn(say="eins"), LLMTurn(say="zwei")])

    assert fake.next_turn("prompt", {}, "erster") == LLMTurn(say="eins")
    assert fake.next_turn("prompt", {}, "zweiter") == LLMTurn(say="zwei")
    assert fake.calls == [("prompt", {}, "erster"), ("prompt", {}, "zweiter")]


def test_fake_llm_ohne_weitere_zuege_meldet_sich_klar():
    fake = FakeLLM([])
    with pytest.raises(AssertionError):
        fake.next_turn("prompt", {}, "input")
