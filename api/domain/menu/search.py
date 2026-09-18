"""search_menu: vom Gesagten zur menu_item_id (docs/04 §search_menu, T-4.3).

Die Auflösungsreihenfolge aus docs/04, streng in dieser Reihenfolge:

1. Zahl im Satz -> exakter Treffer auf die Kartennummer (`exact_number`).
   Nennt der Gast eine Nummer, die es nicht gibt, ist das `not_found` - die
   Suche weicht dann nicht auf ähnliche Namen aus. Wer "Nummer 99" sagt, meint
   kein Gericht, das zufällig ähnlich heisst (CLAUDE.md §2 Regel 2).
2. Alias exakt (`alias`). Hängt derselbe Alias an mehreren Gerichten, ist das
   `ambiguous` - der Importer hat davor gewarnt, die Suche fragt nach.
3. Unscharf über Name und Aliase (pg_trgm): genau ein Treffer über der hohen
   Schwelle -> `fuzzy_single`; sonst alle über der niedrigen -> `ambiguous`
   mit höchstens drei Vorschlägen; keiner -> `not_found`.

Gesucht wird nur in aktiven Gerichten. Ausverkaufte kommen mit `sold_out: true`
und einem Satz zurück: der Agent soll sagen, dass es heute aus ist, statt so zu
tun, als gäbe es das Gericht nicht.

Die Trigram-Werte rechnet die Datenbank über alle aktiven Gerichte des Mandanten
aus. Bei einer Karte von ein paar hundert Zeilen ist das schneller als jede
Vorfilterung; der GIN-Index trägt die Suche, sobald die Karte wächst.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import NotFound
from api.core.time import utcnow
from api.domain.menu.normalize import normalize_alias, normalize_query
from api.domain.menu.numberwords import (
    find_item_number_ref,
    find_marked_item_numbers,
)
from api.models import ItemAlias, ItemOption, MenuItem
from api.schemas.menu import MenuHit, OptionGroup, OptionOut, SearchResult

SAY_NOT_FOUND = (
    "Das habe ich auf der Karte nicht gefunden. Können Sie mir die Nummer sagen?"
)
SAY_NO_SUCH_NUMBER = (
    "Die Nummer {number} habe ich nicht auf der Karte. Können Sie das noch "
    "einmal sagen?"
)
SAY_NO_SUCH_NUMBERS = (
    "Die Nummern {numbers} habe ich nicht auf der Karte. Können Sie das noch "
    "einmal sagen?"
)
SAY_SOLD_OUT = "{name} ist heute leider aus."
AMBIGUOUS_LIMIT = 3


def _active(tenant_id: uuid.UUID) -> tuple:
    return (MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True))


def _by_number(session: Session, tenant_id: uuid.UUID, spoken: str) -> list[MenuItem]:
    """Aktive Gerichte zu einer gesagten Kartennummer.

    Verglichen wird der Text ohne führende Nullen: "07" findet 7, "acht" findet
    08. Die Kartennummer ist Text (docs/14), eine Umwandlung über int verlöre
    "23a" (Befund Codex PR #116); Zahl, Buchstabe und Marker stammen aus
    derselben Stelle im Satz (Befund Codex PR #117).
    """
    stored = func.lower(
        func.coalesce(func.nullif(func.ltrim(MenuItem.number, "0"), ""), "0")
    )
    return list(
        session.scalars(
            select(MenuItem)
            .where(*_active(tenant_id), stored == (spoken.lstrip("0") or "0"))
            .order_by(MenuItem.number)
        )
    )


def _sold_out(item: MenuItem, now: datetime) -> bool:
    return item.sold_out_until is not None and item.sold_out_until > now


def _hits(session: Session, items: list[MenuItem], now: datetime) -> list[MenuHit]:
    options = session.scalars(
        select(ItemOption)
        .where(ItemOption.menu_item_id.in_([i.id for i in items]))
        .order_by(
            ItemOption.group_name,
            ItemOption.is_default.desc(),
            ItemOption.option_name,
        )
    ).all()
    by_item: dict[uuid.UUID, dict[str, OptionGroup]] = {}
    for option in options:
        groups = by_item.setdefault(option.menu_item_id, {})
        group = groups.setdefault(
            option.group_name,
            OptionGroup(group=option.group_name, required=option.required, options=[]),
        )
        group.options.append(
            OptionOut(
                name=option.option_name,
                price_delta_cents=option.price_delta_cents,
                default=option.is_default,
            )
        )
    return [
        MenuHit(
            menu_item_id=item.id,
            number=item.number,
            name=item.name,
            price_cents=item.price_cents,
            sold_out=_sold_out(item, now),
            option_groups=list(by_item.get(item.id, {}).values()),
        )
        for item in items
    ]


def _single(
    session: Session, match_type: str, item: MenuItem, now: datetime
) -> SearchResult:
    say = SAY_SOLD_OUT.format(name=item.name) if _sold_out(item, now) else None
    return SearchResult(
        match_type=match_type, results=_hits(session, [item], now), say=say
    )


def _ambiguous(session: Session, items: list[MenuItem], now: datetime) -> SearchResult:
    names = [f"Nummer {i.number}, {i.name}" for i in items]
    if len(names) == 1:
        question = f"Meinen Sie {names[0]}?"
    else:
        question = f"Meinen Sie {', '.join(names[:-1])} oder {names[-1]}?"
    return SearchResult(
        match_type="ambiguous", results=_hits(session, items, now), say=question
    )


def search_menu(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    max_results: int = AMBIGUOUS_LIMIT,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
) -> SearchResult:
    now = now or utcnow()
    high = settings.menu_fuzzy_threshold_high if high is None else high
    low = settings.menu_fuzzy_threshold_low if low is None else low
    limit = min(max_results, AMBIGUOUS_LIMIT)

    text = normalize_query(query)

    # 1. Nummer. Ohne "Nummer" im Satz zählt eine Zahl nur, wenn sonst nichts
    # vom Gericht gesagt wurde ("die 23"); neben einem Namen ist sie eine Menge
    # ("zwei Frühlingsrollen" ist nicht Gericht 2).
    marked = find_marked_item_numbers(query)
    if len(marked) > 1:
        # "Nummer 23, nein, Nummer 24" oder "Nummer 23 oder Nummer 24": beide
        # zur Wahl stellen statt die erste zu nehmen (Befund Codex PR #117).
        items = [i for ref in marked for i in _by_number(session, tenant_id, ref.text)]
        if not items:
            spoken = " und ".join(ref.text for ref in marked)
            raise NotFound(
                f"Nummern {spoken} nicht auf der Karte",
                say=SAY_NO_SUCH_NUMBERS.format(numbers=spoken),
            )
        return _ambiguous(session, items[:limit], now)

    ref = find_item_number_ref(query)
    if ref is not None and (ref.marked or not text):
        items = _by_number(session, tenant_id, ref.text)
        if not items:
            raise NotFound(
                f"Nummer {ref.text} nicht auf der Karte",
                say=SAY_NO_SUCH_NUMBER.format(number=ref.text),
            )
        if len(items) > 1:
            # "7" und "07" auf derselben Karte: nachfragen statt wählen.
            return _ambiguous(session, items[:limit], now)
        return _single(session, "exact_number", items[0], now)

    if not text:
        raise NotFound("Anfrage ohne Inhalt", say=SAY_NOT_FOUND)

    # 2. Alias exakt. Aliase stehen wie aus der Karte da, oft mit Artikel ("die
    # knusprigen rollen"), der Gast sagt "die knusprigen Rollen, bitte". Beide
    # Seiten werden deshalb ohne Füllwörter verglichen. In Python statt SQL:
    # normalize_query gibt es nur hier, und eine Karte hat ein paar hundert
    # Aliase - das ist ein Index-Scan und eine Schleife, keine Last.
    said = {normalize_alias(query), text}
    matched_ids = {
        item_id
        for item_id, alias in session.execute(
            select(ItemAlias.menu_item_id, ItemAlias.alias)
            .join(MenuItem, ItemAlias.menu_item_id == MenuItem.id)
            .where(*_active(tenant_id))
        )
        if alias in said or normalize_query(alias) == text
    }
    by_alias = (
        session.scalars(
            select(MenuItem)
            .where(MenuItem.id.in_(matched_ids))
            .order_by(MenuItem.number)
        ).all()
        if matched_ids
        else []
    )
    if len(by_alias) == 1:
        return _single(session, "alias", by_alias[0], now)
    if by_alias:
        return _ambiguous(session, list(by_alias)[:limit], now)

    # 3. Unscharf: das bessere von Name und bestem Alias, je Gericht.
    def score(column):
        return func.greatest(
            func.similarity(column, text), func.word_similarity(text, column)
        )

    alias_score = (
        select(func.max(score(ItemAlias.alias)))
        .where(ItemAlias.menu_item_id == MenuItem.id)
        .correlate(MenuItem)
        .scalar_subquery()
    )
    total = func.greatest(
        score(func.lower(MenuItem.name)), func.coalesce(alias_score, 0)
    )
    rows = session.execute(
        select(MenuItem, total.label("score"))
        .where(*_active(tenant_id), total >= low)
        .order_by(total.desc(), MenuItem.number)
        .limit(AMBIGUOUS_LIMIT + 1)
    ).all()
    if not rows:
        raise NotFound(f"Kein Treffer für „{text}“", say=SAY_NOT_FOUND)
    strong = [item for item, value in rows if value >= high]
    if len(strong) == 1:
        return _single(session, "fuzzy_single", strong[0], now)
    return _ambiguous(session, [item for item, _ in rows][:limit], now)
