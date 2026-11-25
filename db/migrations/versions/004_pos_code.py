"""Artikelnummer der Kasse in Originalschreibweise (T-4.11).

`menu_items.number` wird für die Suche klein gespeichert ("35b"), die Kasse
braucht ihre Schreibweise ("35B"). Eingabezettel und Kassenübergabe drucken
`pos_code`, nie `number` (docs/14 §Quelle Kasse). Leer bei Gerichten, die nicht
aus der Kasse kommen.

Revision: 004
Vorgänger: 003
Erstellt: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("menu_items", sa.Column("pos_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("menu_items", "pos_code")
