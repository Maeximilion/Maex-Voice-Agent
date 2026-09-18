"""Live-Schalter des Betriebs: service_config lesen und schreiben (docs/11 §domain/status).

Die Kopfzeile der Betriebsansicht (docs/06 §3) schaltet hier: KI pausieren und
wieder einschalten, Lieferung an und aus, Wartezeit erhöhen. Jede Änderung
schreibt audit_log mit dem Wert davor und danach, damit sich später nachlesen
lässt, wer um 18:05 die Lieferung abgestellt hat.

Jede Funktion sperrt die Zeile (`FOR UPDATE`). Zwei Tablets, die gleichzeitig
"Wartezeit +15" tippen, ergeben +30 und nicht zweimal denselben Wert +15.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.models import AuditLog, ServiceConfig

ACTOR_TABLET = "gui:tablet"
ACTION_MODE = "service_config.mode_changed"
ACTION_DELIVERY = "service_config.delivery_changed"
ACTION_WAIT = "service_config.wait_raised"

PAUSED = "paused"
# Wohin "KI einschalten" zurückkehrt, wenn nicht nachlesbar ist, was vor der
# Pause galt: der Modus, in dem die KI nie selbst abnimmt (docs/03 Default).
RESUME_FALLBACK = "shadow"
# Obergrenze gegen Dauertippen: mehr als drei Stunden sagt die KI keinem Gast an.
MAX_WAIT_MINUTES = 180


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


def raise_wait(
    session: Session, tenant_id: uuid.UUID, minutes: int, actor: str = ACTOR_TABLET
) -> ServiceConfig:
    """Erhöht Abhol- und Lieferwartezeit um denselben Betrag, gedeckelt.

    Beide zusammen, weil die Kopfzeile einen Knopf "Wartezeit +15" hat und nicht
    zwei: wenn die Küche voll ist, ist sie für beide voll (docs/06 §3).
    """
    if minutes <= 0:
        raise InvalidInput(f"Wartezeit muss steigen, nicht {minutes} Minuten")
    config = _locked(session, tenant_id)
    before = (config.pickup_wait_minutes, config.delivery_wait_minutes)
    after = tuple(min(value + minutes, MAX_WAIT_MINUTES) for value in before)
    if after != before:
        _audit(
            session,
            config,
            ACTION_WAIT,
            actor,
            {
                "step": minutes,
                "from": {"pickup": before[0], "delivery": before[1]},
                "to": {"pickup": after[0], "delivery": after[1]},
            },
        )
        config.pickup_wait_minutes, config.delivery_wait_minutes = after
    session.commit()
    return config


def config_change_token(session: Session, tenant_id: uuid.UUID) -> str:
    """Fingerabdruck der Kopfzeile für den Ereignisstrom (gui/sse.py).

    updated_at reicht: jede Änderung an der Zeile setzt es neu, auch die aus
    einem anderen Prozess oder direkt in der Datenbank.
    """
    updated = session.scalars(
        select(ServiceConfig.updated_at).where(ServiceConfig.tenant_id == tenant_id)
    ).first()
    return updated.isoformat() if updated else "-"
