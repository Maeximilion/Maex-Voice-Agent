"""An index on the audit log by tenant, action and id (review of #837).

The version of the operator's page (`api/domain/operator.py` `_version`)
counts and takes the newest of a few actions in the audit log, on every
access read and inside the step's lock. With only `ix_audit_log_tenant_at`
that read walks the tenant's whole history; this index answers it from the
few rows of those actions. The upgrade and the downgrade change no row.

Revision: 043
Revises: 042
Created: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_audit_log_tenant_action"


def upgrade() -> None:
    op.create_index(INDEX, "audit_log", ["tenant_id", "action", "id"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="audit_log")
