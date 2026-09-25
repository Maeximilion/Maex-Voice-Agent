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
import zlib
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
from api.domain.menu.split import raw_pieces, separator_pieces, split_positions
from api.domain.menu.wishes import (
    classify_wish,
    has_number,
    names_it,
    open_wish,
    opens_with_allergy,
    wish_candidates,
)
from api.models import ItemAlias, MenuItem
from api.schemas.menu import MenuHit, SearchResult, Wish

SAY_NOT_FOUND = (
    "Das habe ich auf der Karte nicht gefunden. Können Sie mir die Nummer sagen?"
)
SAY_NO_SUCH_NUMBER = (
    "Die Nummer {number} habe ich nicht auf der Karte. Können Sie das noch "
    "einmal sagen?"
)
SAY_WHICH_NUMBER = "Welche Nummer meinen Sie? Bitte sagen Sie mir nur die eine Nummer."
SAY_SOLD_OUT = "{name} ist heute leider aus."
# Mehrere Positionen in einem Satz: der Gast darf das, und er muss nichts
# wiederholen (Maxi, PR #127). Ueber HTTP fragt der Aufrufer danach je Teil.
SAY_IN_TURN = "Einen Moment, ich nehme das der Reihe nach auf."
# Wuensche (T-4.10, D8). Was die Karte nicht kennt, wird nicht angeboten; die
# Allergie geht ohne Zusage an die Kueche. Der Wortlaut zur Allergie ist ein
# Entwurf und wird vor dem Echtbetrieb mit dem Rechts-Check abgestimmt (docs/09).
SAY_WISH_UNKNOWN = (
    "Den Wunsch „{wish}“ kann ich leider nicht anbieten. {name} nehme ich so auf, "
    "wie es auf der Karte steht."
)
SAY_WISH_WHICH_GROUP = "Meinen Sie {option} bei {groups}?"
SAY_ALLERGY_WHICH = "Wogegen sind Sie allergisch? Das gebe ich an die Küche weiter."
# Mehrere Gerichte mit Allergie ohne Zutat: eine Frage nach der anderen, jede
# mit ihrem Gericht (Codex PR #139, P1).
SAY_ALLERGY_WHICH_FOR = (
    "Wogegen sind Sie bei {name} allergisch? Das gebe ich an die Küche weiter."
)
SAY_ALLERGY_NOTE = (
    "Ihren Hinweis zur Allergie gebe ich an die Küche weiter. Ob {name} frei davon "
    "ist, kann ich Ihnen nur sagen, wenn es bei uns hinterlegt ist."
)
SAY_UNDERSTOOD = (
    "Gern, {items}.",
    "Alles klar, {items}.",
    "Notiert: {items}.",
    "{items}, sehr gern.",
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


def say_understood(
    understood: list[tuple[str, MenuHit, Wish | None]], said: str
) -> str | None:
    """Wiederholt sofort, was eindeutig verstanden wurde - so, wie ein Mensch am
    Telefon es tut (Maxi, PR #127).

    Hat der Gast eine Nummer genannt, kommt nur die Nummer zurueck ("Nummer 9").
    Hat er das Gericht beschrieben ("Süß Sauer mit Ente"), kommt der Name der
    Karte mit Nummer ("Nummer 25a Ente süß-sauer"): nicht das Gesagte, sondern
    das, was das System daraus gemacht hat - ein falscher Treffer faellt so
    sofort auf. Ohne Menge, die kommt mit dem readback von draft_order.

    Die Einleitung wechselt, damit es nicht wie eine Ansage klingt. Gewaehlt
    wird aus dem Gesagten, nicht zufaellig: ein Replay sagt dasselbe (docs/08).
    `understood` sind match_type, Treffer und Wunsch; leer heisst kein Satz.

    Ein Wunsch wird mit wiederholt, damit der Gast hoert, dass er notiert ist:
    "Nummer 23, ohne Karotten", "Nummer 47 Ente knusprig mit Nudeln, 3 Euro
    Aufpreis" - der Aufpreis aus der Karte, nie vom Modell (T-4.10).
    """
    names = [
        _with_wish(
            f"Nummer {hit.number}"
            if match_type == "exact_number"
            else f"Nummer {hit.number} {hit.name}",
            wish,
        )
        for match_type, hit, wish in understood
    ]
    if not names:
        return None
    items = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} und {names[-1]}"
    lead = SAY_UNDERSTOOD[zlib.crc32(said.casefold().encode()) % len(SAY_UNDERSTOOD)]
    return lead.format(items=items)


def _with_wish(item: str, wish: Wish | None) -> str:
    # Unbekannt hat einen eigenen Satz, offen ist noch nicht entschieden, und die
    # Allergie steht im Satz danach (SAY_ALLERGY_NOTE) - hier nur das Gericht.
    if wish is None or wish.kind in ("unknown", "open", "allergy"):
        return item
    if wish.kind != "option":
        return f"{item}, {wish.text}"
    # Import hier: ordering importiert search (validation), oben waere es zirkulaer.
    from api.domain.ordering.readback import spoken_euro

    delta = wish.price_delta_cents or 0
    text = (
        f"{item}, {wish.note}, mit {wish.option}"
        if wish.note
        else f"{item} mit {wish.option}"
    )
    if delta > 0:
        return f"{text}, {spoken_euro(delta)} Aufpreis"
    if delta < 0:
        return f"{text}, {spoken_euro(-delta)} günstiger"
    return text


def position_parts(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    now: datetime | None = None,
    high: float | None = None,
    low: float | None = None,
) -> list[str]:
    """Die Positionen eines Satzes (`_position_parts`). Ein Teil, der mit einer
    eigenen Allergie beginnt ("und einer Sesamallergie"), ist keine neue
    Position, sondern gehoert zur davor (Codex PR #139, P1)."""
    parts = _position_parts(session, tenant_id, query, now, high, low)
    return _keep_allergy_clauses(query, parts)


def _keep_allergy_clauses(query: str, parts: list[str]) -> list[str]:
    if len(parts) <= 1 or not any(opens_with_allergy(p) for p in parts):
        return parts
    if opens_with_allergy(parts[0]):
        # Vorn im Satz: sie gehoert zum ersten Gericht danach, als Wunsch hinter
        # dem Gericht (Review PR #139).
        rest = _keep_allergy_clauses(query, parts[1:])
        if opens_with_allergy(rest[0]):
            return parts
        return [f"{rest[0]}, {parts[0]}", *rest[1:]]
    spans: list[list[int]] = []
    at = 0
    for part in parts:
        start = query.find(part, at)
        if start < 0:
            return parts
        spans.append([start, start + len(part)])
        at = start + len(part)
    merged = [spans[0]]
    for span, part in zip(spans[1:], parts[1:], strict=True):
        if opens_with_allergy(part):
            merged[-1][1] = span[1]
        else:
            merged.append(span)
    return [query[start:end] for start, end in merged]


def _position_parts(
    session: Session,
    tenant_id: uuid.UUID,
    query: str,
    now: datetime | None,
    high: float | None,
    low: float | None,
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
    # Laenger als der laengste Name oder Alias der Karte kann keine Spanne ein
    # Gericht sein. Ohne Grenze pruefte eine Aufzaehlung von 20 Gerichten rund
    # 190 Spannen (Codex PR #127, P2); so sind es hoechstens eine je Stueck, wenn
    # die Karte Namen mit einem "und" hat, und keine, wenn nicht.
    longest = _longest_dish(session, tenant_id)
    positions: list[str] = []
    # Je Gericht die Worte, mit denen es genannt wurde. "Pho Bo und Pho Bo" sind
    # zwei Portionen (Codex PR #127, P1); "Pho und Pho Bo" trifft dasselbe mit
    # anderen Worten - das kann eine Praezisierung sein, der Satz bleibt ganz.
    # Zaehlt fuer zusammengesetzte Gerichte genauso: "Fisch und Chips und
    # Backfisch mit Pommes" nennt dasselbe Gericht mit anderen Worten (Codex PR
    # #127, P1).
    said: dict[uuid.UUID, set[str]] = {}
    i = 0
    while i < len(pieces):
        for j in range(min(len(pieces), i + longest) - 1, i, -1):
            text = query[spans[i][0] : spans[j][1]]
            ids = _whole_dish(session, tenant_id, search, text, pieces[i : j + 1])
            if ids:
                if len(ids) == 1:
                    said.setdefault(next(iter(ids)), set()).add(normalize_query(text))
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


def _longest_dish(session: Session, tenant_id: uuid.UUID) -> int:
    """Die meisten Stuecke an den Trennern, die ein aktiver Name oder Alias hat."""
    names = session.scalars(select(MenuItem.name).where(*_active(tenant_id)))
    aliases = session.scalars(
        select(ItemAlias.alias)
        .join(MenuItem, ItemAlias.menu_item_id == MenuItem.id)
        .where(*_active(tenant_id))
    )
    return max(
        (separator_pieces(text) for text in [*names, *aliases]),
        default=1,
    )


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
) -> set[uuid.UUID]:
    """Ist der ganze Satz genau ein Gericht der Karte: Alias oder derselbe Name?
    Liefert die Gerichte, die er so trifft - mehrere bei einem doppelten Alias,
    keins, wenn er kein ganzes Gericht ist.
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
        return set()
    by_alias = _alias_items(session, tenant_id, query)
    if by_alias:
        return {item.id for item in by_alias}
    try:
        found = search(query)
    except (Ambiguous, NotFound):
        return set()
    said = normalize_query(query)
    return {
        hit.menu_item_id for hit in found.results if normalize_query(hit.name) == said
    }


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


def say_for_wish(hit: MenuHit, wish: Wish) -> str | None:
    """Der Satz, den ein Wunsch braucht: nicht angeboten (D8), die Frage nach der
    Gruppe einer Option, die in zweien steht, oder der Hinweis zur Allergie ohne
    Zusage (E14). Weglassen und Optionen brauchen keinen, sie werden mit
    wiederholt (`say_understood`)."""
    if wish.kind == "unknown":
        sentence = SAY_WISH_UNKNOWN.format(wish=wish.text, name=hit.name)
        # Das Weglassen dazu gilt trotzdem ("ohne Zwiebeln, dafür mit Pommes").
        return f"{sentence} Notiert: {wish.note}." if wish.note else sentence
    if wish.kind == "open" and wish.groups:
        return SAY_WISH_WHICH_GROUP.format(
            option=wish.option, groups=" oder bei ".join(wish.groups)
        )
    if wish.kind == "allergy":
        if wish.ingredient:
            return SAY_ALLERGY_NOTE.format(name=hit.name)
        return SAY_ALLERGY_WHICH
    return None


def allergy_question(names: list[str], named: bool | None = None) -> str | None:
    """Die Frage nach der ersten offenen Allergie. Bei mehreren nennt sie das
    Gericht, damit die Antwort zu ihm gehoert - auch die letzte der Reihe
    (`named`)."""
    if not names:
        return None
    if named is None:
        named = len(names) > 1
    return SAY_ALLERGY_WHICH_FOR.format(name=names[0]) if named else SAY_ALLERGY_WHICH


def _named_dish(session: Session, tenant_id: uuid.UUID, text: str) -> MenuItem | None:
    """Das eine aktive Gericht, das genau so heisst oder diesen Alias hat."""
    by_alias = _alias_items(session, tenant_id, text)
    if len(by_alias) == 1:
        return by_alias[0]
    said = normalize_query(text)
    # Die Namen der Karte einmal je Sitzung, nicht bei jeder Suche mit Wunsch neu
    # (Review PR #139); aktiv wird beim Treffer geprueft.
    names = session.info.setdefault("menu_names", {})
    if tenant_id not in names:
        names[tenant_id] = [
            (normalize_query(name), item_id)
            for item_id, name in session.execute(
                select(MenuItem.id, MenuItem.name).where(
                    MenuItem.tenant_id == tenant_id
                )
            )
        ]
    items = [session.get(MenuItem, i) for n, i in names[tenant_id] if n == said]
    named = [item for item in items if item is not None and item.active]
    return named[0] if len(named) == 1 else None


def _search_with_wish(
    session: Session,
    tenant_id: uuid.UUID,
    candidates: list[tuple[str, str, str]],
    max_results: int,
    now: datetime,
    high: float,
    low: float,
) -> SearchResult | None:
    """Das Gericht ohne den Wunsch suchen, den Wunsch dazu einordnen (T-4.10).

    None heisst: die normale Suche ueber den ganzen Satz entscheidet, weil das
    Gericht ohne Wunsch nichts findet. Gesucht wird einmal, mit dem Gericht der
    ersten Trennung. Gehoert deren Satzteil zum Namen ("Sommerrollen mit
    Garnelen"), gilt der naechste Wunsch fuer dasselbe Gericht ("... ohne
    Koriander") - ohne zweite Suche, die scheitern koennte (Codex und Review
    PR #139).
    """
    dish = candidates[0][0]
    try:
        found = search_menu(
            session, tenant_id, dish, max_results, now, high, low, split_check=False
        )
    except (Ambiguous, NotFound):
        return None
    if not found.results:
        return None
    hit = found.results[0]
    first = 0
    # Ist "Gericht + erster Satzteil" selbst ein Gericht ("Pizza mit Salami"
    # neben "Pizza" oder "Pizza mit Pilzen"), gilt dieses, und erst der naechste
    # Satzteil ist der Wunsch (Codex PR #139).
    named = _named_dish(session, tenant_id, f"{dish} {candidates[0][2]}")
    clear = found.match_type in CLEAR_MATCHES
    if named is not None and (not clear or named.id != hit.menu_item_id):
        found = _single(session, "alias", named, now)
        hit, first = found.results[0], 1
    elif not clear:
        # Gehoert der Satzteil zum Namen eines der Treffer, entscheidet der ganze
        # Satz (Codex PR #139).
        if any(names_it(h.name, candidates[0][2]) for h in found.results):
            return None
        return found.model_copy(update={"wish": open_wish(candidates[0][1])})
    for _, wish, segment in candidates[first:]:
        if names_it(hit.name, segment):
            continue
        classified = classify_wish(wish, hit.option_groups)
        say = found.say or say_for_wish(hit, classified)
        return found.model_copy(update={"wish": classified, "say": say})
    return found


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

    # Die Positionen des Satzes werden hoechstens einmal bestimmt, auch wenn ein
    # Wunsch darin steht (Review PR #139).
    parts: list[str] | None = None
    candidates = wish_candidates(query)
    if candidates and not has_number(candidates[0][1]):
        if split_check:
            parts = position_parts(
                session, tenant_id, query, now=now, high=high, low=low
            )
        if parts is None or len(parts) <= 1:
            found = _search_with_wish(
                session, tenant_id, candidates, max_results, now, high, low
            )
            if found is not None:
                return found
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
    if parts is None:
        parts = (
            position_parts(session, tenant_id, query, now=now, high=high, low=low)
            if split_check
            else [query]
        )
    if len(parts) > 1:
        raise Ambiguous("mehrere Positionen: " + " | ".join(parts), say=SAY_IN_TURN)

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
