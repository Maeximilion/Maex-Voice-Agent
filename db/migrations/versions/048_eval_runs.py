"""The table `eval_runs`: one row per eval run (docs/08 §3, T-5.1).

`python -m evals.runner` (`make eval`, `make eval-targets`) writes the run
into the stack's database after its report: when it ran, model, prompt
version, case folder, tags, passed and failed cases, accuracy, verdict, the
whole report as JSON and the commit. The cases are synthetic (docs/09 L14),
so a row holds no guest data and belongs to no tenant.

A new table only; the upgrade changes no row. The downgrade drops it with
every run stored so far; the JSON reports in `evals/reports/` stay. On a
database a restaurant signs in to, take a backup first with
`bash scripts/backup.sh --label before_migration_048` (CLAUDE.md §8,
docs/13 §4).

Revision: 048
Revises: 047
Created: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("at", TZ, nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.Numeric(5, 4), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("git_sha", sa.Text()),
        sa.Column("created_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TZ, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("passed >= 0 AND failed >= 0", name="ck_eval_runs_counts"),
        sa.CheckConstraint(
            "accuracy >= 0 AND accuracy <= 1", name="ck_eval_runs_accuracy"
        ),
        sa.CheckConstraint(
            "verdict IN ('bestanden', 'durchgefallen')", name="ck_eval_runs_verdict"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("eval_runs")
