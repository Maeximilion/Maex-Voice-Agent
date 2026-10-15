"""The operations view answers only requests that came through Caddy.

Security audit of 15.10.2026, #252 finding 6: every container on the Docker network
reaches api:8000, Asterisk among them, and Caddy's Basic-Auth is the view's only
check. Caddy sends GUI_PROXY_SECRET; outside dev and test the view needs it.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.config import TOKEN_PLACEHOLDER, settings
from api.core.auth import require_gui_proxy
from api.main import app

client = TestClient(app)
SECRET = "s" * 32
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "agent_api_token", "a" * 32)
    monkeypatch.setattr(settings, "gui_proxy_secret", SECRET)


@pytest.mark.parametrize("sent", [None, "", SECRET[:-1], SECRET + "x"])
def test_the_view_refuses_a_request_without_the_right_secret(production, sent):
    headers = {} if sent is None else {"X-Maex-Proxy-Secret": sent}
    assert client.get("/gui/", headers=headers).status_code == 403
    assert client.get("/gui/events", headers=headers).status_code == 403


def test_the_secret_from_caddy_opens_the_view(production):
    require_gui_proxy(SECRET)


@pytest.mark.parametrize("secret", ["", TOKEN_PLACEHOLDER, "short", "a" * 32])
def test_a_missing_weak_or_reused_secret_closes_the_view(monkeypatch, secret):
    """`a` * 32 is the agent token of this test: one secret must not open both."""
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "agent_api_token", "a" * 32)
    monkeypatch.setattr(settings, "gui_proxy_secret", secret)
    with pytest.raises(HTTPException) as refused:
        require_gui_proxy(secret)
    assert refused.value.status_code == 403


@pytest.mark.parametrize("env", ["dev", "test"])
def test_dev_and_test_have_no_proxy_and_need_no_secret(monkeypatch, env):
    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(settings, "gui_proxy_secret", "")
    require_gui_proxy("")


def test_caddy_sends_the_secret_to_the_api():
    text = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert 'header_up X-Maex-Proxy-Secret "{env.GUI_PROXY_SECRET}"' in text
