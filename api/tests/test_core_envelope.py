"""Antwort-Hülle und Fehlerübersetzung (docs/04 §1)."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api.core import envelope, errors
from api.main import app

client = TestClient(app, raise_server_exceptions=False)


def _body(response) -> dict:
    return json.loads(response.body)


def test_ok_huelle_hat_immer_say():
    assert envelope.ok_body({"x": 1}) == {"ok": True, "data": {"x": 1}, "say": None}
    assert _body(envelope.ok({"x": 1}, say="Gern.")) == {
        "ok": True,
        "data": {"x": 1},
        "say": "Gern.",
    }


def test_fail_huelle_traegt_code_und_message():
    r = envelope.fail("closed", "heute geschlossen", say="Wir haben heute leider zu.")
    assert r.status_code == 200
    assert _body(r) == {
        "ok": False,
        "error": {"code": "closed", "message": "heute geschlossen"},
        "say": "Wir haben heute leider zu.",
    }


@pytest.mark.parametrize(
    ("cls", "code"),
    [
        (errors.InvalidInput, "invalid_input"),
        (errors.NotFound, "not_found"),
        (errors.Ambiguous, "ambiguous"),
        (errors.Closed, "closed"),
        (errors.OutOfZone, "out_of_zone"),
        (errors.BelowMinimum, "below_minimum"),
        (errors.Conflict, "conflict"),
        (errors.ServiceUnavailable, "service_unavailable"),
    ],
)
def test_jede_fehlerklasse_hat_einen_spezifizierten_code(cls, code):
    assert code in errors.ERROR_CODES
    exc = cls("m", say="s")
    r = envelope.from_error(exc)
    assert r.status_code == 200
    assert _body(r)["error"] == {"code": code, "message": "m"}
    assert _body(r)["say"] == "s"


def test_fachfehler_im_endpunkt_wird_zur_huelle():
    @app.get("/_test/out-of-zone")
    def out_of_zone():
        raise errors.OutOfZone(
            "PLZ 99999 nicht im Gebiet", say="Dorthin liefern wir leider nicht."
        )

    try:
        r = client.get("/_test/out-of-zone")
        assert r.status_code == 200
        assert r.json()["error"]["code"] == "out_of_zone"
        assert r.json()["say"] == "Dorthin liefern wir leider nicht."
    finally:
        _remove_route("/_test/out-of-zone")


def test_ungueltiger_body_wird_invalid_input_ohne_422():
    class Body(BaseModel):
        call_id: str

    @app.post("/_test/validate")
    def validate(body: Body):
        return envelope.ok({})

    try:
        r = client.post("/_test/validate", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert r.json()["error"]["code"] == "invalid_input"
    finally:
        _remove_route("/_test/validate")


def test_unbehandelter_fehler_liefert_service_unavailable_mit_say():
    @app.get("/_test/crash")
    def crash():
        raise RuntimeError("kaputt")

    try:
        r = client.get("/_test/crash")
        assert r.status_code == 200
        assert r.json()["error"]["code"] == "service_unavailable"
        assert r.json()["say"] == envelope.SAY_ON_FAILURE
        assert "kaputt" not in r.text
    finally:
        _remove_route("/_test/crash")


def test_fehlender_token_ist_401_in_der_huelle():
    r = client.post("/v1/tools/ping")
    assert r.status_code == 401
    assert r.json() == {
        "ok": False,
        "error": {"code": "invalid_input", "message": "unauthorized"},
        "say": None,
    }


def _remove_route(path: str) -> None:
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", "") != path
    ]
