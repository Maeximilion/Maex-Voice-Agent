"""Every answer follows the envelope, the framework's own included (docs/04 §1).

FastAPI answers an unknown path and a wrong method itself, with Starlette's
`HTTPException`. The handler in `api/main.py` was registered for FastAPI's
subclass only, so these two answered `{"detail": ...}` past the envelope.
"""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


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
    # Which method would have worked stays in the answer.
    assert r.headers["allow"] == "POST"
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"
