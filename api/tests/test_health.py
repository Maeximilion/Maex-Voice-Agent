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


# --- Every answer follows the envelope, the framework's own included (docs/04 §1) --


def test_an_unknown_path_answers_in_the_envelope():
    r = client.get("/v1/nothing-here")

    assert r.status_code == 404
    assert r.json() == {
        "ok": False,
        "error": {"code": "not_found", "message": "Not Found"},
        "say": None,
    }


def test_a_wrong_method_answers_in_the_envelope():
    r = client.get("/v1/tools/ping")

    assert r.status_code == 405
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"
