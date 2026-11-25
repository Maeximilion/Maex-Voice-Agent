"""Reservierungsbeginn relativ zur Oeffnung (T-1.14, D12).

Frühester Beginn `reservation_lead_minutes` nach Öffnung, spätester
`reservation_last_start_minutes` vor Schluss, je Service-Fenster. Gilt nur für
Reservierungen, nicht für Abholung oder Lieferung (Annahme D12). Defaults sind
die Werte von Maxi (27.09.2026): Mittag 11-14 buchbar 11:30-13:30 im 30-min-Raster.

Revision: 005
Vorgänger: 004
Erstellt: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (("reservation_lead_minutes", "15"), ("reservation_last_start_minutes", "30"))


def upgrade() -> None:
    for name, default in COLUMNS:
        op.add_column(
            "service_config",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.create_check_constraint(
            f"ck_service_config_{name}", "service_config", f"{name} >= 0"
        )


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_constraint(f"ck_service_config_{name}", "service_config")
        op.drop_column("service_config", name)
