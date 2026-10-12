"""The agent token: what the settings accept, and what `require_token` opens.

Two layers on purpose. The settings refuse to load outside a workbench with a
token that is missing, blank or still the placeholder of `.env.example`, so a
server with such a configuration does not start. And the dependency does not
rely on that: built past the check, such a configuration opens nothing.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.config import PLACEHOLDER_TOKEN, Settings
from api.core import auth
from api.main import app

REPO = Path(__file__).resolve().parents[2]
TOKEN = "k7Qm2-synthetic-token-for-tests-9fX"
OTHER_SECRET = "synthetic-llm-key-never-in-an-error"

# Not there, empty, blanks only, and the value everybody can read in the repo.
UNUSABLE = [None, "", "   ", "\t\n", PLACEHOLDER_TOKEN, f" {PLACEHOLDER_TOKEN} "]
# Production, and what a typo or another stage name makes of ENV: only a
# workbench and CI may run on the placeholder.
NOT_A_WORKBENCH = ["prod", "production", "PROD", "staging", ""]


def settings_for(monkeypatch, env: str, token: str | None) -> Settings:
    """Settings as a process would load them, without a `.env` file."""
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    given = {} if token is None else {"agent_api_token": token}
    return Settings(_env_file=None, env=env, **given)


# --- the settings ----------------------------------------------------------------


@pytest.mark.parametrize("env", NOT_A_WORKBENCH)
@pytest.mark.parametrize("token", UNUSABLE)
def test_settings_do_not_load_with_an_unusable_token_outside_a_workbench(
    monkeypatch, env, token
):
    with pytest.raises(ValidationError) as failed:
        settings_for(monkeypatch, env, token)

    assert "AGENT_API_TOKEN" in str(failed.value)


@pytest.mark.parametrize("env", NOT_A_WORKBENCH)
def test_settings_load_with_a_real_token(monkeypatch, env):
    assert settings_for(monkeypatch, env, TOKEN).agent_api_token == TOKEN


@pytest.mark.parametrize("env", ["dev", "test"])
@pytest.mark.parametrize("token", [None, PLACEHOLDER_TOKEN])
def test_the_placeholder_is_enough_on_a_workbench(monkeypatch, env, token):
    """`cp .env.example .env` and the stack runs (README quickstart)."""
    assert settings_for(monkeypatch, env, token).agent_api_token == PLACEHOLDER_TOKEN


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)

    assert Settings(_env_file=None, env="prod").agent_api_token == TOKEN


# --- the dependency --------------------------------------------------------------


def client_with(monkeypatch, env: str, token: str) -> TestClient:
    """The app with settings built past their own check."""
    monkeypatch.setattr(
        auth, "settings", Settings.model_construct(env=env, agent_api_token=token)
    )
    return TestClient(app)


def ping(client: TestClient, header: str | None):
    headers = {} if header is None else {"Authorization": header}
    return client.post("/v1/tools/ping", headers=headers)


@pytest.mark.parametrize(
    ("env", "token"),
    [
        *[(env, PLACEHOLDER_TOKEN) for env in NOT_A_WORKBENCH],
        *[(env, blank) for env in ("prod", "dev", "test") for blank in ("", "   ")],
    ],
)
def test_an_unusable_token_opens_nothing_past_the_settings_check(
    monkeypatch, env, token
):
    """The audit case of 12.10.2026: `Bearer change_me_long_random` and a bare
    `Bearer ` both answered 200 under ENV=prod."""
    client = client_with(monkeypatch, env, token)

    for header in (f"Bearer {token}", "Bearer ", "Bearer", "", None):
        answer = ping(client, header)
        assert answer.status_code == 401, header
        assert answer.json()["ok"] is False


@pytest.mark.parametrize(
    ("env", "token"),
    [("prod", TOKEN), ("dev", TOKEN), ("dev", PLACEHOLDER_TOKEN), ("test", TOKEN)],
)
def test_the_configured_token_opens_and_no_other(monkeypatch, env, token):
    client = client_with(monkeypatch, env, token)

    assert ping(client, f"Bearer {token}").json() == {
        "ok": True,
        "data": {"pong": True},
        "say": None,
    }
    for header in (None, "", "Bearer ", f"Bearer {token}x", f"bearer {token}", token):
        answer = ping(client, header)
        assert answer.status_code == 401, header
        assert answer.json()["error"]["code"] == "invalid_input"


def test_a_header_that_is_not_ascii_is_refused_not_a_crash(monkeypatch):
    """Header bytes outside ASCII reach the app as text; the comparison in
    constant time takes bytes, text of that kind would raise."""
    client = client_with(monkeypatch, "prod", TOKEN)

    answer = client.post(
        "/v1/tools/ping",
        headers={"Authorization": "Bearer schlüssel".encode("latin-1")},
    )

    assert answer.status_code == 401


def test_health_stays_open_whatever_the_token(monkeypatch):
    client = client_with(monkeypatch, "prod", "")

    assert client.get("/health").status_code == 200


# --- the way a server starts -----------------------------------------------------


def start_app(tmp_path, **env: str) -> subprocess.CompletedProcess:
    """Imports the app in a process of its own, as uvicorn does, from a folder
    without a `.env`: only what the environment carries counts."""
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in ("AGENT_API_TOKEN", "ENV")
    }
    return subprocess.run(
        [sys.executable, "-c", "import api.main"],
        cwd=tmp_path,
        env={
            **inherited,
            "PYTHONPATH": str(REPO),
            "LLM_API_KEY": OTHER_SECRET,
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("token", UNUSABLE)
def test_a_server_does_not_start_with_an_unusable_token(tmp_path, token):
    env = (
        {"ENV": "prod"} if token is None else {"ENV": "prod", "AGENT_API_TOKEN": token}
    )

    started = start_app(tmp_path, **env)

    assert started.returncode != 0
    assert "AGENT_API_TOKEN" in started.stderr
    # The error names the variable and not what the environment holds.
    assert OTHER_SECRET not in started.stderr


def test_a_server_starts_with_a_real_token(tmp_path):
    started = start_app(tmp_path, ENV="prod", AGENT_API_TOKEN=TOKEN)

    assert started.returncode == 0, started.stderr
