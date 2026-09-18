"""domain/menu/details: Allergen-Regel "unbekannt ist nicht keine" (T-4.4, docs/04)."""

import uuid
from datetime import date

import pytest

from api.core.errors import NotFound
from api.domain.menu import get_item_details
from api.domain.menu.details import SAY_ALLERGENS_UNKNOWN
from api.tests.test_domain_menu_search import NOW, menu  # noqa: F401


def details(menu, number, **kw):  # noqa: F811
    session, tenant_id, items = menu
    return get_item_details(session, tenant_id, items[number].id, now=NOW, **kw)


def test_gepflegte_allergene_kommen_in_der_reihenfolge_der_lmiv(menu):  # noqa: F811
    result = details(menu, "23")
    assert result.allergens.known is True
    assert result.allergens.codes == ["A", "F"]
    assert result.allergens.confirmed_at == date(2026, 8, 1)
    # Bei gepflegten Werten sagt das Team nichts nach, der Agent liest vor.
    assert result.say is None


def test_ohne_gepflegten_wert_gibt_es_keine_auskunft(menu):  # noqa: F811
    result = details(menu, "40")
    assert result.allergens.known is False
    assert result.allergens.codes == []
    assert result.allergens.confirmed_at is None
    assert result.say == SAY_ALLERGENS_UNKNOWN


def test_beschreibung_und_optionen_kommen_mit(menu):  # noqa: F811
    result = details(menu, "23")
    assert result.description == "mit Gemüsefüllung, dazu süßsaure Sauce"
    assert result.number == "23" and result.price_cents == 690
    assert [g.group for g in result.option_groups] == ["Sauce"]
    assert len(result.option_groups[0].options) == 2


def test_ausverkauft_steht_auch_in_den_details(menu):  # noqa: F811
    assert details(menu, "12").sold_out is True
    assert details(menu, "23").sold_out is False


def test_unbekannte_id_ist_not_found(menu):  # noqa: F811
    session, tenant_id, _ = menu
    with pytest.raises(NotFound):
        get_item_details(session, tenant_id, uuid.uuid4(), now=NOW)


def test_fremder_mandant_bekommt_das_gericht_nicht(menu):  # noqa: F811
    session, _, items = menu
    with pytest.raises(NotFound):
        get_item_details(session, uuid.uuid4(), items["23"].id, now=NOW)


def test_inaktives_gericht_liefert_keine_details(menu):  # noqa: F811
    session, tenant_id, items = menu
    with pytest.raises(NotFound):
        get_item_details(session, tenant_id, items["99"].id, now=NOW)
