"""domain/status/config: Live-Schalter der Kopfzeile - Pause, Lieferung, Wartezeit, Audit (T-3.2)."""

import threading
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.status import config as switches
from api.domain.status.waits import current_wait
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


def wait_of(config: ServiceConfig, service: str) -> int:
    return current_wait(config, service, utcnow())


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


def test_wartezeit_je_service_getrennt(session, tenant_id):
    vorher = session.get(ServiceConfig, tenant_id)
    abholung, lieferung = wait_of(vorher, "pickup"), wait_of(vorher, "delivery")

    config = switches.change_wait(session, tenant_id, "delivery", 15)

    assert wait_of(config, "delivery") == lieferung + 15
    assert wait_of(config, "pickup") == abholung
    eintrag = audit(session, tenant_id)[-1]
    assert eintrag.action == switches.ACTION_WAIT
    assert eintrag.payload == {
        "service": "delivery",
        "step": 15,
        "from": lieferung,
        "to": lieferung + 15,
        "until": eintrag.payload["until"],
    }


def test_wartezeit_laesst_sich_senken(session, tenant_id):
    switches.change_wait(session, tenant_id, "pickup", 15)
    vorher = wait_of(session.get(ServiceConfig, tenant_id), "pickup")

    config = switches.change_wait(session, tenant_id, "pickup", -15)

    assert wait_of(config, "pickup") == vorher - 15


def test_wartezeit_hat_eine_obergrenze(session, tenant_id):
    for _ in range(20):
        config = switches.change_wait(session, tenant_id, "pickup", 15)

    assert wait_of(config, "pickup") == switches.MAX_WAIT_MINUTES
    # Am Deckel angekommen, schreibt ein weiterer Tap nichts mehr.
    anzahl = len(audit(session, tenant_id))
    switches.change_wait(session, tenant_id, "pickup", 15)
    assert len(audit(session, tenant_id)) == anzahl


def test_wartezeit_hat_eine_untergrenze(session, tenant_id):
    for _ in range(20):
        config = switches.change_wait(session, tenant_id, "delivery", -15)

    assert wait_of(config, "delivery") == switches.MIN_WAIT_MINUTES
    anzahl = len(audit(session, tenant_id))
    switches.change_wait(session, tenant_id, "delivery", -15)
    assert len(audit(session, tenant_id)) == anzahl


def test_senken_erhoeht_nie_einen_wert_unter_der_untergrenze(session, tenant_id):
    """Steht schon weniger als die Untergrenze in der DB, macht "-15" daraus nicht mehr."""
    config = session.get(ServiceConfig, tenant_id)
    config.pickup_wait_standard_minutes = 5
    session.commit()

    config = switches.change_wait(session, tenant_id, "pickup", -15)

    assert wait_of(config, "pickup") == 5
    config = switches.change_wait(session, tenant_id, "pickup", 15)
    assert wait_of(config, "pickup") == 15


def test_the_lower_limit_does_not_take_the_wait_off_the_grid(session, tenant_id):
    """Maxi, 02.11.2026: 30 down to 10 and back gave 25 and 40 instead of 30 and 45."""
    config = session.get(ServiceConfig, tenant_id)
    config.pickup_wait_standard_minutes = 30
    session.commit()

    seen = []
    for step in (-15, -15, -15, 15, 15, 15):
        config = switches.change_wait(session, tenant_id, "pickup", step)
        seen.append(wait_of(config, "pickup"))

    assert seen == [15, 10, 10, 15, 30, 45]


@pytest.mark.parametrize(
    "before, step, after",
    [
        (25, 15, 30),
        (25, -15, 15),
        (40, 15, 45),
        (40, -15, 30),
        (170, 15, 180),
        (175, 15, 180),
        (200, 15, 200),
        (200, -15, 195),
    ],
)
def test_a_value_off_the_grid_goes_to_the_next_stop(
    session, tenant_id, before, step, after
):
    """A wait stored before this rule, or typed in later, joins the grid on its next tap."""
    config = session.get(ServiceConfig, tenant_id)
    config.pickup_wait_standard_minutes = before
    session.commit()

    config = switches.change_wait(session, tenant_id, "pickup", step)

    assert wait_of(config, "pickup") == after


@pytest.mark.parametrize(("service", "minuten"), [("pickup", 0), ("dinein", 15)])
def test_wartezeit_ungueltige_eingabe(session, tenant_id, service, minuten):
    with pytest.raises(InvalidInput):
        switches.change_wait(session, tenant_id, service, minuten)


def test_ohne_service_config_klare_meldung(session):
    with pytest.raises(NotFound):
        switches.pause_ai(session, uuid.uuid4())


@pytest.mark.parametrize(
    "setter, field, action",
    [
        (switches.set_pickup, "pickup_enabled", switches.ACTION_PICKUP),
        (
            switches.set_reservations,
            "reservations_enabled",
            switches.ACTION_RESERVATIONS,
        ),
    ],
)
def test_a_service_switch_stands_in_the_audit_once(
    session, tenant_id, setter, field, action
):
    setter(session, tenant_id, False)
    config = setter(session, tenant_id, False)

    assert getattr(config, field) is False
    entries = [e for e in audit(session, tenant_id) if e.action == action]
    assert [e.payload for e in entries] == [{"from": True, "to": False}]


@pytest.mark.parametrize("setter", [switches.set_pickup, switches.set_reservations])
def test_the_stream_sees_a_service_switch(session, tenant_id, setter):
    before = switches.config_change_token(session, tenant_id)
    setter(session, tenant_id, False)

    assert switches.config_change_token(session, tenant_id) != before


def _wait_audits(session, tenant_id) -> list[dict]:
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .where(AuditLog.action == switches.ACTION_WAIT)
    ).all()
    return [row.payload for row in rows]


def test_a_typed_wait_is_taken_and_stands_in_the_audit(session, tenant_id):
    """T-3.9: 17 is not on the grid of the stepper, and the team may want it."""
    before = wait_of(session.get(ServiceConfig, tenant_id), "pickup")

    config = switches.set_wait(session, tenant_id, "pickup", 17)

    assert wait_of(config, "pickup") == 17
    entry = _wait_audits(session, tenant_id)[-1]
    assert (entry["typed"], entry["from"], entry["to"]) == (True, before, 17)


def test_a_typed_wait_touches_only_its_service(session, tenant_id):
    before = wait_of(session.get(ServiceConfig, tenant_id), "pickup")

    config = switches.set_wait(session, tenant_id, "delivery", 75)

    assert wait_of(config, "delivery") == 75
    assert wait_of(config, "pickup") == before


@pytest.mark.parametrize(
    "minutes", [switches.MIN_WAIT_MINUTES, switches.MAX_WAIT_MINUTES]
)
def test_a_typed_wait_may_stand_on_a_limit(session, tenant_id, minutes):
    config = switches.set_wait(session, tenant_id, "pickup", minutes)

    assert wait_of(config, "pickup") == minutes


@pytest.mark.parametrize(
    "minutes",
    [0, -5, switches.MIN_WAIT_MINUTES - 1, switches.MAX_WAIT_MINUTES + 1],
)
def test_a_typed_wait_outside_the_limits_changes_nothing(session, tenant_id, minutes):
    before = wait_of(session.get(ServiceConfig, tenant_id), "pickup")

    with pytest.raises(InvalidInput) as refused:
        switches.set_wait(session, tenant_id, "pickup", minutes)

    assert "10 bis 180 Minuten" in refused.value.say
    session.rollback()
    assert wait_of(session.get(ServiceConfig, tenant_id), "pickup") == before
    assert _wait_audits(session, tenant_id) == []


def test_the_same_wait_typed_again_writes_nothing(session, tenant_id):
    switches.set_wait(session, tenant_id, "pickup", 17)

    switches.set_wait(session, tenant_id, "pickup", 17)

    assert len(_wait_audits(session, tenant_id)) == 1


def test_a_typed_wait_survives_a_restart(engine, session, tenant_id):
    """Maxi, 02.11.2026: set 15 and it stays 15. A new session reads the database,
    which is all a restarted process has."""
    switches.set_wait(session, tenant_id, "pickup", 15)

    with Session(engine) as restarted:
        assert wait_of(restarted.get(ServiceConfig, tenant_id), "pickup") == 15


def test_an_unknown_service_is_refused(session, tenant_id):
    with pytest.raises(InvalidInput):
        switches.set_wait(session, tenant_id, "dinein", 30)


def test_gleichzeitige_taps_gehen_nicht_verloren(engine, session, tenant_id):
    """Zwei Tablets tippen gleichzeitig "Wartezeit Abholung +15": das ergibt +30, nicht +15."""
    config = session.get(ServiceConfig, tenant_id)
    config.pickup_wait_standard_minutes = start = 30
    session.commit()
    barrier = threading.Barrier(2)

    def tap():
        with Session(engine) as s:
            barrier.wait()
            switches.change_wait(s, tenant_id, "pickup", 15)

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session.expire_all()
    assert wait_of(session.get(ServiceConfig, tenant_id), "pickup") == start + 30


def test_token_aendert_sich_mit_jedem_schalter(session, tenant_id):
    vorher = switches.config_change_token(session, tenant_id)

    switches.set_delivery(session, tenant_id, False)

    assert switches.config_change_token(session, tenant_id) != vorher
    assert switches.config_change_token(session, uuid.uuid4()) == "-"


def test_token_sieht_auch_direkte_aenderungen_in_der_db(session, tenant_id):
    """Befund Codex P2 (PR #111): updated_at setzt nur das ORM.

    Ein rohes UPDATE laesst updated_at stehen; haengt der Fingerabdruck nur daran,
    zeigen alle Tablets den alten Stand, bis jemand am Tablet schaltet.
    """
    from sqlalchemy import text

    vorher = switches.config_change_token(session, tenant_id)

    session.execute(
        text("UPDATE service_config SET call_mode = 'paused' WHERE tenant_id = :t"),
        {"t": tenant_id},
    )
    session.commit()

    assert switches.config_change_token(session, tenant_id) != vorher
