"""Tests für create_reservation-Domainlogik."""

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.domain.reservations import create_reservation
from api.models import Call, Reservation, Tenant
from api.tests.conftest import p95_ms


def _make_tenant(db: Session) -> Tenant:
    """Erstellt einen Testmandanten."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="<Pilotbetrieb>",
        timezone="Europe/Berlin",
    )
    db.add(tenant)
    db.commit()
    return tenant


def _make_call(db: Session, tenant_id: uuid.UUID) -> Call:
    """Erstellt einen Testandruf."""
    call = Call(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        external_session_id=f"test-session-{uuid.uuid4().hex[:8]}",
        started_at=utcnow(),
        delete_after=(utcnow() + timedelta(days=1)).date(),
    )
    db.add(call)
    db.commit()
    return call


class TestCreateReservationBasic:
    """Normalfall: neue Reservierung anlegen."""

    def test_create_draft_reservation(self, db: Session):
        """Eine neue Reservierung als draft anlegen."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="test-key-1",
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1, hours=2),
        )

        assert result.status == "draft"
        assert result.reservation_id is not None
        assert "Müller" in result.readback
        assert "4 Personen" in result.readback
        assert "Passt das so?" in result.readback

    def test_readback_single_guest(self, db: Session):
        """Readback mit einer Person im Singular."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="single-guest",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=1,
            reserved_for=utcnow() + timedelta(days=1),
        )

        assert "eine Person" in result.readback

    def test_readback_multiple_guests(self, db: Session):
        """Readback mit mehreren Personen."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="multi-guest",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=6,
            reserved_for=utcnow() + timedelta(days=1),
        )

        assert "6 Personen" in result.readback

    def test_readback_with_note(self, db: Session):
        """Readback mit Zusatznotiz."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="note-test",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=2,
            reserved_for=utcnow() + timedelta(days=1),
            note="Kinderstuhl",
        )

        assert "Kinderstuhl vorhanden" in result.readback

    def test_readback_date_today(self, db: Session):
        """Readback mit „heute" (Reservierung in weniger als 5 Stunden)."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        now = utcnow()
        zone = ZoneInfo("Europe/Berlin")
        local_now = now.astimezone(zone)
        later_today = local_now.replace(hour=20, minute=0, second=0, microsecond=0)
        if later_today < local_now:
            pytest.skip("Reservierung wäre in der Vergangenheit")

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="today-test",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=2,
            reserved_for=later_today.replace(tzinfo=zone),
        )

        assert "heute" in result.readback

    def test_readback_date_tomorrow(self, db: Session):
        """Readback mit „morgen"."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        zone = ZoneInfo("Europe/Berlin")
        tomorrow_at_18 = (
            utcnow().astimezone(zone).replace(
                hour=18, minute=0, second=0, microsecond=0
            )
            + timedelta(days=1)
        ).replace(tzinfo=zone)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="tomorrow-test",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=2,
            reserved_for=tomorrow_at_18,
        )

        assert "morgen" in result.readback

    def test_readback_date_by_weekday(self, db: Session):
        """Readback mit Wochentagsangabe für Datum in > 1 Tagen."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        zone = ZoneInfo("Europe/Berlin")
        in_five_days = (
            utcnow().astimezone(zone).replace(
                hour=18, minute=0, second=0, microsecond=0
            )
            + timedelta(days=5)
        ).replace(tzinfo=zone)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="weekday-test",
            guest_name="Schmidt",
            phone="+49721555123",
            party_size=2,
            reserved_for=in_five_days,
        )

        weekdays = (
            "Montag",
            "Dienstag",
            "Mittwoch",
            "Donnerstag",
            "Freitag",
            "Samstag",
            "Sonntag",
        )
        assert any(day in result.readback for day in weekdays)


class TestIdempotency:
    """Idempotenzverhalten: gleicher Schlüssel → gleiche Antwort."""

    def test_idempotent_returns_same_result(self, db: Session):
        """Zwei Aufrufe mit gleichem Schlüssel geben die gleiche Antwort."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)
        key = "idempotent-key-123"

        result1 = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key=key,
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1),
        )

        result2 = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key=key,
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1),
        )

        assert result1.reservation_id == result2.reservation_id
        assert result1.readback == result2.readback
        assert result1.status == result2.status

    def test_idempotent_single_reservation_in_db(self, db: Session):
        """Trotz zweimaligen Aufrufs wird nur eine Reservierung gespeichert."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)
        key = "idempotent-key-456"

        create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key=key,
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1),
        )

        create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key=key,
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1),
        )

        count = db.query(Reservation).filter_by(idempotency_key=key).count()
        assert count == 1


class TestPhoneNormalization:
    """Telefonnummern-Normalisierung zu E.164."""

    def test_phone_plus_format(self, db: Session):
        """Eingabe im +49-Format wird akzeptiert."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="phone-plus",
            guest_name="Müller",
            phone="+49721555123",
            party_size=1,
            reserved_for=utcnow() + timedelta(days=1),
        )

        res = db.query(Reservation).filter_by(id=result.reservation_id).one()
        assert res.phone == "+49721555123"

    def test_phone_0049_format(self, db: Session):
        """Eingabe im 0049-Format wird zu +49 normalisiert."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="phone-0049",
            guest_name="Müller",
            phone="00497215551234",
            party_size=1,
            reserved_for=utcnow() + timedelta(days=1),
        )

        res = db.query(Reservation).filter_by(id=result.reservation_id).one()
        assert res.phone == "+497215551234"

    def test_phone_0_format(self, db: Session):
        """Eingabe im 0-Format wird zu +49 normalisiert."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="phone-0",
            guest_name="Müller",
            phone="07215551234",
            party_size=1,
            reserved_for=utcnow() + timedelta(days=1),
        )

        res = db.query(Reservation).filter_by(id=result.reservation_id).one()
        assert res.phone == "+497215551234"

    def test_phone_with_spaces(self, db: Session):
        """Eingabe mit Leerzeichen wird normalisiert."""
        tenant = _make_tenant(db)
        call = _make_call(db, tenant.id)

        result = create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key="phone-spaces",
            guest_name="Müller",
            phone="+49 721 555 1234",
            party_size=1,
            reserved_for=utcnow() + timedelta(days=1),
        )

        res = db.query(Reservation).filter_by(id=result.reservation_id).one()
        assert res.phone == "+497215551234"


class TestErrors:
    """Fehlerbehandlung."""

    def test_tenant_not_found(self, db: Session, call: Call):
        """Fehler wenn Mandant nicht existiert."""
        from api.core.errors import NotFound

        with pytest.raises(NotFound, match="Mandant unbekannt"):
            create_reservation(
                db,
                uuid.uuid4(),
                call.id,
                idempotency_key="tenant-missing",
                guest_name="Müller",
                phone="+49721555123",
                party_size=1,
                reserved_for=utcnow() + timedelta(days=1),
            )

    def test_call_not_found(self, db: Session, tenant: Tenant):
        """Fehler wenn Anruf nicht existiert."""
        from api.core.errors import NotFound

        with pytest.raises(NotFound, match="Anruf nicht gefunden"):
            create_reservation(
                db,
                tenant.id,
                uuid.uuid4(),
                idempotency_key="call-missing",
                guest_name="Müller",
                phone="+49721555123",
                party_size=1,
                reserved_for=utcnow() + timedelta(days=1),
            )

    def test_invalid_phone(self, db: Session, tenant: Tenant, call: Call):
        """Fehler bei ungültiger Telefonnummer."""
        from api.core.errors import InvalidInput

        with pytest.raises(InvalidInput, match="Telefonnummer"):
            create_reservation(
                db,
                tenant.id,
                call.id,
                idempotency_key="bad-phone",
                guest_name="Müller",
                phone="",
                party_size=1,
                reserved_for=utcnow() + timedelta(days=1),
            )


class TestLatency:
    """Latenzanforderungen: p95 < 300 ms."""

    @p95_ms(max_ms=300)
    def test_create_reservation_latency(
        self, db: Session, tenant: Tenant, call: Call
    ):
        """create_reservation sollte unter 300 ms p95 sein."""
        create_reservation(
            db,
            tenant.id,
            call.id,
            idempotency_key=f"latency-{uuid.uuid4()}",
            guest_name="Müller",
            phone="+49721555123",
            party_size=4,
            reserved_for=utcnow() + timedelta(days=1),
        )
