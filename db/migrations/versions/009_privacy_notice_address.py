"""Where the privacy notice is, said in the greeting of every call (T-11.5).

The legal check of 20.10.2026 asks for two layers of information (docs/09 §1
L4): in the call the controller, the AI notice and where the full privacy
notice is; on the website everything else. The address is the restaurant's,
so it is a field of `service_config` and no text in the code (CLAUDE.md §2
rule 1): `privacy_notice_address`, written the way the caller should hear it
("example.com/datenschutz" is only the repository's placeholder).

Empty (NULL) on every existing row: no address is invented. Without one the
greeting leaves the sentence out (`telephony/handler.py`), and the duty of L4
stays open until the restaurant's notice is online and its address is set
here. A blank text is refused, it would be read out as "unter .".

On a database that holds a restaurant's data: backup first, with
`bash scripts/backup.sh --label before_migration_009` (CLAUDE.md §8, docs/13
§4). The upgrade only adds the empty column; the downgrade drops it, and with
it the address set since. That backup is the way back to that value.

Revision: 009
Revises: 008
Created: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "privacy_notice_address"
CHECK = f"ck_service_config_{COLUMN}"


def upgrade() -> None:
    op.add_column("service_config", sa.Column(COLUMN, sa.Text(), nullable=True))
    op.create_check_constraint(CHECK, "service_config", f"btrim({COLUMN}) <> ''")


def downgrade() -> None:
    op.drop_constraint(CHECK, "service_config")
    op.drop_column("service_config", COLUMN)
