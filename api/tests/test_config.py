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


@pytest.mark.parametrize("token", [TOKEN_PLACEHOLDER, "", "short", "k" * 31])
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
