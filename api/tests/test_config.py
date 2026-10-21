"""Settings: production refuses to start on a token anyone knows."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from api.config import TOKEN_PLACEHOLDER, Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]

LONG_ENOUGH = "k" * 32


def _settings(**values: Any) -> Settings:
    # No .env and no process environment: only what the test names.
    # pydantic-settings takes `_env_file` at runtime; without its mypy plugin
    # the signature mypy sees holds the fields only.
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_the_placeholder_is_what_the_example_file_ships():
    """The guard knows the placeholder by value; the example must not drift."""
    example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"AGENT_API_TOKEN={TOKEN_PLACEHOLDER}" in example.splitlines()


@pytest.mark.parametrize(
    "token",
    [
        TOKEN_PLACEHOLDER,
        "",
        "short",
        "k" * 31,
        " " * 32,
        " " * 12 + TOKEN_PLACEHOLDER,
        TOKEN_PLACEHOLDER + " " * 12,
        " " * 16 + "short" + " " * 16,
        " " + LONG_ENOUGH,
        LONG_ENOUGH + " ",
        "k" * 16 + " " + "k" * 16,
        "\u00a0" + LONG_ENOUGH,
        LONG_ENOUGH + "\t",
    ],
)
def test_production_does_not_start_on_a_weak_token(token):
    with pytest.raises(ValidationError, match="AGENT_API_TOKEN"):
        _settings(env="prod", agent_api_token=token)


def test_an_unknown_environment_counts_as_production():
    """`ENV=production` or a typo must not be the way around the guard."""
    with pytest.raises(ValidationError, match="AGENT_API_TOKEN"):
        _settings(env="production", agent_api_token=TOKEN_PLACEHOLDER)


def test_production_starts_on_a_long_random_token():
    assert _settings(env="prod", agent_api_token=LONG_ENOUGH).env == "prod"


@pytest.mark.parametrize("env", ["dev", "test"])
def test_development_and_tests_keep_their_short_tokens(env):
    """`ci-token` and the placeholder of a copied .env.example keep working."""
    assert _settings(env=env, agent_api_token=TOKEN_PLACEHOLDER).env == env
    assert _settings(env=env, agent_api_token="ci-token").env == env


def test_the_error_does_not_print_the_token():
    with pytest.raises(ValidationError) as failure:
        _settings(env="prod", agent_api_token="short-secret")
    assert "short-secret" not in str(failure.value)


# Security audit of 15.10.2026, #252 finding 5: the proxy offers /v1/kitchen/*
# to the internet, so the bridge token gets the agent token's strength rule.
@pytest.mark.parametrize(
    "token", [TOKEN_PLACEHOLDER, "short", "k" * 31, "k" * 16 + " " + "k" * 16]
)
def test_production_does_not_start_on_a_weak_kitchen_token(token):
    with pytest.raises(ValidationError, match="KITCHEN_BRIDGE_TOKEN"):
        _settings(env="prod", agent_api_token=LONG_ENOUGH, kitchen_bridge_token=token)


def test_production_refuses_the_agent_token_as_kitchen_token():
    """One leaked value must not open both the tools and the kitchen slips."""
    with pytest.raises(ValidationError, match="KITCHEN_BRIDGE_TOKEN"):
        _settings(
            env="prod", agent_api_token=LONG_ENOUGH, kitchen_bridge_token=LONG_ENOUGH
        )


def test_production_starts_without_a_bridge_or_with_a_strong_kitchen_token():
    """Empty keeps /v1/kitchen/* closed, as before."""
    assert _settings(env="prod", agent_api_token=LONG_ENOUGH).kitchen_bridge_token == ""
    other = "b" * 32
    assert (
        _settings(
            env="prod", agent_api_token=LONG_ENOUGH, kitchen_bridge_token=other
        ).kitchen_bridge_token
        == other
    )


@pytest.mark.parametrize("password", ["change_me", "short", "k" * 31, " " * 32])
def test_production_does_not_start_on_a_weak_n8n_webhook_password(password):
    with pytest.raises(ValidationError, match="N8N_BASIC_AUTH_PASSWORD"):
        _settings(
            env="prod",
            agent_api_token=LONG_ENOUGH,
            n8n_basic_auth_user="maex",
            n8n_basic_auth_password=password,
        )


def test_the_n8n_password_is_checked_only_where_a_service_has_it():
    """Compose hands every service the user and only the dispatcher the
    password (#359): the API and voice must start with the user alone."""
    assert _settings(env="prod", agent_api_token=LONG_ENOUGH).env == "prod"
    with_user_only = _settings(
        env="prod", agent_api_token=LONG_ENOUGH, n8n_basic_auth_user="maex"
    )
    assert with_user_only.n8n_basic_auth_password == ""


def test_development_keeps_short_kitchen_and_n8n_values():
    values = _settings(
        env="dev",
        kitchen_bridge_token="bridge",
        n8n_basic_auth_user="maex",
        n8n_basic_auth_password="change_me",
    )
    assert values.kitchen_bridge_token == "bridge"
