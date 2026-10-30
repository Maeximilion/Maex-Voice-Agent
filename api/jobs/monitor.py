"""Alarms and the day report for the operator (T-8.3, docs/02 §5, docs/13 §5).

One pass every INTERVAL_SECONDS in the dispatcher process (api/jobs/cold_path.py)
looks at each tenant and pushes to the operator, never to the team:

- the model cost of the business day above ALARM_COST_PER_DAY_CENTS, once a day;
- at least ALARM_ERROR_CALLS_PER_HOUR calls with outcome `error` in the last
  hour, at most once a clock hour;
- events to n8n that failed in the last hour, at most once a clock hour
  (kitchen slips go to the print bridge, not to n8n, and are left out).
  n8n down shows up here first, so this push does not go through n8n;
- with VOICE_URL set, `voice` not answering, or Asterisk not registered: its
  healthcheck says so to `voice` while it is, and Asterisk takes no connection
  of its own (api/telephony/router.py). At most once a clock hour. A restart
  of `voice` in the moment of a pass reads as down;
- no `backup.done` from scripts/backup.sh for ALARM_BACKUP_HOURS, once a day.
  The row is missed rather than a failure reported, so a cron line that never
  ran counts too;
- from MONITOR_REPORT_HOUR local time, the previous business day in one line:
  calls by outcome, cost, orders, reservations. A day without a call is
  reported too: no call at all can mean the line is down.

The push goes straight to the push server of the stack, topic `ops`, which only
the operator reads (Maxi, 29.10.2026). The audit log remembers each push sent
(`monitor.alarm`), so a restart does not send it again; a push that fails is
not recorded and goes out on the next pass. Without MONITOR_PUSH_TOKEN the
push is only logged (development, tests). Counts and amounts only, no guest
details.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.logging import error_text, get_logger, log
from api.core.time import business_day, business_day_bounds_utc, to_local
from api.events.types import KITCHEN
from api.models import AuditLog, Call, Order, OutboxEvent, Reservation, Tenant

logger = get_logger("api.jobs.monitor")

INTERVAL_SECONDS = 300.0
TOPIC = "ops"
ACTOR = "monitor"
ACTION = "monitor.alarm"
# Written by scripts/backup.sh after a good run.
BACKUP_DONE = "backup.done"
# Its healthcheck runs every 10 s: this long without one is not a hiccup.
ASTERISK_SILENT_SECONDS = 120.0
PRIORITIES = {"normal": 3, "high": 4}
# Outcomes in the day report, in this order; an unknown one is left out.
OUTCOME_WORDS = {
    "completed": "completed",
    "transferred": "to the team",
    "callback": "callback",
    "abandoned": "abandoned",
    "error": "error",
}
ORDERS_TAKEN = ("confirmed", "approved", "handed_over")

Send = Callable[[str, str, str], None]
# (kind, key, title, text, priority): the key makes a push once per day or hour.
Alarm = tuple[str, str, str, str, str]


def push(title: str, text: str, priority: str) -> None:
    """POST to the push server; raises on a refused or failed request."""
    if not settings.monitor_push_token:
        log(logger, logging.WARNING, "Monitor push not sent, no token", title=title)
        return
    response = httpx.post(
        settings.monitor_push_url,
        json={
            "topic": TOPIC,
            "title": title,
            "message": text,
            "priority": PRIORITIES[priority],
        },
        headers={"Authorization": f"Bearer {settings.monitor_push_token}"},
        timeout=settings.n8n_timeout_seconds,
    )
    response.raise_for_status()


def asterisk_seen() -> float | None:
    """Seconds since Asterisk last said it is registered, None if it never did.
    Raises when `voice` does not answer."""
    response = httpx.get(
        f"{settings.voice_url}/asterisk/seen", timeout=settings.n8n_timeout_seconds
    )
    response.raise_for_status()
    return response.json()["seconds"]


def check(
    session: Session,
    now: datetime,
    send: Send = push,
    probe: Callable[[], float | None] = asterisk_seen,
) -> int:
    """One pass over every tenant. Commits; returns how many pushes went out.

    Each tenant runs in a savepoint of its own, as in api/jobs/daily_report.py:
    one that fails is logged, the others still get their pass.
    """
    sent = 0
    for tenant in session.scalars(select(Tenant).order_by(Tenant.id)).all():
        try:
            with session.begin_nested():
                for kind, key, title, text, priority in _due(
                    session, tenant, now, probe
                ):
                    if _sent(session, tenant, kind, key):
                        continue
                    try:
                        send(title, text, priority)
                    except Exception as exc:  # noqa: BLE001 - next pass tries again
                        log(
                            logger,
                            logging.ERROR,
                            "Monitor push failed",
                            kind=kind,
                            error=error_text(exc),
                        )
                        continue
                    session.add(
                        AuditLog(
                            tenant_id=tenant.id,
                            actor=ACTOR,
                            action=ACTION,
                            entity="monitor",
                            payload={"kind": kind, "key": key},
                        )
                    )
                    sent += 1
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            log(
                logger,
                logging.ERROR,
                "Monitor pass failed for a tenant",
                tenant_id=str(tenant.id),
                error=error_text(exc),
            )
    session.commit()
    return sent


def _due(
    session: Session,
    tenant: Tenant,
    now: datetime,
    probe: Callable[[], float | None],
) -> list[Alarm]:
    hour = now.astimezone(UTC).strftime("%Y-%m-%dT%H")
    since = now - timedelta(hours=1)
    due: list[Alarm] = []

    day = business_day(now, tenant.timezone)
    start, end = business_day_bounds_utc(day, tenant.timezone)
    cost, unknown = _cost(session, tenant, start, end)
    if cost > settings.alarm_cost_per_day_cents:
        due.append(
            (
                "cost",
                day.isoformat(),
                "Cost above the daily limit",
                f"Today {_cost_text(cost, unknown)}, limit "
                f"{_euro(settings.alarm_cost_per_day_cents)}.",
                "high",
            )
        )

    errors = session.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            Call.tenant_id == tenant.id,
            Call.outcome == "error",
            Call.ended_at > since,
        )
    )
    if (errors or 0) >= settings.alarm_error_calls_per_hour:
        due.append(
            (
                "errors",
                hour,
                "Calls with errors",
                f"{errors} calls ended with an error in the last hour.",
                "high",
            )
        )

    failed = session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant.id,
            OutboxEvent.status == "failed",
            # Kitchen slips go to the print bridge, not to n8n; the tablet
            # shows a failed one red (domain/ordering/handover.py).
            OutboxEvent.event_type.not_in(KITCHEN),
            # When it failed: the final failure stamps next_attempt_at
            # (events/outbox.py). Not updated_at: the deletion job rewrites
            # failed rows (allergy notes, expiry) without any delivery failing.
            OutboxEvent.next_attempt_at > since,
        )
    )
    if failed:
        due.append(
            (
                "outbox_failed",
                hour,
                "Events not delivered",
                f"{failed} events to n8n failed for good in the last hour. "
                "Is n8n running?",
                "high",
            )
        )

    if settings.voice_url:
        due.extend(_voice_layer(hour, probe))
    if _backup_overdue(session, tenant, now):
        due.append(
            (
                "backup",
                day.isoformat(),
                "Backup overdue",
                f"No good backup for {settings.alarm_backup_hours} hours. "
                "See backups/backup.log.",
                "high",
            )
        )

    if to_local(now, tenant.timezone).time() >= time(settings.monitor_report_hour):
        due.append(_day_report(session, tenant, day - timedelta(days=1)))
    return due


def _voice_layer(hour: str, probe: Callable[[], float | None]) -> list[Alarm]:
    try:
        seen = probe()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return [
            (
                "voice_down",
                hour,
                "Voice layer not answering",
                f"voice does not answer ({error_text(exc)}). Calls ring the team "
                "through the dialplan or the router.",
                "high",
            )
        ]
    if seen is None or seen > ASTERISK_SILENT_SECONDS:
        return [
            (
                "asterisk_down",
                hour,
                "Asterisk not registered",
                "Asterisk has not reported a registration for over "
                f"{int(ASTERISK_SILENT_SECONDS)} seconds. No call reaches the AI.",
                "high",
            )
        ]
    return []


def _backup_overdue(session: Session, tenant: Tenant, now: datetime) -> bool:
    if not settings.alarm_backup_hours:
        return False
    last = session.scalar(
        select(func.max(AuditLog.at)).where(
            AuditLog.tenant_id == tenant.id, AuditLog.action == BACKUP_DONE
        )
    )
    return last is None or last < now - timedelta(hours=settings.alarm_backup_hours)


def _day_report(session: Session, tenant: Tenant, day: date) -> Alarm:
    start, end = business_day_bounds_utc(day, tenant.timezone)
    outcomes = dict(
        session.execute(
            select(Call.outcome, func.count())
            .where(
                Call.tenant_id == tenant.id,
                Call.started_at >= start,
                Call.started_at < end,
            )
            .group_by(Call.outcome)
        ).all()
    )
    calls = sum(outcomes.values())
    orders = session.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.tenant_id == tenant.id,
            Order.status.in_(ORDERS_TAKEN),
            Order.created_at >= start,
            Order.created_at < end,
        )
    )
    reservations = session.scalar(
        select(func.count())
        .select_from(Reservation)
        .where(
            Reservation.tenant_id == tenant.id,
            Reservation.status == "confirmed",
            Reservation.created_at >= start,
            Reservation.created_at < end,
        )
    )
    parts = [
        f"{outcomes[o]} {word}" for o, word in OUTCOME_WORDS.items() if o in outcomes
    ]
    text = f"{calls} calls" + (f" ({', '.join(parts)})" if parts else "")
    text += (
        f", cost {_cost_text(*_cost(session, tenant, start, end))}, "
        f"{orders} orders, {reservations} reservations."
    )
    return (
        "day_report",
        day.isoformat(),
        f"Day report {day.isoformat()}",
        text,
        "normal",
    )


def _cost(
    session: Session, tenant: Tenant, start: datetime, end: datetime
) -> tuple[int, int]:
    """The known cost of the calls in the window, and how many calls have none:
    a price not set in .env or a transfer not measured leaves `cost_cents` empty,
    which is not a cost of zero."""
    known, unknown = session.execute(
        select(
            func.coalesce(func.sum(Call.cost_cents), 0),
            func.count().filter(Call.cost_cents.is_(None)),
        ).where(
            Call.tenant_id == tenant.id,
            Call.started_at >= start,
            Call.started_at < end,
        )
    ).one()
    return known or 0, unknown


def _cost_text(known: int, unknown: int) -> str:
    if not unknown:
        return _euro(known)
    return f"{_euro(known)} plus {unknown} calls of unknown cost"


def _sent(session: Session, tenant: Tenant, kind: str, key: str) -> bool:
    return (
        session.scalar(
            select(AuditLog.id)
            .where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == ACTION,
                AuditLog.payload["kind"].astext == kind,
                AuditLog.payload["key"].astext == key,
            )
            .limit(1)
        )
        is not None
    )


def _euro(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d} EUR"
