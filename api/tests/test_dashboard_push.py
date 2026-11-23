"""The operator's push leaves through its outbox (#827 F3, docs/23 §5).

A changed factor queues a row in the operator database in its own request; the
dashboard's sender posts it to the push server later. These tests run a real
HTTP server on the loopback as the push server, so the push is seen leaving
the process, and stop it to see a push wait and come through afterwards.
"""

import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import delete, select

from api.dashboard import push
from api.dashboard.config import settings
from api.dashboard.models import OperatorPush
from api.tests import test_dashboard_passkeys as base
from api.tests.passkey_device import Device
from api.tests.test_dashboard_login import START
from api.tests.test_dashboard_passkeys import _register

# Fixtures of the passkey tests, taken by assignment (see test_ops_write.py).
operator_url = base.operator_url
sessions = base.sessions
clock = base.clock
client = base.client
factors = base.factors

TOKEN = "tk_" + "a" * 29


class PushServer:
    """A stand-in for ntfy on the loopback: records each POST it is sent."""

    def __init__(self) -> None:
        self.received: list[tuple[dict[str, Any], str]] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(length))
                received.append((body, self.headers["Authorization"]))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server(monkeypatch):
    running = PushServer()
    monkeypatch.setattr(settings, "operator_push_url", running.url)
    monkeypatch.setattr(settings, "operator_push_token", TOKEN)
    yield running
    running.stop()


def _signed_in(client, factors, clock, sessions) -> None:
    """Signed in, with the push of the new browser (#888 A1) taken out of the
    outbox again, so each test sees only the pushes it makes itself."""
    base._signed_in(client, factors, clock)
    with sessions() as db:
        db.execute(delete(OperatorPush))
        db.commit()


def _queued(sessions) -> list[OperatorPush]:
    with sessions() as db:
        return list(db.scalars(select(OperatorPush).order_by(OperatorPush.id)))


def test_a_new_passkey_is_queued_in_the_request_and_sent_by_the_sender(
    client, factors, clock, sessions, server
):
    _signed_in(client, factors, clock, sessions)
    answer = _register(client, Device())
    assert answer.headers["location"] == "/factors?note=passkey-added"
    # The request sent nothing itself: the push waits in the outbox.
    assert server.received == []
    (row,) = _queued(sessions)
    assert row.sent_at is None and row.message == "maxi: new passkey 'Laptop'"

    with sessions() as db:
        assert push.deliver_once(db, clock.at) == 1

    ((body, authorization),) = server.received
    assert body == {
        "topic": "ops",
        "title": push.TITLE,
        "message": "maxi: new passkey 'Laptop'",
        "priority": 4,
    }
    assert authorization == f"Bearer {TOKEN}"
    (row,) = _queued(sessions)
    assert row.sent_at == clock.at and row.attempts == 1 and row.last_error is None
    # Sent once: the next round has nothing to do.
    with sessions() as db:
        assert push.deliver_once(db, clock.at + timedelta(hours=1)) == 0
    assert len(server.received) == 1


def test_a_push_server_that_is_down_never_blocks_the_request_or_loses_the_push(
    client, factors, clock, sessions, server
):
    server.stop()
    _signed_in(client, factors, clock, sessions)
    assert _register(client, Device()).status_code == 303

    with sessions() as db:
        assert push.deliver_once(db, clock.at) == 0
    (row,) = _queued(sessions)
    assert row.sent_at is None and row.attempts == 1 and row.last_error
    assert row.next_attempt_at == clock.at + timedelta(seconds=5)
    # Not due yet: the same round does not hammer the server.
    with sessions() as db:
        assert push.deliver_once(db, clock.at + timedelta(seconds=4)) == 0
    assert _queued(sessions)[0].attempts == 1

    # Down for a long time: it keeps waiting at the longest step, never gives up.
    later = clock.at
    for _ in range(8):
        later = _queued(sessions)[0].next_attempt_at
        with sessions() as db:
            push.deliver_once(db, later)
    (row,) = _queued(sessions)
    assert row.sent_at is None and row.attempts == 9
    assert row.next_attempt_at - later == timedelta(seconds=600)

    back = PushServer()
    try:
        settings.operator_push_url = back.url
        with sessions() as db:
            assert push.deliver_once(db, row.next_attempt_at) == 1
        assert [body["message"] for body, _ in back.received] == [
            "maxi: new passkey 'Laptop'"
        ]
    finally:
        back.stop()


def test_pushes_go_out_oldest_first(sessions, server):
    with sessions() as db:
        push.queue(db, "first", START)
        push.queue(db, "second", START)
        assert push.deliver_once(db, START + timedelta(days=1)) == 2
    assert [body["message"] for body, _ in server.received] == ["first", "second"]


@pytest.mark.parametrize(
    ("url", "token"), [("", ""), ("http://push.test/", ""), ("", "secret")]
)
def test_without_a_server_and_a_token_the_pushes_wait(
    sessions, monkeypatch, url, token
):
    monkeypatch.setattr(settings, "operator_push_url", url)
    monkeypatch.setattr(settings, "operator_push_token", token)
    with sessions() as db:
        push.queue(db, "waiting", START)
        assert push.deliver_once(db, START) == 0
    (row,) = _queued(sessions)
    assert row.sent_at is None and row.attempts == 0
    assert push.configured() is False


def test_a_refusing_push_server_counts_as_a_failed_attempt(sessions, monkeypatch):
    """A 401 (a wrong token) is a failure like a server that is down."""

    class Refusing(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    refusing = ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    threading.Thread(target=refusing.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(
            settings,
            "operator_push_url",
            f"http://127.0.0.1:{refusing.server_address[1]}/",
        )
        monkeypatch.setattr(settings, "operator_push_token", TOKEN)
        with sessions() as db:
            push.queue(db, "refused", START)
            assert push.deliver_once(db, START) == 0
    finally:
        refusing.shutdown()
        refusing.server_close()
    (row,) = _queued(sessions)
    error = row.last_error or ""
    assert row.sent_at is None and row.attempts == 1 and "401" in error
    assert TOKEN not in error


def test_the_waits_are_the_customer_outboxs():
    from api.events import outbox

    assert push.BACKOFF_SECONDS == outbox.BACKOFF_SECONDS
    assert push.LAST_ERROR_MAX == outbox.LAST_ERROR_MAX


def test_a_round_stops_at_the_first_failure(sessions, server):
    """A server that is down costs one timeout per round, not one per row."""
    server.stop()
    with sessions() as db:
        push.queue(db, "first", START)
        push.queue(db, "second", START)
        assert push.deliver_once(db, START) == 0
    assert [row.attempts for row in _queued(sessions)] == [1, 0]


# --- the sender thread (#853) -------------------------------------------------------


class _Db:
    def __enter__(self) -> "_Db":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_sender_runs_rounds_survives_a_raising_one_and_stops(monkeypatch, caplog):
    rounds: list[int] = []

    def deliver(db: Any, now: Any) -> int:
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("a bad round")
        return 0

    monkeypatch.setattr(push, "INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(push, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(push, "deliver_once", deliver)
    monkeypatch.setattr(settings, "operator_push_url", "http://push.test/")
    monkeypatch.setattr(settings, "operator_push_token", TOKEN)
    with caplog.at_level("ERROR"):
        stop = push.start()
        assert stop is not None
        # The raising round did not end the loop: a later round came.
        assert _wait_for(lambda: len(rounds) >= 3)
    assert "Operator push round failed" in caplog.text
    stop.set()
    (sender,) = [t for t in threading.enumerate() if t.name == "operator-push"]
    sender.join(timeout=5)
    assert not sender.is_alive()
    seen = len(rounds)
    time.sleep(0.05)
    assert len(rounds) == seen


def test_no_push_server_means_no_sender(monkeypatch):
    monkeypatch.setattr(settings, "operator_push_url", "")
    monkeypatch.setattr(settings, "operator_push_token", "")
    assert push.start() is None
    assert not [t for t in threading.enumerate() if t.name == "operator-push"]


def test_the_dashboard_starts_the_sender_and_stops_it_with_the_app(monkeypatch):
    from fastapi.testclient import TestClient

    from api.dashboard import app as web

    stop = threading.Event()
    monkeypatch.setattr(push, "start", lambda: stop)
    with TestClient(web.app):
        assert not stop.is_set()
    assert stop.is_set()


# --- a push that cannot be sent shows on the home page (#853) -----------------------


def test_pushes_stuck_past_the_ladder_are_counted_and_shown(
    client, factors, clock, sessions, server
):
    server.stop()
    _signed_in(client, factors, clock, sessions)
    with sessions() as db:
        push.queue(db, "never goes out", clock.at)
        assert push.stuck(db) == 0
    assert "could not be sent" not in client.get("/").text

    # Four failed rounds walk the ladder; the fifth is past it.
    at = clock.at
    for _ in range(len(push.BACKOFF_SECONDS)):
        with sessions() as db:
            push.deliver_once(db, at)
        at = _queued(sessions)[0].next_attempt_at
        with sessions() as db:
            assert push.stuck(db) == 0
    with sessions() as db:
        push.deliver_once(db, at)
        assert push.stuck(db) == 1
    assert "1 push to the ops topic could not be sent" in client.get("/").text


def test_pushes_waiting_without_a_push_server_show_a_banner(
    client, factors, clock, sessions, monkeypatch
):
    monkeypatch.setattr(settings, "operator_push_url", "")
    monkeypatch.setattr(settings, "operator_push_token", "")
    _signed_in(client, factors, clock, sessions)
    with sessions() as db:
        assert push.unsent_without_server(db) == 0
    assert "No push server is set" not in client.get("/").text
    with sessions() as db:
        push.queue(db, "waits", clock.at)
        assert push.unsent_without_server(db) == 1
        assert push.stuck(db) == 0
    assert (
        "No push server is set, so 1 push to the ops topic is" in client.get("/").text
    )


def test_no_banner_for_unsent_rows_when_a_push_server_is_set(
    client, factors, clock, sessions, server
):
    _signed_in(client, factors, clock, sessions)
    with sessions() as db:
        push.queue(db, "due soon", clock.at)
        assert push.unsent_without_server(db) == 0
    assert "No push server is set" not in client.get("/").text


def test_a_sent_push_is_not_stuck(sessions, server):
    with sessions() as db:
        push.queue(db, "goes out", START)
        push.deliver_once(db, START)
        assert push.stuck(db) == 0


def test_a_push_whose_commit_fails_after_the_send_goes_out_twice(sessions, server):
    """#861, pinned: at least once. The push is posted, then `sent_at` is
    committed; when that commit fails the row is still unsent and the next
    round posts it again."""
    with sessions() as db:
        push.queue(db, "once or twice", START)

    def broken() -> None:
        raise RuntimeError("the database went away")

    with sessions() as db:
        real = db.commit
        db.commit = broken  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            push.deliver_once(db, START)
        db.commit = real  # type: ignore[method-assign]
        db.rollback()
    (row,) = _queued(sessions)
    assert row.sent_at is None

    with sessions() as db:
        assert push.deliver_once(db, START) == 1
    assert [body["message"] for body, _ in server.received] == ["once or twice"] * 2


def test_sent_pushes_older_than_the_keep_period_are_pruned_by_the_next_one(
    sessions, server
):
    """#861: `operator_pushes` was never pruned. A sent row goes with the next
    push once `SENT_KEEP` has passed; an unsent one never does."""
    with sessions() as db:
        push.queue(db, "old and sent", START)
        assert push.deliver_once(db, START) == 1
        push.queue(db, "just inside", START + push.SENT_KEEP - timedelta(seconds=1))
    assert [row.message for row in _queued(sessions)] == ["old and sent", "just inside"]

    with sessions() as db:
        push.queue(db, "new", START + push.SENT_KEEP)
    assert [row.message for row in _queued(sessions)] == ["just inside", "new"]


def test_a_prune_that_fails_still_queues_the_push(sessions, server, monkeypatch):
    """#880: the prune ran first in the push's transaction, so a failing
    `DELETE` lost the takeover push. Now the push is in before the prune."""
    with sessions() as db:
        push.queue(db, "old and sent", START)
        assert push.deliver_once(db, START) == 1

    def broken(*args, **kwargs):
        raise RuntimeError("lock wait timeout")

    with sessions() as db:
        monkeypatch.setattr(db, "execute", broken)
        push.queue(db, "takeover", START + push.SENT_KEEP)
    # The push is queued, and the old row waits for the next prune.
    assert [row.message for row in _queued(sessions)] == ["old and sent", "takeover"]

    with sessions() as db:
        push.queue(db, "next", START + push.SENT_KEEP)
    assert [row.message for row in _queued(sessions)] == ["takeover", "next"]


def test_an_unsent_push_is_never_pruned(sessions, monkeypatch):
    monkeypatch.setattr(settings, "operator_push_url", "")
    with sessions() as db:
        push.queue(db, "waiting", START)
        push.queue(db, "later", START + push.SENT_KEEP * 3)
    assert [row.message for row in _queued(sessions)] == ["waiting", "later"]
