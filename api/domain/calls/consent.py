"""The consent log (legal check L13): one row per answer to a consent question.

The conversation core asks, judges the answer and decides what the answer
allows (`api/agent/loop.py`); this records it, so that a stored allergy note
can be traced to the "ja" it rests on.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.models import ConsentLog

Answer = Literal["yes", "no", "unclear"]


def record_consent(
    session: Session,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID,
    question: str,
    answer: Answer,
    now: datetime | None = None,
) -> None:
    """Writes the answer and commits: the proof stands before anything is
    stored on the strength of it."""
    session.add(
        ConsentLog(
            tenant_id=tenant_id,
            call_id=call_id,
            question=question,
            answer=answer,
            answered_at=now or utcnow(),
        )
    )
    session.commit()
