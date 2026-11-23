"""Display and form reading of the tab "Lieferung" (#571, docs/06 §4): German
words, and the typed fields handed to `domain/delivery/owner_zones.py` as
text.

Every amount shown comes from the database through that module, in cents; this
file writes them as euros and names the fields. It reads no amount and checks
nothing: what a field may hold is decided in the domain.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from api.domain.delivery import owner_zones
from api.domain.menu.importer import format_eur
from api.gui import form_view
from api.gui.orders_view import euro
from api.models import DeliveryZone, Tenant

# What a refused form echoes back at most of a field the owner typed: more
# than the domain takes, so a typing error is still there to correct. The
# postal codes get room for the most a zone takes with any separators.
ECHO = {"name": 60, "plz": 8000, "pauschale": 12, "mindestwert": 12, "lieferzeit": 12}
# The length of `owner_zones.token`.
TOKEN = 16
# A field of `owner_zones.Typed`, as the domain names a fault, by its input.
FIELDS = {
    "name": "name",
    "postal_codes": "plz",
    "fee": "pauschale",
    "min_order": "mindestwert",
    "eta": "lieferzeit",
}
# Each input by its id and its words in the list of faults.
NAMES: form_view.Fields = {
    "name": ("f-name", "Name der Zone"),
    "plz": ("f-plz", "Postleitzahlen"),
    "pauschale": ("f-pauschale", "Lieferpauschale"),
    "mindestwert": ("f-mindestwert", "Mindestbestellwert"),
    "lieferzeit": ("f-lieferzeit", "Lieferzeit"),
}


def typed(form: dict[str, str]) -> owner_zones.Typed:
    """The fields of a zone's form, as text."""
    return owner_zones.Typed(
        name=form.get("name", ""),
        postal_codes=form.get("plz", ""),
        fee=form.get("pauschale", ""),
        min_order=form.get("mindestwert", ""),
        eta=form.get("lieferzeit", ""),
        active=bool(form.get("aktiv")),
    )


def zone_rows(session: Session, tenant: Tenant) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "name": row.name,
            "codes": ", ".join(row.postal_codes or []),
            "fee": euro(row.fee_cents),
            "min_order": euro(row.min_order_cents),
            "eta": f"{row.eta_minutes} Min",
            "active": row.active,
        }
        for row in owner_zones.zones(session, tenant.id)
    ]


def _shown(row: DeliveryZone) -> dict[str, Any]:
    """A zone as its form shows it: what an untouched save sends back."""
    return {
        "name": row.name,
        "plz": ", ".join(row.postal_codes or []),
        "pauschale": format_eur(row.fee_cents),
        "mindestwert": format_eur(row.min_order_cents),
        "lieferzeit": str(row.eta_minutes),
        "aktiv": row.active,
        "seen": owner_zones.token(row),
    }


def field_errors(faults: dict[str, str]) -> dict[str, str]:
    """The domain's faults by the inputs of the form."""
    return {FIELDS.get(key, key): say for key, say in faults.items()}


def zone_form(
    row: DeliveryZone | None,
    sent: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The form of one zone, or of a new one without `row`: what the database
    holds with the token of that state; or what was typed and refused
    (`sent`), with the token it was typed against. A refused form never gets a
    fresh token: its fields come from the old state, and with a new token the
    corrected save would write over what another device changed meanwhile."""
    if sent is not None:
        shown = _shown(row) if row is not None else {}
        # A field sent back as the form showed it comes back whole: a name or
        # a list of codes the import wrote longer than the echo would come
        # back cut, and the corrected save would change it.
        fields: dict[str, Any] = {}
        for key, most in ECHO.items():
            value = sent.get(key, "")
            fields[key] = value if value == shown.get(key) else value[:most]
        fields["aktiv"] = bool(sent.get("aktiv"))
        fields["seen"] = sent.get("gesehen", "")[:TOKEN]
    elif row is None:
        fields = dict.fromkeys(ECHO, "")
        fields["aktiv"] = True
        fields["seen"] = ""
    else:
        fields = _shown(row)
    errors = errors or {}
    return {
        **fields,
        "errors": errors,
        "summary": form_view.summary(errors, NAMES),
        "title": "Neue Lieferzone" if row is None else f"Lieferzone „{row.name}“",
        "action": _action(row.id if row is not None else None),
        "wait_max": owner_zones.MAX_ETA_MINUTES,
    }


def _action(zone_id: uuid.UUID | None) -> str:
    return "/gui/lieferung/neu" if zone_id is None else f"/gui/lieferung/zone/{zone_id}"


def page(session: Session, tenant: Tenant) -> dict[str, Any]:
    rows = zone_rows(session, tenant)
    return {"zones": rows, "delivers": any(row["active"] for row in rows)}
