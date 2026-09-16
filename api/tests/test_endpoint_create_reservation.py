"""Integration tests für POST /v1/tools/create_reservation."""

import uuid
from datetime import timedelta

import pytest
from httpx import Client
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.models import Call, Reservation, Tenant
from api.tests.conftest import make_auth_header, p95_ms


@pytest.fixture
def tenant(db: Session) -> Tenant:
    """Standardmandant für Tests."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="<Pilotbetrieb>",
        timezone="Europe/Berlin",
    )
    db.add(tenant)
    db.commit()
    return tenant


@pytest.fixture
def call(db: Session, tenant: Tenant) -> Call:
    """Standardanruf für Tests."""
    call = Call(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        external_session_id="test-session-123",
        started_at=utcnow(),
        delete_after=(utcnow() + timedelta(days=1)).date(),
    )
    db.add(call)
    db.commit()
    return call


class TestCreateReservationEndpoint:
    """HTTP-Endpunkt POST /v1/tools/create_reservation."""

    def test_create_reservation_success(
        self, client: Client, tenant: Tenant, call: Call
    ):
        """Erfolgreiche Reservierungserstellung."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-test-1",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "data" in data
        assert data["data"]["status"] == "draft"
        assert "readback" in data["data"]
        assert "Müller" in data["data"]["readback"]
        assert data["data"]["reservation_id"] is not None

    def test_create_reservation_with_note(
        self, client: Client, tenant: Tenant, call: Call
    ):
        """Reservierung mit Zusatznotiz."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-test-with-note",
                "guest_name": "Schmidt",
                "phone": "+49721555123",
                "party_size": 2,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
                "note": "Kinderstuhl",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "Kinderstuhl" in data["data"]["readback"]

    def test_create_reservation_missing_token(self, client: Client):
        """Fehler ohne Token."""
        response = client.post(
            "/v1/tools/create_reservation",
            json={
                "call_id": str(uuid.uuid4()),
                "tenant_id": str(uuid.uuid4()),
                "idempotency_key": "test",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 1,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 401

    def test_create_reservation_missing_field(
        self, client: Client, tenant: Tenant, call: Call
    ):
        """Fehler bei fehlendem Feld."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-missing-field",
                "guest_name": "Müller",
                # phone fehlt
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 422

    def test_create_reservation_invalid_party_size(
        self, client: Client, tenant: Tenant, call: Call
    ):
        """Fehler bei party_size < 1."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-invalid-size",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 0,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 422

    def test_create_reservation_unknown_tenant(
        self, client: Client, call: Call
    ):
        """Fehler bei unbekanntem Mandanten."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(uuid.uuid4()),
                "idempotency_key": "endpoint-unknown-tenant",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "not_found"

    def test_create_reservation_unknown_call(
        self, client: Client, tenant: Tenant
    ):
        """Fehler bei unbekanntem Anruf."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(uuid.uuid4()),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-unknown-call",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["error"]["code"] == "not_found"

    @p95_ms(max_ms=300)
    def test_create_reservation_endpoint_latency(
        self, client: Client, tenant: Tenant, call: Call
    ):
        """Endpunkt sollte unter 300 ms p95 sein."""
        client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": f"latency-{uuid.uuid4()}",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

    def test_create_reservation_stored_in_db(
        self, client: Client, db: Session, tenant: Tenant, call: Call
    ):
        """Reservierung wird in der Datenbank gespeichert."""
        response = client.post(
            "/v1/tools/create_reservation",
            headers=make_auth_header(),
            json={
                "call_id": str(call.id),
                "tenant_id": str(tenant.id),
                "idempotency_key": "endpoint-db-check",
                "guest_name": "Müller",
                "phone": "+49721555123",
                "party_size": 4,
                "reserved_for": (utcnow() + timedelta(days=1)).isoformat(),
            },
        )

        assert response.status_code == 200
        reservation_id = response.json()["data"]["reservation_id"]

        res = db.query(Reservation).filter_by(id=reservation_id).one()
        assert res.guest_name == "Müller"
        assert res.phone == "+49721555123"
        assert res.party_size == 4
        assert res.status == "draft"
        assert res.call_id == call.id
        assert res.tenant_id == tenant.id
