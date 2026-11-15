"""Probe area E, PR #721 (#676): a card number never lost or invented in a split.

Uses the sushi menu of test_domain_menu_search (23 Frühlingsrollen, 24
Sommerrollen, 13 Pho Bo, S12 Thunfisch Maki, ...)."""
import pytest

from api.core.errors import Ambiguous, NotFound
from api.domain.menu.search import CLEAR_MATCHES, position_parts, search_menu
from api.tests.test_domain_menu_search import NOW, KARTE, engine, session, sushi, tenant_id  # noqa: F401


def _clear(session, tid, said):
    try:
        found = search_menu(session, tid, said, now=NOW)
    except (Ambiguous, NotFound):
        return None
    return found if found.match_type in CLEAR_MATCHES else None


@pytest.mark.parametrize(
    "said",
    [
        "Pho Bo äh die 23",
        "die 23 äh Pho Bo",
        "die dreiundzwanzig äh Pho Bo",
        "Die 23. Pho Bo.",
        "Pho Bo. Die 23.",
        "Pho Bo? Die 23",
        "die 23 - Pho Bo",
        "die 23 also Pho Bo",
        "Pho Bo dazu die 23",
    ],
)
def test_a_card_number_beside_a_name_without_comma_asks(session, sushi, said):
    """#676 item 2 required: a sentence kept whole with a card number asks.
    The guard cuts only at comma/und/sowie, so a hesitation or a full stop the
    ear writes without a comma takes Pho Bo clear and drops the 23."""
    found = _clear(session, sushi, said)
    assert found is None, f"{said!r} -> clear {[h.number for h in found.results]}"


@pytest.mark.parametrize(
    "said",
    [
        "die 23 ohne Zwiebeln, nee, Pho Bo",
        "die 23 ohne Zwiebeln, äh, Pho Bo",
        "die 23 ohne Zwiebeln, oder Pho Bo",
        "S12 ohne Wasabi, äh, Pho Bo",
        "die 23 äh Pho Bo ohne Koriander",
    ],
)
def test_a_wish_does_not_bypass_the_card_number_guard(session, sushi, said):
    """The guard in search_menu runs after _search_with_wish returned: with a
    removal in the sentence the corrected dish lands in the kitchen note."""
    found = _clear(session, sushi, said)
    assert found is None, (
        f"{said!r} -> clear {[h.number for h in found.results]}, wish {found.wish}"
    )


@pytest.mark.parametrize(
    "said",
    [
        "die 23, ne, die 24",
        "die 23, quatsch, die 24",
        "die 23, sorry, die 24",
        "die 23, doch die 24",
        "die 23, Moment, die 24",
    ],
)
def test_a_correction_between_two_numbers_is_not_two_positions(session, sushi, said):
    """Regression of #721's cut in whole(): before it, "die 23, ne" stayed one
    piece and its search found nothing; now the 23 is cut out as a clear
    position next to the 24, so the corrected 23 is booked as well."""
    parts = position_parts(session, sushi, said, now=NOW)
    clear = [p for p in parts if _clear(session, sushi, p) is not None]
    assert len(clear) < 2, f"{said!r} -> parts {parts}, clear {clear}"
