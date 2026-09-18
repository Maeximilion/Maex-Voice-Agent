"""search_menu: gesprochene Bestellung auf die Karte abbilden (T-4.3, docs/04).

Das wichtigste Tool und die groesste Fehlerquelle, deshalb eine feste
Reihenfolge von sicher nach unsicher:

1. Zahl im Satz -> exakter Treffer auf `menu_items.number` (`exact_number`)
2. Alias, exakt und normalisiert (`alias`)
3. Unscharfe Suche ueber Name und Alias per Trigram
   - genau ein Treffer ueber der hohen Schwelle -> `fuzzy_single`
   - mehrere Treffer ueber der niedrigen Schwelle -> `ambiguous`, der Agent fragt nach
   - keiner -> `not_found`

Geraten wird nie (CLAUDE.md §2 Regel 2): `ambiguous` ist eine Rueckfrage, keine
Auswahl. Die Schwellen stehen in der Konfiguration, weil sie sich erst am
echten Gespraech einstellen lassen (`MENU_FUZZY_THRESHOLD_HIGH`/`_LOW`).

Inaktive Gerichte tauchen nie auf. Ausverkaufte schon: der Agent muss sagen
koennen, dass es das Gericht gibt, es heute aber aus ist.
"""

import re
import uuid
from datetime import datetime

from sqlalchemy import Float, func, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import InvalidInput, NotFound
from api.core.time import utcnow
from api.domain.menu.items import is_sold_out, option_groups
from api.domain.menu.normalize import normalize_alias
from api.domain.menu.numberwords import find_item_number
from api.models import ItemAlias, MenuItem
from api.schemas.menu import MAX_RESULTS, MenuHit, MenuSearch

# Fuellwoerter des Bestellsatzes. Sie stehen nie in einem Alias der Karte
# (docs/14), verwaessern aber jeden Trigram-Vergleich: "einmal die
# Fruehlingsrollen bitte" gegen "Fruehlingsrollen" verliert sonst die Haelfte
# der Aehnlichkeit an Text, der nichts bezeichnet.
_FILLER = frozenset(
    {
        "ich",
        "wir",
        "haette",
        "hätte",
        "hatte",
        "will",
        "wollte",
        "moechte",
        "möchte",
        "nehme",
        "nehmen",
        "bekomme",
        "bekommen",
        "kriege",
        "gern",
        "gerne",
        "bitte",
        "danke",
        "einmal",
        "zweimal",
        "dreimal",
        "mal",
        "eine",
        "einen",
        "einem",
        "ein",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "nummer",
        "nr",
        "und",
        "noch",
        "dazu",
        "auch",
        "bestellen",
        "bestellung",
        "also",
        "ja",
        "dann",
        "so",
    }
)


def search_menu(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    max_results: int = MAX_RESULTS,
    now: datetime | None = None,
    threshold_high: float | None = None,
    threshold_low: float | None = None,
) -> MenuSearch:
    text = query.strip()
    if not text:
        raise InvalidInput("query ist leer")
    now = now or utcnow()
    max_results = max(1, min(max_results, MAX_RESULTS))
    high = (
        settings.menu_fuzzy_threshold_high if threshold_high is None else threshold_high
    )
    low = settings.menu_fuzzy_threshold_low if threshold_low is None else threshold_low

    hit = _by_number(session, tenant_id, text)
    if hit is not None:
        return _answer("exact_number", [hit], session, now)

    items = _by_alias(session, tenant_id, text, max_results)
    if len(items) == 1:
        return _answer("alias", items, session, now)
    if items:
        return _answer("ambiguous", items[:max_results], session, now)

    scored = _by_similarity(session, tenant_id, text, max_results, low)
    if not scored:
        raise NotFound("Kein Gericht zu dieser Nennung gefunden")
    above_high = [item for item, score in scored if score >= high]
    if len(above_high) == 1:
        return _answer("fuzzy_single", above_high, session, now)
    return _answer(
        "ambiguous", [item for item, _ in scored[:max_results]], session, now
    )


def needle(text: str) -> str:
    """Der Suchtext ohne Fuellwoerter, normalisiert wie ein Alias beim Import.

    Bleibt nichts uebrig ("einmal bitte"), zaehlt der ganze Satz: lieber ein
    schwacher Vergleich als gar keiner.
    """
    normalized = normalize_alias(text)
    words = [w for w in normalized.split(" ") if w and w not in _FILLER]
    return " ".join(words) or normalized


def _active(tenant_id: uuid.UUID):
    return (MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True))


def card_numbers(text: str) -> list[str]:
    """Kandidaten fuer die Kartennummer, vom genauesten zum allgemeinsten.

    Die Kartennummer ist Text, nicht Zahl (docs/14): "23a" und "01" kommen vor.
    Ueber die Zahl allein waere "die Nummer 23a" die 23 - ein falsches Gericht,
    und das waere geraten (Codex-Review PR #118, P1). Deshalb zaehlt zuerst die
    Nummer mit Buchstabe, auch getrennt gesprochen ("23 a"), danach die Zahl aus
    `find_item_number` und ihre Formen mit fuehrender Null. Welcher Kandidat
    gilt, entscheidet die Karte: was es nicht gibt, trifft auch nicht.

    Eine nackte Ziffernfolge ohne Buchstabe kommt **nur** ueber
    `find_item_number` herein. Sonst wuerde die Menge in "2 x die 23" zur
    Kartennummer, und bei zwei genannten Zahlen waere die erste eine Vermutung.
    """
    tokens = normalize_alias(text).split(" ")
    lettered: list[str] = []
    for i, token in enumerate(tokens):
        match = re.fullmatch(r"(\d{1,3})([a-z])?", token)
        if match is None:
            continue
        if match.group(2) is not None:
            lettered.append(token)
            continue
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        if re.fullmatch(r"[a-z]", following):
            lettered.append(token + following)
    # Zwei verschiedene Nummern mit Buchstabe: nicht entscheidbar, also keine.
    if len(set(lettered)) > 1:
        lettered = []

    spoken: list[str] = []
    number = find_item_number(text)
    if number is not None:
        # "dreiundzwanzig a": der Buchstabe steht allein im Satz.
        letters = {t for t in tokens if re.fullmatch(r"[a-z]", t)}
        spoken += [f"{number}{letter}" for letter in sorted(letters)]
        spoken.append(str(number))
        spoken += [str(number).zfill(width) for width in (2, 3)]

    seen: set[str] = set()
    return [c for c in lettered + spoken if not (c in seen or seen.add(c))]


def _by_number(session: Session, tenant_id: uuid.UUID, text: str) -> MenuItem | None:
    candidates = card_numbers(text)
    if not candidates:
        return None
    rows = session.scalars(
        select(MenuItem).where(
            *_active(tenant_id), func.lower(MenuItem.number).in_(candidates)
        )
    ).all()
    found = {row.number.lower(): row for row in rows}
    return next((found[c] for c in candidates if c in found), None)


def _by_alias(
    session: Session, tenant_id: uuid.UUID, text: str, max_results: int
) -> list[MenuItem]:
    """Exakter Alias-Treffer. Haengt derselbe Alias an mehreren Gerichten, wird gefragt.

    Geholt wird ein Gericht mehr als gefragt: sonst versteckt die Obergrenze die
    Mehrdeutigkeit, und aus zwei Gerichten am selben Alias wuerde bei
    `max_results=1` ein sicherer Treffer - also ein Raten (Codex-Review PR #118,
    P1). Der Aufrufer kuerzt erst, nachdem er die Mehrdeutigkeit gesehen hat.
    """
    candidates = {normalize_alias(text), needle(text)}
    items = session.scalars(
        select(MenuItem)
        .join(ItemAlias, ItemAlias.menu_item_id == MenuItem.id)
        .where(*_active(tenant_id), ItemAlias.alias.in_(candidates))
        .order_by(MenuItem.number)
        .distinct()
        .limit(max_results + 1)
    ).all()
    return list(items)


def _by_similarity(
    session: Session,
    tenant_id: uuid.UUID,
    text: str,
    max_results: int,
    low: float,
) -> list[tuple[MenuItem, float]]:
    """Trigram ueber Name und Alias, der bessere der beiden zaehlt je Gericht.

    Es wird ein Treffer mehr geholt als gefragt: erst daran laesst sich sehen,
    ob ueber der hohen Schwelle wirklich nur einer steht.
    """
    probe = needle(text)
    score = func.greatest(
        func.similarity(MenuItem.name, probe),
        func.coalesce(func.max(func.similarity(ItemAlias.alias, probe)), 0.0),
    )
    rows = session.execute(
        select(MenuItem, score.cast(Float).label("score"))
        .outerjoin(ItemAlias, ItemAlias.menu_item_id == MenuItem.id)
        .where(*_active(tenant_id))
        .group_by(MenuItem.id)
        .having(score >= low)
        .order_by(score.desc(), MenuItem.number)
        .limit(max_results + 1)
    ).all()
    return [(row[0], float(row[1])) for row in rows]


def _answer(
    match_type: str, items: list[MenuItem], session: Session, now: datetime
) -> MenuSearch:
    groups = option_groups(session, [item.id for item in items])
    return MenuSearch(
        match_type=match_type,
        results=[
            MenuHit(
                menu_item_id=item.id,
                number=item.number,
                name=item.name,
                price_cents=item.price_cents,
                sold_out=is_sold_out(item, now),
                option_groups=groups.get(item.id, []),
            )
            for item in items
        ],
    )
