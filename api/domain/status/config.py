"""Live-Schalter des Betriebs: service_config lesen und schreiben (docs/11 §domain/status).

Die Kopfzeile der Betriebsansicht (docs/06 §3) schaltet hier: KI pausieren und
wieder einschalten, Lieferung an und aus, Wartezeit je Service ändern. Jede Änderung
schreibt audit_log mit dem Wert davor und danach, damit sich später nachlesen
lässt, wer um 18:05 die Lieferung abgestellt hat.

Jede Funktion sperrt die Zeile (`FOR UPDATE`). Zwei Tablets, die gleichzeitig
"Wartezeit +15" tippen, ergeben +30 und nicht zweimal denselben Wert +15.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core import time as clock
from api.core.errors import InvalidInput, NotFound
from api.domain.status.bridge import silence
from api.domain.status.mode import CAUTION, PAUSED, mode_at
from api.models import AuditLog, ServiceConfig, Tenant

ACTOR_TABLET = "gui:tablet"
ACTION_MODE = "service_config.mode_changed"
ACTION_DELIVERY = "service_config.delivery_changed"
ACTION_WAIT = "service_config.wait_changed"

# Wohin "KI einschalten" zurückkehrt, wenn nicht nachlesbar ist, was vor der
# Pause galt: der Modus, in dem die KI nie selbst abnimmt (docs/03 Default).
RESUME_FALLBACK = "shadow"
# Obergrenze gegen Dauertippen: mehr als drei Stunden sagt die KI keinem Gast an.
MAX_WAIT_MINUTES = 180
# Untergrenze beim Senken: schneller als zehn Minuten verspricht die KI nichts,
# auch wenn jemand mehrmals "-15" tippt (Annahme 26.09.2026, T-3.6).
MIN_WAIT_MINUTES = 10
# Service in der Kopfzeile -> Feld in service_config.
WAIT_FIELDS = {
    "pickup": "pickup_wait_minutes",
    "delivery": "delivery_wait_minutes",
}


def _locked(session: Session, tenant_id: uuid.UUID) -> ServiceConfig:
    config = session.scalars(
        select(ServiceConfig)
        .where(ServiceConfig.tenant_id == tenant_id)
        .with_for_update()
    ).first()
    if config is None:
        raise NotFound(f"service_config fehlt fuer Mandant {tenant_id}")
    return config


def _audit(
    session: Session, config: ServiceConfig, action: str, actor: str, payload: dict
) -> None:
    session.add(
        AuditLog(
            tenant_id=config.tenant_id,
            actor=actor,
            action=action,
            entity="service_config",
            entity_id=config.tenant_id,
            payload=payload,
        )
    )


def _mode_before_pause(session: Session, tenant_id: uuid.UUID) -> str:
    """Letzter Modus vor der jüngsten Pause, aus dem audit_log.

    Bewusst ohne eigene Spalte: die Pause schreibt ohnehin `from` ins Log, eine
    zweite Kopie derselben Tatsache in service_config koennte davon abweichen.
    """
    payload = session.scalars(
        select(AuditLog.payload)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == ACTION_MODE,
            AuditLog.payload["to"].astext == PAUSED,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).first()
    previous = (payload or {}).get("from")
    if not previous or previous == PAUSED:
        return RESUME_FALLBACK
    return previous


def pause_ai(
    session: Session, tenant_id: uuid.UUID, actor: str = ACTOR_TABLET
) -> ServiceConfig:
    """Not-Aus: ab dem nächsten Anruf nimmt die KI nicht mehr ab. Ohne Rückfrage."""
    config = _locked(session, tenant_id)
    if config.call_mode != PAUSED:
        _audit(
            session,
            config,
            ACTION_MODE,
            actor,
            {"from": config.call_mode, "to": PAUSED},
        )
        config.call_mode = PAUSED
    # Auch ohne Aenderung: commit gibt die Sperre frei.
    session.commit()
    return config


def resume_ai(
    session: Session, tenant_id: uuid.UUID, actor: str = ACTOR_TABLET
) -> ServiceConfig:
    """Hebt die Pause auf und stellt den Modus von davor wieder her."""
    config = _locked(session, tenant_id)
    if config.call_mode == PAUSED:
        target = _mode_before_pause(session, tenant_id)
        _audit(session, config, ACTION_MODE, actor, {"from": PAUSED, "to": target})
        config.call_mode = target
    session.commit()
    return config


def set_mode(
    session: Session,
    tenant_id: uuid.UUID,
    mode: str,
    actor: str = ACTOR_TABLET,
    now: datetime | None = None,
) -> ServiceConfig:
    """The mode switch of the header (T-8.1). Holds from the next call on, also
    against a window that began before the tap, until the next window begins
    (`mode.py`). A tap on the mode that holds changes nothing. Never into the
    emergency stop and never out of it: both have a button of their own, and
    switching the agent back on asks first (docs/06 §3)."""
    if mode not in CAUTION:
        raise InvalidInput(f"No mode {mode!r} on the switch")
    config = _locked(session, tenant_id)
    if config.call_mode == PAUSED:
        raise InvalidInput("The agent is paused; only switching it on ends that")
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound(f"Tenant {tenant_id} unknown")
    now = now or clock.utcnow()
    holding = mode_at(session, tenant, config, now).mode
    if holding != mode:
        _audit(session, config, ACTION_MODE, actor, {"from": holding, "to": mode})
        config.call_mode = mode
        config.call_mode_chosen_at = now
    session.commit()
    return config


def set_delivery(
    session: Session, tenant_id: uuid.UUID, enabled: bool, actor: str = ACTOR_TABLET
) -> ServiceConfig:
    """Lieferung an oder aus. Zustand statt Umschalten: doppelt getippt bleibt es aus."""
    config = _locked(session, tenant_id)
    if config.delivery_enabled != enabled:
        _audit(
            session,
            config,
            ACTION_DELIVERY,
            actor,
            {"from": config.delivery_enabled, "to": enabled},
        )
        config.delivery_enabled = enabled
    session.commit()
    return config


def change_wait(
    session: Session,
    tenant_id: uuid.UUID,
    service: str,
    minutes: int,
    actor: str = ACTOR_TABLET,
) -> ServiceConfig:
    """Ändert die Wartezeit eines Service um `minutes`, zwischen Unter- und Obergrenze.

    Abholung und Lieferung getrennt (T-3.6, docs/06 §3): ist nur der Fahrer
    unterwegs, bleibt die Abholzeit, wie sie ist. An einer Grenze angekommen,
    ändert ein weiterer Tap nichts und schreibt nichts.
    """
    field = WAIT_FIELDS.get(service)
    if field is None:
        raise InvalidInput(f"Unbekannter Service {service!r} fuer die Wartezeit")
    if minutes == 0:
        raise InvalidInput("Wartezeit ändert sich um 0 Minuten nicht")
    config = _locked(session, tenant_id)
    before = getattr(config, field)
    # Die Grenze gilt nur in Tipp-Richtung: steht in der DB schon weniger als die
    # Untergrenze, macht "-15" daraus nicht mehr (und "+15" über 180 nicht weniger).
    if minutes < 0:
        after = max(before + minutes, min(MIN_WAIT_MINUTES, before))
    else:
        after = min(before + minutes, max(MAX_WAIT_MINUTES, before))
    if after != before:
        _audit(
            session,
            config,
            ACTION_WAIT,
            actor,
            {"service": service, "step": minutes, "from": before, "to": after},
        )
        setattr(config, field, after)
    session.commit()
    return config


def config_change_token(
    session: Session, tenant_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Fingerabdruck der Kopfzeile für den Ereignisstrom (gui/sse.py).

    Die angezeigten Werte selbst, nicht nur updated_at: das setzt allein das ORM
    (onupdate), ein rohes UPDATE in der Datenbank laesst es stehen und die Tablets
    blieben auf dem alten Stand. updated_at bleibt dabei, damit auch eine
    Aenderung hin und wieder zurueck innerhalb eines Takts ein Signal gibt.
    The mode is the one that holds at `now`: a window that begins or ends
    writes nothing to the database, and the header still has to show it (T-8.1).
    The same goes for a print bridge that falls silent (T-8.3).
    """
    config = session.get(ServiceConfig, tenant_id, populate_existing=True)
    tenant = session.get(Tenant, tenant_id)
    if config is None or tenant is None:
        return "-"
    holding = mode_at(session, tenant, config, now)
    until = holding.planned_until.isoformat() if holding.planned_until else ""
    quiet = silence(session, tenant, now)
    return (
        f"{holding.mode}:{until}:{quiet}:{int(config.delivery_enabled)}:"
        f"{config.pickup_wait_minutes}:{config.delivery_wait_minutes}:"
        f"{config.updated_at.isoformat()}"
    )
