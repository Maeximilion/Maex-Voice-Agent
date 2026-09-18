"""domain/menu/search: Auflösungsreihenfolge, Schwellen, nie raten (T-4.3, docs/04)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.core.errors import InvalidInput, NotFound
from api.domain.menu import search_menu
from api.models import ItemAlias, ItemAllergen, ItemOption, MenuItem
from scripts.seed import seed

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


def build_menu(session: Session, tenant_id: uuid.UUID) -> dict[str, MenuItem]:
    """Eine kleine Karte mit genau den Fällen, an denen die Suche scheitern kann."""
    items = {
        "23": MenuItem(
            tenant_id=tenant_id,
            number="23",
            name="Frühlingsrollen (4 Stück)",
            category="Vorspeisen",
            price_cents=690,
            description="mit Gemüsefüllung, dazu süßsaure Sauce",
        ),
        "24": MenuItem(
            tenant_id=tenant_id,
            number="24",
            name="Frühlingsrollen mit Ente",
            category="Vorspeisen",
            price_cents=890,
        ),
        "40": MenuItem(
            tenant_id=tenant_id,
            number="40",
            name="Gebratene Nudeln mit Hühnerfleisch",
            category="Hauptgerichte",
            price_cents=950,
        ),
        "41": MenuItem(
            tenant_id=tenant_id,
            number="41",
            name="Gebratene Nudeln mit Rindfleisch",
            category="Hauptgerichte",
            price_cents=990,
        ),
        "12": MenuItem(
            tenant_id=tenant_id,
            number="12",
            name="Wan-Tan-Suppe",
            category="Suppen",
            price_cents=450,
            sold_out_until=NOW + timedelta(hours=6),
        ),
        "23a": MenuItem(
            tenant_id=tenant_id,
            number="23a",
            name="Frühlingsrollen mit Garnelen",
            category="Vorspeisen",
            price_cents=790,
        ),
        "01": MenuItem(
            tenant_id=tenant_id,
            number="01",
            name="Gemischter Salat",
            category="Salate",
            price_cents=490,
        ),
        "99": MenuItem(
            tenant_id=tenant_id,
            number="99",
            name="Altes Gericht",
            category="Sonstiges",
            price_cents=100,
            active=False,
        ),
    }
    session.add_all(items.values())
    session.flush()

    session.add_all(
        [
            # Ein Alias, der nur an einem Gericht hängt.
            ItemAlias(
                menu_item_id=items["23"].id, alias="glücksrollen", source="manual"
            ),
            # Derselbe Alias an zwei Gerichten: hier wird gefragt, nicht gewählt.
            ItemAlias(menu_item_id=items["40"].id, alias="bami", source="import"),
            ItemAlias(menu_item_id=items["41"].id, alias="bami", source="import"),
            ItemOption(
                menu_item_id=items["23"].id,
                group_name="Sauce",
                option_name="süßsauer",
                price_delta_cents=0,
                is_default=True,
                required=False,
            ),
            ItemOption(
                menu_item_id=items["23"].id,
                group_name="Sauce",
                option_name="Erdnuss",
                price_delta_cents=50,
                is_default=False,
                required=True,
            ),
            ItemAllergen(
                menu_item_id=items["23"].id,
                allergen_code="F",
                confirmed_by="Küche",
                confirmed_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            ),
            ItemAllergen(
                menu_item_id=items["23"].id,
                allergen_code="A",
                confirmed_by="Küche",
                confirmed_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            ),
        ]
    )
    session.commit()
    return items


@pytest.fixture
def menu(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as session:
        tenant_id = uuid.UUID(
            seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
        )
        items = build_menu(session, tenant_id)
        yield session, tenant_id, items
    engine.dispose()


def search(menu, query, **kw):
    session, tenant_id, _ = menu
    return search_menu(session, tenant_id, query, now=NOW, **kw)


def test_zahl_im_satz_trifft_die_kartennummer(menu):
    _, _, items = menu
    result = search(menu, "einmal die Nummer dreiundzwanzig bitte")
    assert result.match_type == "exact_number"
    assert [hit.menu_item_id for hit in result.results] == [items["23"].id]
    assert result.results[0].price_cents == 690


def test_menge_wird_nicht_zur_kartennummer(menu):
    _, _, items = menu
    result = search(menu, "zweimal die vierzig")
    assert result.match_type == "exact_number"
    assert result.results[0].menu_item_id == items["40"].id


def test_optionsgruppen_kommen_mit(menu):
    result = search(menu, "Nummer 23")
    groups = result.results[0].option_groups
    assert [g.group for g in groups] == ["Sauce"]
    # Pflicht auf einer Option macht die ganze Gruppe zur Pflichtfrage.
    assert groups[0].required is True
    assert [(o.name, o.price_delta_cents, o.default) for o in groups[0].options] == [
        ("süßsauer", 0, True),
        ("Erdnuss", 50, False),
    ]


def test_alias_exakt_trifft_genau_ein_gericht(menu):
    _, _, items = menu
    result = search(menu, "Die Glücksrollen!")
    assert result.match_type == "alias"
    assert result.results[0].menu_item_id == items["23"].id


def test_alias_an_zwei_gerichten_fragt_nach(menu):
    result = search(menu, "einmal Bami bitte")
    assert result.match_type == "ambiguous"
    assert {hit.number for hit in result.results} == {"40", "41"}


def test_unscharfer_einzeltreffer(menu):
    _, _, items = menu
    result = search(menu, "Wan Tan Suppe")
    assert result.match_type == "fuzzy_single"
    assert result.results[0].menu_item_id == items["12"].id


def test_mehrere_aehnliche_namen_sind_ambiguous(menu):
    result = search(menu, "Frühlingsrollen")
    assert result.match_type == "ambiguous"
    assert 2 <= len(result.results) <= 3
    assert {hit.number for hit in result.results} <= {"23", "23a", "24"}


def test_hoehere_schwelle_macht_aus_ambiguous_noch_kein_raten(menu):
    result = search(menu, "Frühlingsrollen", threshold_high=0.99)
    assert result.match_type == "ambiguous"


def test_ausverkauft_wird_gemeldet_aber_nicht_verschwiegen(menu):
    result = search(menu, "Nummer zwölf")
    assert result.results[0].sold_out is True


def test_ohne_treffer_kommt_not_found(menu):
    with pytest.raises(NotFound):
        search(menu, "Pizza Salami")


def test_inaktives_gericht_bleibt_unsichtbar(menu):
    with pytest.raises(NotFound):
        search(menu, "Nummer neunundneunzig")


def test_leere_anfrage_ist_invalid_input(menu):
    with pytest.raises(InvalidInput):
        search(menu, "   ")


def test_fremder_mandant_sieht_die_karte_nicht(menu):
    session, _, _ = menu
    with pytest.raises(NotFound):
        search_menu(session, uuid.uuid4(), "Nummer 23", now=NOW)


def test_hoechstens_drei_vorschlaege(menu):
    result = search(menu, "Gebratene Nudeln", max_results=3)
    assert len(result.results) <= 3


def test_alias_an_zwei_gerichten_bleibt_ambiguous_auch_bei_einem_vorschlag(menu):
    """Codex-Review PR #118 (P1): die Obergrenze darf die Mehrdeutigkeit nicht verstecken."""
    result = search(menu, "einmal Bami bitte", max_results=1)
    assert result.match_type == "ambiguous"
    assert len(result.results) == 1


def test_kartennummer_mit_buchstabe_trifft_nicht_die_nackte_zahl(menu):
    """Codex-Review PR #118 (P1): "23a" ist nicht 23."""
    _, _, items = menu
    result = search(menu, "einmal die Nummer 23a")
    assert result.match_type == "exact_number"
    assert result.results[0].menu_item_id == items["23a"].id


def test_buchstabe_getrennt_gesprochen_gehoert_zur_nummer(menu):
    _, _, items = menu
    result = search(menu, "Nummer 23 a bitte")
    assert result.results[0].menu_item_id == items["23a"].id


def test_nackte_zahl_bleibt_beim_gericht_ohne_buchstabe(menu):
    _, _, items = menu
    result = search(menu, "die Nummer dreiundzwanzig")
    assert result.results[0].menu_item_id == items["23"].id


def test_fuehrende_null_in_der_kartennummer_wird_gefunden(menu):
    _, _, items = menu
    result = search(menu, "Nummer eins")
    assert result.match_type == "exact_number"
    assert result.results[0].menu_item_id == items["01"].id


def test_menge_wird_auch_als_ziffer_nicht_zur_kartennummer(menu):
    """ "2 x die 23": die 2 ist die Menge, nicht das Gericht (CLAUDE.md §2 Regel 2)."""
    _, _, items = menu
    result = search(menu, "2 x die 23")
    assert result.results[0].menu_item_id == items["23"].id


def test_zwei_genannte_zahlen_werden_nicht_geraten(menu):
    with pytest.raises(NotFound):
        search(menu, "23 und 40")
