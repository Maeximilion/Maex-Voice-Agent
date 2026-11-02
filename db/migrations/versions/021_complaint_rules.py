"""The catalogue of complaint rules, one table per tenant (T-10.11, D19).

A rule holds the words that select it, the remedy in cents and the two sentences
the guest hears. The conversation core matches a complaint against the tenant's
active rules; the model never reads them. The table starts empty and nothing
seeds it: the catalogue is Maxi's, and with an empty one every complaint goes to
the team as before. A new row is off until someone switches it on.

No personal data, nothing backfilled. The downgrade drops the table with every
rule; the remedies booked stay on their callback cards and in `audit_log`.

Number 021 with its parent 019: 020 is taken by the open T-4.9 PR (#476). The
later of the two PRs sets its parent to the other one.

Revision: 021
Revises: 019
Created: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "complaint_rules",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phrases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("remedy_cents", sa.Integer(), nullable=False),
        sa.Column("offer_say", sa.Text(), nullable=False),
        sa.Column("granted_say", sa.Text(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cardinality(phrases) > 0", name="ck_complaint_rules_phrases"
        ),
        sa.CheckConstraint("remedy_cents > 0", name="ck_complaint_rules_remedy_cents"),
        sa.CheckConstraint(
            "btrim(name) <> '' AND btrim(offer_say) <> '' AND btrim(granted_say) <> ''",
            name="ck_complaint_rules_texts",
        ),
        sa.CheckConstraint("char_length(name) <= 100", name="ck_complaint_rules_name"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_complaint_rules_tenant_id", "complaint_rules", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("complaint_rules")
