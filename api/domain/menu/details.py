"""get_item_details: Optionen, Beschreibung und Allergene zu einem Gericht (T-4.4, docs/04).

Die Allergen-Regel ist die harte Stelle: **unbekannt ist nicht "keine"**. Ohne
gepflegte Zeile in `item_allergens` gibt es keine Auskunft, sondern den Satz
aus dem Code und den Rueckruf durch das Team (docs/05 §3 Allergie, CLAUDE.md §9).
Der Agent formuliert hier nichts selbst.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.menu.items import is_sold_out, option_groups
from api.models import ItemAllergen, MenuItem
from api.models.menu import ALLERGEN_CODES
from api.schemas.menu import Allergens, ItemDetails

SAY_ALLERGENS_UNKNOWN = "Das lasse ich Ihnen vom Team bestätigen."


def get_item_details(
    session: Session,
    tenant_id: uuid.UUID,
    menu_item_id: uuid.UUID,
    now: datetime | None = None,
) -> ItemDetails:
    now = now or utcnow()
    item = session.scalar(
        select(MenuItem).where(
            MenuItem.tenant_id == tenant_id,
            MenuItem.id == menu_item_id,
            MenuItem.active.is_(True),
        )
    )
    if item is None:
        raise NotFound("Gericht unbekannt")

    allergens = _allergens(session, item.id)
    return ItemDetails(
        menu_item_id=item.id,
        number=item.number,
        name=item.name,
        price_cents=item.price_cents,
        sold_out=is_sold_out(item, now),
        description=item.description,
        allergens=allergens,
        option_groups=option_groups(session, [item.id]).get(item.id, []),
        say=None if allergens.known else SAY_ALLERGENS_UNKNOWN,
    )


def _allergens(session: Session, menu_item_id: uuid.UUID) -> Allergens:
    rows = session.scalars(
        select(ItemAllergen).where(ItemAllergen.menu_item_id == menu_item_id)
    ).all()
    if not rows:
        return Allergens(known=False)
    # Reihenfolge der LMIV-Liste, nicht die des Imports: vorgelesen wird immer gleich.
    codes = sorted(
        (row.allergen_code for row in rows), key=lambda code: ALLERGEN_CODES.index(code)
    )
    return Allergens(
        known=True,
        codes=codes,
        confirmed_at=max(row.confirmed_at for row in rows).date(),
    )
