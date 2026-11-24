"""The callback reason `order_open`: an order read back and never answered (#977).

A call that ends while an order waits for the guest's yes leaves a card for
the team (#953). It was written with the reason `not_understood` and read
"Nicht verstanden" on the tablet, though the agent understood the order and
only the yes was missing (Maxi, 24.11.2026). `order_open` reads "Bestellung
offen". Only the conversation core writes it, never the model.

The upgrade widens `ck_callbacks_reason` and moves the cards written so far
to the new reason: they all carry the fixed sentence of that card
(`SUMMARY_READBACK_UNANSWERED` in `api/agent/loop.py`), behind the kitchen
marker where the guest spoke of an allergy. The downgrade gives them back as
`not_understood` and narrows the check again. No column changes, no row is
lost either way; on a database a restaurant signs in to, take a backup first
with `bash scripts/backup.sh --label before_migration_045` (CLAUDE.md §8,
docs/13 §4).

Revision: 045
Revises: 044
Created: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_callbacks_reason"
BEFORE = ("complaint", "not_understood", "human_requested", "out_of_scope")
AFTER = (*BEFORE, "order_open")
# The card's fixed sentence as of this revision, copied: a migration does not
# follow later edits of the code.
UNANSWERED = "Anruf endete vor dem Ja zur vorgelesenen Bestellung."


def _check(values: tuple[str, ...]) -> None:
    op.drop_constraint(CHECK, "callbacks", type_="check")
    quoted = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(CHECK, "callbacks", f"reason IN ({quoted})")


def upgrade() -> None:
    _check(AFTER)
    op.get_bind().execute(
        sa.text(
            "UPDATE callbacks SET reason = 'order_open'"
            " WHERE reason = 'not_understood' AND strpos(summary, :said) > 0"
        ),
        {"said": UNANSWERED},
    )


def downgrade() -> None:
    op.execute(
        "UPDATE callbacks SET reason = 'not_understood' WHERE reason = 'order_open'"
    )
    _check(BEFORE)
