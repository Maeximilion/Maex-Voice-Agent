"""Begruendung fuer den Aufpreis einer Option (T-4.10, Entscheidung D8).

Fragt der Gast, warum "Nudeln statt Reis" mehr kostet, nennt der Agent nur, was
hier steht - nie eine eigene Erklaerung (CLAUDE.md §2 Regel 1). Leer heisst: der
Preis steht so in der Karte, mehr sagt der Agent nicht. Kommt aus der optionalen
Spalte `price_reason` in `item_options.csv` (docs/14).

Revision: 003
Vorgänger: 002
Erstellt: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("item_options", sa.Column("price_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("item_options", "price_reason")
