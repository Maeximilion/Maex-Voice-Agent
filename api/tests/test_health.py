from fastapi.testclient import TestClient

from api.config import settings
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
