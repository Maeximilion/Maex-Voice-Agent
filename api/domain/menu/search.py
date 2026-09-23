"""search_menu: vom Gesagten zur menu_item_id (docs/04 §search_menu, T-4.3).

Die Auflösungsreihenfolge aus docs/04, streng in dieser Reihenfolge:

1. Nummer -> exakter Treffer auf die Kartennummer (`exact_number`), aber nur,
   wenn der ganze Satz genau eine Nummer ist (Regel A, numberwords.
   sole_item_number). Steht mehr daneben - eine zweite Zahl, ein Name, "oder" -
   ist das `ambiguous` mit der Frage nach der einen Nummer. Nennt der Gast eine
   Nummer, die es nicht gibt, ist das `not_found`; die Suche weicht dann nicht
   auf ähnliche Namen aus (CLAUDE.md §2 Regel 2).
2. Alias exakt (`alias`). Hängt derselbe Alias an mehreren Gerichten, ist das
   `ambiguous` - der Importer hat davor gewarnt, die Suche fragt nach.
3. Unscharf über Name und Aliase (pg_trgm): genau ein Treffer über der hohen
   Schwelle -> `fuzzy_single`; sonst alle über der niedrigen -> `ambiguous`
   mit höchstens drei Vorschlägen; keiner -> `not_found`.

Gesucht wird nur in aktiven Gerichten. Ausverkaufte kommen mit `sold_out: true`
und einem Satz zurück: der Agent soll sagen, dass es heute aus ist, statt so zu
tun, als gäbe es das Gericht nicht.

Die unscharfe Suche läuft in zwei Schritten: erst ein Vorfilter mit den
Operatoren `%` und `<%`, der die GIN-Indizes auf `menu_items.name` und
`item_aliases.alias` benutzt, dann die genauen Werte nur auf den Treffern. Der
Vorfilter vergleicht gegen die Schwellen der Sitzung, die dafür auf dieselbe
niedrige Schwelle gesetzt werden - er ist damit deckungsgleich mit der
Bedingung und schneidet nichts weg. Bei ein paar hundert Zeilen ist der
Unterschied klein, mit wachsender Karte trägt der Index die Suche.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from functools import partial

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import Ambiguous, NotFound
from api.core.time import utcnow
from api.domain.menu.items import is_sold_out as _sold_out
from api.domain.menu.items import option_groups
from api.domain.menu.normalize import normalize_alias, normalize_query
from api.domain.menu.numberwords import sole_item_number
from api.domain.menu.split import raw_pieces, split_positions
from api.models import ItemAlias, MenuItem
from api.schemas.menu import MenuHit, SearchResult

SAY_NOT_FOUND = (
    "Das habe ich auf der Karte nicht gefunden. Können Sie mir die Nummer sagen?"
)
SAY_NO_SUCH_NUMBER = (
    "Die Nummer {number} habe ich nicht auf der Karte. Können Sie das noch "
    "einmal sagen?"
)
SAY_WHICH_NUMBER = "Welche Nummer meinen Sie? Bitte sagen Sie mir nur die eine Nummer."
SAY_SOLD_OUT = "{name} ist heute leider aus."
SAY_ONE_AT_A_TIME = (
    "Das waren mehrere Sachen. Sagen Sie mir bitte eins nach dem anderen - was zuerst?"
)
AMBIGUOUS_LIMIT = 3
# Abstand der Sitzungsschwelle zur eigentlichen Schwelle, damit der Vorfilter
# sicher eine Obermenge bleibt. Klein genug, um keine echte Zeile zusaetzlich
# zu holen, gross genug fuer den Vergleich in float4.
PREFILTER_EPSILON = 1e-4


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


def _hits(session: Session, items: list[MenuItem], now: datetime) -> list[MenuHit]:
    groups = option_groups(session, [i.id for i in items])
    return [
        MenuHit(
            menu_item_id=item.id,
            number=item.number,
            name=item.name,
            price_cents=item.price_cents,
            sold_out=_sold_out(item, now),
            option_groups=groups.get(item.id, []),
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


CLEAR_MATCHES = ("exact_number", "alias", "fuzzy_single")


def position_parts(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
) -> list[str]:
    """Die Positionen eines Satzes: erst nach dem Satz (`split_positions`), dann
    mit der Karte.

    "die 23 und Pho Bo": "Pho Bo" eroeffnet ohne Menge keine Position, der Satz
    allein bliebe ganz, und die Namenssuche ueber den ganzen Satz faende nur Pho
    Bo - die 23 fiele still weg (Codex PR #127, P1). Trifft jedes Stueck an den
    Trennern fuer sich eindeutig ein **anderes** Gericht, sind es mehrere
    Positionen. Trifft eines nichts oder dasselbe, war das "und" Teil eines
    Namens ("Ente süß und sauer"), und der Satz bleibt ganz.

    Vorher gelten zusammenhaengende Stuecke: steht der ganze Satz oder ein Teil
    davon selbst so auf der Karte ("Fisch und Chips" als Alias oder als Name),
    ist er ein Gericht, auch wenn "Fisch" und "Chips" es einzeln auch sind -
    auch mitten in einer Aufzaehlung ("Fisch und Chips und Pho Bo", Codex PR
    #127, P1). Gesucht wird von links, das laengste Stueck zuerst.
    """
    parts = split_positions(query)
    if len(parts) > 1:
        return parts
    pieces = raw_pieces(query)
    if len(pieces) <= 1:
        return [query]
    search = partial(
        search_menu, session, tenant_id, now=now, high=high, low=low, split_check=False
    )
    spans = _spans(query, pieces)
    positions: list[str] = []
    # Je Gericht die Worte, mit denen es genannt wurde. "Pho Bo und Pho Bo" sind
    # zwei Portionen (Codex PR #127, P1); "Pho und Pho Bo" trifft dasselbe mit
    # anderen Worten - das kann eine Praezisierung sein, der Satz bleibt ganz.
    said: dict[uuid.UUID, set[str]] = {}
    i = 0
    while i < len(pieces):
        for j in range(len(pieces) - 1, i, -1):
            text = query[spans[i][0] : spans[j][1]]
            if _whole_dish(session, tenant_id, search, text, pieces[i : j + 1]):
                positions.append(text)
                i = j + 1
                break
        else:
            piece = pieces[i]
            try:
                found = search(piece)
            except (Ambiguous, NotFound):
                return [query]
            if found.match_type not in CLEAR_MATCHES or not found.results:
                return [query]
            said.setdefault(found.results[0].menu_item_id, set()).add(
                normalize_query(piece)
            )
            positions.append(piece)
            i += 1
    if len(positions) == 1 or any(len(words) > 1 for words in said.values()):
        return [query]
    return positions


def _spans(query: str, pieces: list[str]) -> list[tuple[int, int]]:
    """Wo jedes Stueck im Satz steht, damit benachbarte Stuecke mit ihrem
    Trenner wieder zusammengesetzt werden koennen, wie der Gast sie sagte."""
    spans = []
    start = 0
    for piece in pieces:
        begin = query.index(piece, start)
        start = begin + len(piece)
        spans.append((begin, start))
    return spans


def _whole_dish(
    session: Session,
    tenant_id: uuid.UUID,
    search: Callable[[str], SearchResult],
    query: str,
    pieces: list[str],
) -> bool:
    """Ist der ganze Satz genau ein Gericht der Karte: Alias oder derselbe Name?
    Unscharf zaehlt nicht - "die 23 und Pho Bo" traefe unscharf Pho Bo.

    Verglichen wird in der Form der Suche (normalize_query): "einmal Fisch und
    Chips, bitte" ist der Name mit Menge und Fuellwort (Codex PR #127, P1). Die
    Form wirft auch Nummern weg, aus "Pho Bo und die 23" bliebe "pho bo". Darum
    muss jedes Stueck dabei Inhalt behalten: "die 23" allein ist ein eigenes
    Gericht, kein Teil des Namens. Das gilt auch fuer den Alias: "die 23 und
    Pho" traefe sonst den Alias "Pho" (Codex PR #127, P1).

    Ein Alias zaehlt auch, wenn er an mehreren Gerichten haengt: dann fragt die
    Suche ueber den ganzen Satz, welches gemeint ist, statt die Stuecke als
    Positionen zu nehmen (Codex PR #127, P2)."""
    if not all(normalize_query(p) for p in pieces):
        return False
    if _alias_items(session, tenant_id, query):
        return True
    try:
        found = search(query)
    except (Ambiguous, NotFound):
        return False
    if not found.results:
        return False
    said = normalize_query(query)
    return any(normalize_query(hit.name) == said for hit in found.results)


def _alias_items(session: Session, tenant_id: uuid.UUID, query: str) -> list[MenuItem]:
    """Aktive Gerichte, deren Alias genau das Gesagte ist (Schritt 2 der Suche)."""
    text = normalize_query(query)
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
    if not matched_ids:
        return []
    return list(
        session.scalars(
            select(MenuItem)
            .where(MenuItem.id.in_(matched_ids))
            .order_by(MenuItem.number)
        )
    )


def search_menu(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    max_results: int = AMBIGUOUS_LIMIT,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
    *,
    split_check: bool = True,
) -> SearchResult:
    """`split_check=False` nur fuer `position_parts`: die Suche je Stueck und ueber
    den ganzen Satz darf nicht wieder in die Pruefung auf mehrere Positionen."""
    now = now or utcnow()
    high = settings.menu_fuzzy_threshold_high if high is None else high
    low = settings.menu_fuzzy_threshold_low if low is None else low
    limit = min(max_results, AMBIGUOUS_LIMIT)

    text = normalize_query(query)

    # 1. Nummer - Regel A: direkt nur, wenn der ganze Satz genau eine Nummer
    # ist ("Nummer 23", "die 23", "zweimal die 23"). Steht mehr daneben (eine
    # zweite Zahl, ein Name, "oder"), fragt die Suche nach, statt eine Zahl zu
    # wählen. Ohne "Nummer" ist eine Zahl neben einem Namen eine Menge ("zwei
    # Frühlingsrollen") und die Namenssuche entscheidet.
    ref, unclear = sole_item_number(query)
    if unclear:
        raise Ambiguous("Nummer nicht eindeutig", say=SAY_WHICH_NUMBER)
    if ref is not None:
        # "23g": eine Endung, die es auf keiner Karte gibt, ist nicht die 23.
        items = _by_number(session, tenant_id, ref.text) if ref.valid else []
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

    # Ein Satz, eine Position: nennt der Satz mehrere, fragt die Suche nach,
    # statt die Namenssuche über den ganzen Satz laufen zu lassen - die fände
    # eine und verschluckte die andere still (Codex PR #124, P1). Zerlegt wird
    # hier nichts; die Teile stehen in der Meldung, der Aufrufer fragt je Teil.
    parts = (
        position_parts(session, tenant_id, query, now=now, high=high, low=low)
        if split_check
        else [query]
    )
    if len(parts) > 1:
        raise Ambiguous(
            "mehrere Positionen: " + " | ".join(parts), say=SAY_ONE_AT_A_TIME
        )

    # 2. Alias exakt. Aliase stehen wie aus der Karte da, oft mit Artikel ("die
    # knusprigen rollen"), der Gast sagt "die knusprigen Rollen, bitte". Beide
    # Seiten werden deshalb ohne Füllwörter verglichen. In Python statt SQL:
    # normalize_query gibt es nur hier, und eine Karte hat ein paar hundert
    # Aliase - das ist ein Index-Scan und eine Schleife, keine Last.
    by_alias = _alias_items(session, tenant_id, query)
    if len(by_alias) == 1:
        return _single(session, "alias", by_alias[0], now)
    if by_alias:
        return _ambiguous(session, list(by_alias)[:limit], now)

    # 3. Unscharf: das bessere von Name und bestem Alias, je Gericht.
    #
    # Zwei Schritte, weil nur der erste den GIN-Index benutzen kann: die
    # Operatoren % und <% schlagen im Index nach, ein greatest(similarity(...))
    # im WHERE muss jede aktive Zeile anfassen (Codex PR #117, P2).
    #
    # Der Vorfilter ist bewusst eine Obermenge, nicht die genaue Bedingung: die
    # Sitzungsschwelle liegt eine Winzigkeit unter `low`. Ob die Operatoren auf
    # ">" oder ">=" gegen ihre Schwelle pruefen, haengt an der Version; ein
    # Treffer genau auf der Schwelle waere sonst schon hier weg, obwohl
    # `total >= low` ihn behalten wuerde (Codex PR #117, P2). Entschieden wird
    # ohnehin unten in der Abfrage, der Vorfilter spart nur Zeilen.
    #
    # `SET LOCAL` ueber set_config(..., true): die Werte gelten nur fuer diese
    # Transaktion und bleiben nicht an der Verbindung aus dem Pool haengen.
    grenze = max(0.0, low - PREFILTER_EPSILON)
    session.execute(
        select(
            func.set_config("pg_trgm.similarity_threshold", str(grenze), True),
            func.set_config("pg_trgm.word_similarity_threshold", str(grenze), True),
        )
    )

    def score(column):
        return func.greatest(
            func.similarity(column, text), func.word_similarity(text, column)
        )

    def candidate(column):
        # Obermenge von score(column) >= low, indexgestuetzt.
        return or_(column.op("%")(text), literal(text).op("<%")(column))

    # Ohne lower(): pg_trgm bildet seine Trigramme selbst in Kleinschreibung,
    # und ein lower(name) im Ausdruck passt nicht mehr zum Index auf name.
    alias_score = (
        select(func.max(score(ItemAlias.alias)))
        .where(ItemAlias.menu_item_id == MenuItem.id)
        .correlate(MenuItem)
        .scalar_subquery()
    )
    alias_candidate = (
        select(1)
        .where(ItemAlias.menu_item_id == MenuItem.id, candidate(ItemAlias.alias))
        .correlate(MenuItem)
        .exists()
    )
    total = func.greatest(score(MenuItem.name), func.coalesce(alias_score, 0))
    rows = session.execute(
        select(MenuItem, total.label("score"))
        .where(
            *_active(tenant_id),
            # Bei `low <= 0` faellt der Vorfilter weg: er koennte dann nur noch
            # Zeilen mit Wert genau 0 verlieren, die `total >= low` behaelt.
            *((or_(candidate(MenuItem.name), alias_candidate),) if grenze > 0 else ()),
            # Die Schwelle entscheidet hier, nicht die Sitzungsvariable.
            total >= low,
        )
        .order_by(total.desc(), MenuItem.number)
        .limit(AMBIGUOUS_LIMIT + 1)
    ).all()
    if not rows:
        raise NotFound(f"Kein Treffer für „{text}“", say=SAY_NOT_FOUND)
    strong = [item for item, value in rows if value >= high]
    if len(strong) == 1:
        return _single(session, "fuzzy_single", strong[0], now)
    return _ambiguous(session, [item for item, _ in rows][:limit], now)
