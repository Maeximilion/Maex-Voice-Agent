"""One eval run (docs/08 §3, migration 048). Synthetic cases only, no tenant."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, UUIDPrimaryKey


class EvalRun(UUIDPrimaryKey, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("passed >= 0 AND failed >= 0", name="ck_eval_runs_counts"),
        CheckConstraint(
            "accuracy >= 0 AND accuracy <= 1", name="ck_eval_runs_accuracy"
        ),
        CheckConstraint(
            "verdict IN ('bestanden', 'durchgefallen')", name="ck_eval_runs_verdict"
        ),
    )

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    suite: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, nullable=False)
    accuracy: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text)
