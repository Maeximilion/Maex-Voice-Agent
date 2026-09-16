"""core/ids: Idempotenz-Schlüssel sind deterministisch und unterscheiden Aufruf, Tool und Eingabe."""

import uuid

from api.core.ids import idempotency_key, new_id


def test_new_id_ist_uuid4():
    assert new_id().version == 4


def test_schluessel_deterministisch():
    call = uuid.uuid4()
    a = idempotency_key(call, "create_reservation", 4, "2026-09-15T18:30:00+02:00")
    b = idempotency_key(call, "create_reservation", 4, "2026-09-15T18:30:00+02:00")
    assert a == b and len(a) == 32


def test_schluessel_trennt_aufruf_tool_und_eingabe():
    call = uuid.uuid4()
    base = idempotency_key(call, "create_reservation", 4)
    assert idempotency_key(uuid.uuid4(), "create_reservation", 4) != base
    assert idempotency_key(call, "confirm", 4) != base
    assert idempotency_key(call, "create_reservation", 5) != base
    assert idempotency_key(call, "create_reservation", "4", "") != base
