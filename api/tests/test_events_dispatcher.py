"""events/: Outbox schreiben, Dispatcher mit Backoff, Fake-n8n, Alarm bei failed (T-1.12)."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.events import enqueue
from api.events.dispatcher import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    dispatch_once,
    send_to_n8n,
)
from api.events.types import ALL as EVENT_TYPES
from api.events.types import RESERVATION_CONFIRMED
from api.models import OutboxEvent
from api.models.outbox import EVENT_TYPES as TABELLEN_TYPEN
from scripts.seed import seed

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)


class FakeN8n:
    """Nimmt Ereignisse entgegen; `fail_times` Zustellungen scheitern vorher."""

    def __init__(self, fail_times: int = 0, error: Exception | None = None):
        self.fail_times = fail_times
        self.error = error or httpx.ConnectError("n8n nicht erreichbar")
        self.received: list[dict] = []

    def __call__(self, event: OutboxEvent) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.received.append(
            {"id": event.id, "event_type": event.event_type, "payload": event.payload}
        )


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def add_event(session, tenant_id, **overrides) -> OutboxEvent:
    event = enqueue(
        session,
        tenant_id=tenant_id,
        event_type=overrides.pop("event_type", RESERVATION_CONFIRMED),
        payload=overrides.pop("payload", {"reservation_id": "r1", "party_size": 4}),
    )
    session.flush()
    # Der Server-Default setzt next_attempt_at auf die echte Uhr; die Tests rechnen
    # mit NOW, sonst waere kein Ereignis faellig.
    overrides.setdefault("next_attempt_at", NOW)
    for feld, wert in overrides.items():
        setattr(event, feld, wert)
    session.commit()
    return event


def test_typen_stimmen_mit_der_tabelle_ueberein():
    assert set(EVENT_TYPES) == set(TABELLEN_TYPEN)


def test_enqueue_lehnt_unbekannten_typ_ab(session, tenant_id):
    with pytest.raises(ValueError, match="unbekannter Ereignistyp"):
        enqueue(session, tenant_id=tenant_id, event_type="order.exploded", payload={})


def test_enqueue_committet_nicht(session, tenant_id):
    enqueue(
        session, tenant_id=tenant_id, event_type=RESERVATION_CONFIRMED, payload={"a": 1}
    )
    session.rollback()

    assert session.query(OutboxEvent).count() == 0


def test_faelliges_ereignis_geht_an_n8n_und_wird_sent(session, tenant_id):
    event = add_event(session, tenant_id)
    n8n = FakeN8n()

    result = dispatch_once(session, n8n, now=NOW)

    session.refresh(event)
    assert (result.sent, result.retried, result.failed) == (1, 0, 0)
    assert [r["id"] for r in n8n.received] == [event.id]
    assert n8n.received[0]["payload"] == {"reservation_id": "r1", "party_size": 4}
    assert event.status == "sent"
    assert event.sent_at == NOW
    assert event.attempts == 1
    assert event.last_error is None


def test_gesendetes_ereignis_wird_nicht_erneut_zugestellt(session, tenant_id):
    add_event(session, tenant_id)
    n8n = FakeN8n()
    dispatch_once(session, n8n, now=NOW)

    result = dispatch_once(session, n8n, now=NOW + timedelta(hours=1))

    assert result.handled == 0
    assert len(n8n.received) == 1


def test_fehlschlag_plant_ersten_backoff_und_merkt_den_fehler(session, tenant_id):
    event = add_event(session, tenant_id)
    n8n = FakeN8n(fail_times=1)

    result = dispatch_once(session, n8n, now=NOW)

    session.refresh(event)
    assert (result.sent, result.retried, result.failed) == (0, 1, 0)
    assert event.status == "pending"
    assert event.attempts == 1
    assert event.next_attempt_at == NOW + timedelta(seconds=BACKOFF_SECONDS[0])
    assert "ConnectError" in event.last_error


def test_ereignis_vor_seiner_zeit_bleibt_liegen(session, tenant_id):
    add_event(session, tenant_id, next_attempt_at=NOW + timedelta(seconds=30))
    n8n = FakeN8n()

    result = dispatch_once(session, n8n, now=NOW)

    assert result.handled == 0
    assert n8n.received == []


def test_backoff_folgt_der_reihe_und_endet_in_failed(session, tenant_id, caplog):
    event = add_event(session, tenant_id)
    n8n = FakeN8n(fail_times=MAX_ATTEMPTS)
    moment = NOW

    geplant = []
    for _ in range(len(BACKOFF_SECONDS)):
        dispatch_once(session, n8n, now=moment)
        session.refresh(event)
        geplant.append(int((event.next_attempt_at - moment).total_seconds()))
        moment = event.next_attempt_at

    assert geplant == list(BACKOFF_SECONDS)
    assert event.status == "pending"

    with caplog.at_level(logging.ERROR):
        result = dispatch_once(session, n8n, now=moment)

    session.refresh(event)
    assert (result.failed, result.retried) == (1, 0)
    assert event.status == "failed"
    assert event.attempts == MAX_ATTEMPTS
    assert "Alarm" in caplog.text


def test_mehrere_ereignisse_in_der_reihenfolge_ihrer_faelligkeit(session, tenant_id):
    spaet = add_event(session, tenant_id, next_attempt_at=NOW - timedelta(seconds=5))
    frueh = add_event(session, tenant_id, next_attempt_at=NOW - timedelta(seconds=60))
    n8n = FakeN8n()

    result = dispatch_once(session, n8n, now=NOW)

    assert result.sent == 2
    assert [r["id"] for r in n8n.received] == [frueh.id, spaet.id]


def test_limit_begrenzt_den_durchlauf(session, tenant_id):
    add_event(session, tenant_id)
    add_event(session, tenant_id)
    n8n = FakeN8n()

    result = dispatch_once(session, n8n, limit=1, now=NOW)

    assert result.sent == 1
    assert (
        session.query(OutboxEvent).filter(OutboxEvent.status == "pending").count() == 1
    )


def test_ein_fehlschlag_blockiert_die_uebrigen_nicht(session, tenant_id):
    add_event(session, tenant_id, next_attempt_at=NOW - timedelta(seconds=60))
    add_event(session, tenant_id, next_attempt_at=NOW - timedelta(seconds=5))
    n8n = FakeN8n(fail_times=1)

    result = dispatch_once(session, n8n, now=NOW)

    assert (result.sent, result.retried) == (1, 1)


def test_send_to_n8n_setzt_idempotenz_kopf_und_wirft_bei_5xx(session, tenant_id):
    event = add_event(session, tenant_id)
    gesehen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send_to_n8n(event, client=client)

    assert gesehen[0].headers["X-Idempotency-Key"] == str(event.id)
    assert b'"event_type":"reservation.confirmed"' in gesehen[0].content

    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(502))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            send_to_n8n(event, client=client)
