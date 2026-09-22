"""get_item_details: Optionen, Beschreibung und Allergene zu einem Gericht (T-4.4, docs/04).

Die Allergen-Regel ist die harte Stelle: **unbekannt ist nicht "keine"**. Ohne
gepflegte Zeile in `item_allergens` gibt es keine Auskunft, sondern den Satz aus
dem Code und den Rueckruf durch das Team (docs/05 §3 Allergie, CLAUDE.md §9).
Der Agent formuliert hier nichts selbst. Der Satz haengt an `allergen_question`:
dasselbe Tool beantwortet auch Fragen nach Optionen, und die duerfen nicht mit
einem Rueckruf enden.

Das Gericht kommt ueber die `menu_item_id`, die `search_menu` geliefert hat -
ein anderer Weg in die Karte existiert fuer den Agenten nicht (CLAUDE.md §2
Regel 2). Inaktive Gerichte liefert auch dieses Tool nicht, ausverkaufte schon,
mit demselben Satz wie die Suche.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.errors import NotFound
from api.core.time import tz, utcnow
from api.domain.menu.items import is_sold_out, option_groups
from api.domain.menu.search import SAY_SOLD_OUT
from api.models import ItemAllergen, MenuItem, Tenant
from api.models.menu import ALLERGEN_CODES
from api.schemas.menu import Allergens, ItemDetails

SAY_ALLERGENS_UNKNOWN = (
    "Das kann ich Ihnen nicht sicher sagen. Das Team ruft Sie dazu zurück."
)


def get_item_details(
    session: Session,
    tenant_id: uuid.UUID,
    menu_item_id: uuid.UUID,
    allergen_question: bool,
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

    allergens = _allergens(session, tenant_id, item.id)
    sold_out = is_sold_out(item, now)
    return ItemDetails(
        menu_item_id=item.id,
        number=item.number,
        name=item.name,
        price_cents=item.price_cents,
        sold_out=sold_out,
        description=item.description,
        allergens=allergens,
        option_groups=option_groups(session, [item.id]).get(item.id, []),
        say=_say(allergens, item, sold_out, allergen_question),
    )


def _say(
    allergens: Allergens, item: MenuItem, sold_out: bool, allergen_question: bool
) -> str | None:
    """Die fehlende Allergen-Auskunft wiegt schwerer als "heute aus".

    Aber nur, wenn ueberhaupt nach Allergenen gefragt wurde: dasselbe Tool
    beantwortet auch Fragen nach Optionen und Beschreibung (docs/04). Ohne diese
    Unterscheidung bekaeme "Welche Saucen gibt es zur Wan-Tan-Suppe?" den Satz
    zum Rueckruf statt einer Antwort - und das Team einen Rueckruf, den niemand
    wollte. Die Absicht kann nur der Anrufer kennen, deshalb steht sie im
    Request und nicht hier.
    """
    if allergen_question and not allergens.known:
        return SAY_ALLERGENS_UNKNOWN
    if sold_out:
        return SAY_SOLD_OUT.format(name=item.name)
    return None


def _allergens(
    session: Session, tenant_id: uuid.UUID, menu_item_id: uuid.UUID
) -> Allergens:
    rows = session.scalars(
        select(ItemAllergen).where(ItemAllergen.menu_item_id == menu_item_id)
    ).all()
    if not rows:
        return Allergens(known=False)
    # Reihenfolge der LMIV-Liste, nicht die des Imports: vorgelesen wird immer gleich.
    codes = sorted(
        (row.allergen_code for row in rows), key=lambda code: ALLERGEN_CODES.index(code)
    )
    # Ortsdatum, nicht UTC-Datum (CLAUDE.md §8): ein Nachweis vom 19.09. um
    # 00:30 Ortszeit steht als 18.09. 22:30 UTC in der Zeile und waere sonst
    # einen Tag zu alt gemeldet.
    tenant = session.get(Tenant, tenant_id)
    zone = tz(tenant.timezone if tenant else None)
    return Allergens(
        known=True,
        codes=codes,
        confirmed_at=max(row.confirmed_at for row in rows).astimezone(zone).date(),
    )
