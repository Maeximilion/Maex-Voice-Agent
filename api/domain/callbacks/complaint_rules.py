"""Which fixed rule answers a complaint (D19, docs/05 §3, T-10.11).

Words only, no understanding: the phrases of a row against the words the guest
said. What the core does with the rule stays in `agent/loop.py`. Strict on
purpose: a rule that is missed sends the guest to the team, as before; a rule
wrongly heard offers money for a complaint nobody made.
"""

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import AuditLog, ComplaintRule, Order

ACTION_REMEDY_BOOKED = "complaint.remedy_booked"
# A rule books money for whoever says its words: it is offered only for an
# order the calling line placed this long before the call, and once per
# order (#888 S8, a default awaiting Maxi's decision). Longer is kinder to a guest
# who calls the next evening, shorter gives a stranger less to name.
REMEDY_ORDER_WINDOW = timedelta(hours=24)
# What the guest's yes made of an order and nobody took back (`create._ORDER_STANDS`).
_ORDER_STANDS = ("confirmed", "approved", "handed_over")
_WORD = re.compile(r"\w+")
# A sentence that rules something out or only supposes it does not say what the
# phrase says ("nicht kalt", "wenn es kalt ist"). Read outside the phrase, so a
# row can hold "nicht warm". Any such word silences the sentence, the safe side.
# "weder ... noch", "kaum" and "alles andere als" too (#657).
_DOUBT = re.compile(
    r" (?:nichts?|nie|niemals|nein|kein\w*|ohne|wenn|falls|ob|vielleicht"
    r"|weder|kaum|alles andere als) "
)


def matching_rule(
    session: Session, tenant_id: uuid.UUID, said: str
) -> ComplaintRule | None:
    """The one active rule of the tenant that matches `said`. None when there
    is none, when two match, or when the sentence leaves a doubt."""
    words = _spoken(said)
    rules = session.scalars(
        select(ComplaintRule).where(
            ComplaintRule.tenant_id == tenant_id, ComplaintRule.active.is_(True)
        )
    ).all()
    # A NULL among the phrases of a row from before migration 037 says nothing
    # (#692): skipped, or every complaint would end as an outage.
    phrases = {rule.id: [p for p in rule.phrases if p is not None] for rule in rules}
    hits = [r for r in rules if any(_spoken(p) in words for p in phrases[r.id])]
    if len(hits) != 1:
        return None
    for phrase in phrases[hits[0].id]:
        words = words.replace(_spoken(phrase), " ")
    return None if _DOUBT.search(words) else hits[0]


def _spoken(text: str) -> str:
    """The words of `text`, lowercased, one space around each: a phrase matches
    whole words only, and one without a word matches nothing."""
    return f" {' '.join(_WORD.findall(text.lower()))} "


def remedy_order(
    session: Session, tenant_id: uuid.UUID, phone: str | None, now: datetime
) -> Order | None:
    """The order a remedy can be for: the newest order that stands, placed
    within `REMEDY_ORDER_WINDOW` before `now` from the line `phone` (the
    caller ID) and booked under it (#888 S8). A number said in either call
    can be anyone's, so an order booked under one counts for nothing. The
    complaint is about that newest order: once it has a remedy, None, and no
    older order of the line stands in for it. None sends the complaint to
    the team.
    shortcut: two calls of one line at the same moment can both be offered a
    remedy for the same order; the team sees both cards. A lock on the order
    row in the booking transaction closes it, once a catalogue is live."""
    if not phone:
        return None
    order = session.scalars(
        select(Order)
        .where(
            Order.tenant_id == tenant_id,
            Order.phone == phone,
            Order.booked_on_own_line.is_(True),
            Order.status.in_(_ORDER_STANDS),
            Order.deleted_at.is_(None),
            Order.created_at >= now - REMEDY_ORDER_WINDOW,
            Order.created_at <= now,
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    ).first()
    if order is None:
        return None
    remedied = session.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == ACTION_REMEDY_BOOKED,
            AuditLog.payload["order_id"].astext == str(order.id),
        )
        .limit(1)
    )
    return None if remedied is not None else order


def note_remedy(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    rule_id: uuid.UUID,
    remedy_cents: int,
    order_id: uuid.UUID,
) -> None:
    """The guest's yes to a rule for `order_id`, in `audit_log`: the next
    complaint about that order finds it (`remedy_order`). Not committed: it
    goes with the card that books the remedy, or with its failure."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor="agent",
            action=ACTION_REMEDY_BOOKED,
            entity="complaint_rule",
            entity_id=rule_id,
            payload={
                "call_id": str(call_id),
                "remedy_cents": remedy_cents,
                "order_id": str(order_id),
            },
        )
    )
