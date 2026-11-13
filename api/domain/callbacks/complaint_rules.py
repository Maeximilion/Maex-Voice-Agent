"""Which fixed rule answers a complaint (D19, docs/05 §3, T-10.11).

Words only, no understanding: the phrases of a row against the words the guest
said. What the core does with the rule stays in `agent/loop.py`. Strict on
purpose: a rule that is missed sends the guest to the team, as before; a rule
wrongly heard offers money for a complaint nobody made.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import AuditLog, ComplaintRule

ACTION_REMEDY_BOOKED = "complaint.remedy_booked"
_WORD = re.compile(r"\w+")
# A sentence that rules something out or only supposes it does not say what the
# phrase says ("nicht kalt", "wenn es kalt ist"). Read outside the phrase, so a
# row can hold "nicht warm". Any such word silences the sentence, the safe side.
_DOUBT = re.compile(
    r" (?:nichts?|nie|niemals|nein|kein\w*|ohne|wenn|falls|ob|vielleicht) "
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


def note_remedy(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    rule_id: uuid.UUID,
    remedy_cents: int,
) -> None:
    """The guest's yes to a rule, in `audit_log`. Not committed: it goes with
    the card that books the remedy, or with its failure."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor="agent",
            action=ACTION_REMEDY_BOOKED,
            entity="complaint_rule",
            entity_id=rule_id,
            payload={"call_id": str(call_id), "remedy_cents": remedy_cents},
        )
    )
