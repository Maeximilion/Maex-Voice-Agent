"""The push to the operator's `ops` topic when a way into the dashboard
changes (docs/23 §5): a new or removed passkey, a new or removed TOTP secret,
a used recovery code. A takeover must not be silent.

This is the one place the dashboard sends a push from. It publishes the way a
customer's stack does (`api/jobs/monitor.py`): a POST to the push server with
a token that may write to `ops`. The operator stack has no Compose file yet
(#571, the last part), so `OPERATOR_PUSH_URL` and `OPERATOR_PUSH_TOKEN` are
empty until then and the push is only logged; the audit row is written either
way, by the caller.
"""

import logging

import httpx

from api.core.logging import get_logger, log
from api.dashboard.config import settings

logger = get_logger("api.dashboard.push")

TOPIC = "ops"
# ntfy's "high": the phone makes itself heard.
PRIORITY = 4
TIMEOUT_SECONDS = 3.0


def notify(title: str, text: str) -> bool:
    """Send one push. False when no push server is set; raises
    `httpx.HTTPError` when the server refuses or does not answer."""
    if not settings.operator_push_url or not settings.operator_push_token:
        log(logger, logging.WARNING, "Operator push not sent, no server", title=title)
        return False
    response = httpx.post(
        settings.operator_push_url,
        json={"topic": TOPIC, "title": title, "message": text, "priority": PRIORITY},
        headers={"Authorization": f"Bearer {settings.operator_push_token}"},
        timeout=TIMEOUT_SECONDS,
        trust_env=False,
    )
    response.raise_for_status()
    return True
