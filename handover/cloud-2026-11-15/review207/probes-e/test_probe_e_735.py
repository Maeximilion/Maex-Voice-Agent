"""Probe area E, PR #735 (#719): an allergy behind the caller's denial, as the code stands after #788."""
import pytest

from api.core.errors import Ambiguous, NotFound
from api.domain.menu.search import search_menu
from api.domain.menu.wishes import classify_wish
from api.tests.test_domain_menu_search import NOW, KARTE, engine, session, sushi, tenant_id  # noqa: F401

EARLIER = [
    ("Ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam", "nuss"),
    ("Meine Tochter hat eine Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam", "erdnuss"),
    ("Mein Sohn ist gegen Erdnuss allergisch. Ich habe keine Allergie, aber meine Frau gegen Sesam", "erdnuss"),
    ("Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam", "erdnuss"),
]


@pytest.mark.parametrize(("said", "lost"), EARLIER)
def test_an_allergy_said_before_the_denial_is_kept(said, lost):
    """`_theirs_behind_a_denial` runs first in `_with_allergy`, judges the
    denial only on the last sentence before the comma, and builds the note
    from the behind-clause and the removals: an allergy in an earlier
    sentence is dropped, and the result is a confident single-ingredient
    allergy instead of the open allergy (asked) it was without this path."""
    wish = classify_wish(said, [])
    assert wish is not None
    assert wish.ingredient is None or lost in wish.text.lower(), wish


@pytest.mark.parametrize(
    "said",
    [
        "Pho Bo, ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam",
        "die 13, meine Tochter hat eine Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
    ],
)
def test_through_search(session, sushi, said):
    try:
        found = search_menu(session, sushi, said, now=NOW)
    except (Ambiguous, NotFound):
        return
    wish = found.wish
    print(said, "->", found.match_type, [h.number for h in found.results], wish)
    assert wish is None or wish.ingredient is None or "nuss" in wish.text.lower(), wish
