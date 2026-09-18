"""domain/status/config: Live-Schalter der Kopfzeile - Pause, Lieferung, Wartezeit, Audit (T-3.2)."""

import threading
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.status import config as switches
from api.models import AuditLog, ServiceConfig
from scripts.seed import seed


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session) -> uuid.UUID:
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def set_mode(session, tenant_id, mode: str) -> None:
    session.get(ServiceConfig, tenant_id).call_mode = mode
    session.commit()


def audit(session, tenant_id) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.entity == "service_config",
            )
            .order_by(AuditLog.id)
        )
    )


def test_pause_schaltet_ab_und_protokolliert(session, tenant_id):
    set_mode(session, tenant_id, "primary")

    config = switches.pause_ai(session, tenant_id)

    assert config.call_mode == "paused"
    [eintrag] = audit(session, tenant_id)
    assert eintrag.actor == "gui:tablet"
    assert eintrag.action == switches.ACTION_MODE
    assert eintrag.payload == {"from": "primary", "to": "paused"}


def test_doppelter_not_aus_schreibt_einmal(session, tenant_id):
    """Zweimal hektisch getippt: ein Eintrag, und die Pause bleibt."""
    set_mode(session, tenant_id, "overflow")

    switches.pause_ai(session, tenant_id)
    config = switches.pause_ai(session, tenant_id)

    assert config.call_mode == "paused"
    assert len(audit(session, tenant_id)) == 1


@pytest.mark.parametrize("vorher", ["primary", "overflow", "shadow"])
def test_einschalten_stellt_den_modus_von_davor_her(session, tenant_id, vorher):
    set_mode(session, tenant_id, vorher)
    switches.pause_ai(session, tenant_id)

    config = switches.resume_ai(session, tenant_id)

    assert config.call_mode == vorher
    assert audit(session, tenant_id)[-1].payload == {"from": "paused", "to": vorher}


def test_einschalten_ohne_vorgeschichte_nimmt_den_sicheren_modus(session, tenant_id):
    """Pause ohne Logeintrag (z. B. direkt in der DB gesetzt): nie blind auf primary."""
    set_mode(session, tenant_id, "paused")

    config = switches.resume_ai(session, tenant_id)

    assert config.call_mode == switches.RESUME_FALLBACK == "shadow"


def test_einschalten_nimmt_die_juengste_pause(session, tenant_id):
    set_mode(session, tenant_id, "primary")
    switches.pause_ai(session, tenant_id)
    switches.resume_ai(session, tenant_id)
    set_mode(session, tenant_id, "overflow")
    switches.pause_ai(session, tenant_id)

    assert switches.resume_ai(session, tenant_id).call_mode == "overflow"


def test_einschalten_ohne_pause_aendert_nichts(session, tenant_id):
    set_mode(session, tenant_id, "primary")

    assert switches.resume_ai(session, tenant_id).call_mode == "primary"
    assert audit(session, tenant_id) == []


def test_lieferung_aus_und_wieder_an(session, tenant_id):
    assert switches.set_delivery(session, tenant_id, False).delivery_enabled is False
    # Zustand, nicht Umschalten: nochmal "aus" bleibt aus.
    assert switches.set_delivery(session, tenant_id, False).delivery_enabled is False
    assert switches.set_delivery(session, tenant_id, True).delivery_enabled is True

    payloads = [e.payload for e in audit(session, tenant_id)]
    assert payloads == [{"from": True, "to": False}, {"from": False, "to": True}]


def test_wartezeit_steigt_fuer_abholung_und_lieferung(session, tenant_id):
    vorher = session.get(ServiceConfig, tenant_id)
    abholung, lieferung = vorher.pickup_wait_minutes, vorher.delivery_wait_minutes

    config = switches.raise_wait(session, tenant_id, 15)

    assert config.pickup_wait_minutes == abholung + 15
    assert config.delivery_wait_minutes == lieferung + 15
    assert audit(session, tenant_id)[-1].payload["step"] == 15


def test_wartezeit_hat_eine_obergrenze(session, tenant_id):
    for _ in range(20):
        config = switches.raise_wait(session, tenant_id, 30)

    assert config.pickup_wait_minutes == switches.MAX_WAIT_MINUTES
    assert config.delivery_wait_minutes == switches.MAX_WAIT_MINUTES
    # Am Deckel angekommen, schreibt ein weiterer Tap nichts mehr.
    anzahl = len(audit(session, tenant_id))
    switches.raise_wait(session, tenant_id, 15)
    assert len(audit(session, tenant_id)) == anzahl


@pytest.mark.parametrize("minuten", [0, -15])
def test_wartezeit_sinkt_hier_nie(session, tenant_id, minuten):
    with pytest.raises(InvalidInput):
        switches.raise_wait(session, tenant_id, minuten)


def test_ohne_service_config_klare_meldung(session):
    with pytest.raises(NotFound):
        switches.pause_ai(session, uuid.uuid4())


def test_gleichzeitige_taps_gehen_nicht_verloren(engine, session, tenant_id):
    """Zwei Tablets tippen gleichzeitig "Wartezeit +15": das ergibt +30, nicht +15."""
    start = session.get(ServiceConfig, tenant_id).pickup_wait_minutes
    barrier = threading.Barrier(2)

    def tap():
        with Session(engine) as s:
            barrier.wait()
            switches.raise_wait(s, tenant_id, 15)

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session.expire_all()
    assert session.get(ServiceConfig, tenant_id).pickup_wait_minutes == start + 30


def test_token_aendert_sich_mit_jedem_schalter(session, tenant_id):
    vorher = switches.config_change_token(session, tenant_id)

    switches.set_delivery(session, tenant_id, False)

    assert switches.config_change_token(session, tenant_id) != vorher
    assert switches.config_change_token(session, uuid.uuid4()) == "-"
