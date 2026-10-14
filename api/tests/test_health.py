import pytest
from fastapi.testclient import TestClient

from api.config import TOKEN_PLACEHOLDER, settings
from api.main import app

client = TestClient(app)


def test_health_antwortet():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_tool_ohne_token_abgelehnt():
    r = client.post("/v1/tools/ping")
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"


def test_tool_mit_token():
    r = client.post(
        "/v1/tools/ping",
        headers={"Authorization": f"Bearer {settings.agent_api_token}"},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["pong"] is True
    assert "say" in body


@pytest.mark.parametrize("header", ["Bearer ", "Bearer", ""])
def test_an_empty_token_opens_nothing(monkeypatch, header):
    """`AGENT_API_TOKEN=` in the environment: the expected header would be
    "Bearer " with nothing behind it, which anyone can send."""
    monkeypatch.setattr(settings, "agent_api_token", "")
    r = client.post("/v1/tools/ping", headers={"Authorization": header})
    assert r.status_code == 401


def test_the_token_is_compared_in_full():
    """A prefix of the right token is not the token."""
    r = client.post(
        "/v1/tools/ping",
        headers={"Authorization": f"Bearer {settings.agent_api_token[:-1]}"},
    )
    assert r.status_code == 401


@pytest.mark.parametrize("env", ["prod", "production"])
@pytest.mark.parametrize(
    "token",
    [
        TOKEN_PLACEHOLDER,
        "short",
        " " * 32,
        " " * 12 + TOKEN_PLACEHOLDER,
        TOKEN_PLACEHOLDER + " " * 12,
        "k" * 16 + " " + "k" * 16,
    ],
)
def test_production_auth_rejects_an_invalid_token_even_if_settings_were_bypassed(
    monkeypatch, env, token
):
    """A later settings mutation must not reopen the shared authentication gate."""
    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(settings, "agent_api_token", token)
    r = client.post("/v1/tools/ping", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["ok"] is False


@pytest.mark.parametrize("env", ["prod", "production"])
def test_production_auth_accepts_the_complete_valid_token(monkeypatch, env):
    token = "k" * 32
    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(settings, "agent_api_token", token)
    assert (
        client.post(
            "/v1/tools/ping", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/tools/ping", headers={"Authorization": f"Bearer {token[:-1]}"}
        ).status_code
        == 401
    )
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_description_is_served_in_development_only(path):
    """The schema lists every path, the kitchen bridge included. The suite runs
    with ENV=test, so the routes must be gone here as they are in production."""
    assert settings.env != "dev"
    assert client.get(path).status_code == 404
