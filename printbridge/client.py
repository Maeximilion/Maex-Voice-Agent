"""Verbindung der Bruecke zum Server: /v1/kitchen/claim und /v1/kitchen/ack (docs/04 §Kuechenbon).

Die Bruecke ruft nur hinaus, der Server nie hinein: im Router des Restaurants
muss kein Port offen sein.
"""

import json
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ServerError(Exception):
    """Server nicht erreichbar oder Antwort mit ok=false."""


class Server:
    def __init__(
        self,
        base_url: str,
        token: str,
        tenant_id: str,
        timeout: float = 10.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ):
        parts = urlsplit(base_url)
        # Das Token reist in jeder Anfrage mit: nie ueber unverschluesseltes HTTP ins Netz.
        if parts.scheme != "https" and parts.hostname not in LOCAL_HOSTS:
            raise ValueError("Server-Adresse muss mit https:// beginnen")
        if not token:
            raise ValueError("Token der Druckbruecke fehlt")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant_id = tenant_id
        self.timeout = timeout
        self.opener = opener

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps({"tenant_id": self.tenant_id, **body}).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise ServerError(f"{path}: {exc}") from exc
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise ServerError(f"{path}: {error.get('code')} {error.get('message')}")
        return envelope["data"]

    def claim(self, limit: int = 5) -> list[dict[str, Any]]:
        return self._post("/v1/kitchen/claim", {"limit": limit})["tickets"]

    def ack(self, event_id: str, ok: bool, error: str | None = None) -> str:
        body: dict[str, Any] = {"event_id": event_id, "ok": ok}
        if error:
            body["error"] = error[:500]
        return self._post("/v1/kitchen/ack", body)["status"]
