"""Speisekarte aus den CSV-Dateien nach docs/14 einspielen (T-4.2).

Zwei Schritte, damit sich jeder allein testen lässt:

- `parse(files)` liest und prüft, ohne Datenbank. Fehler verhindern jeden
  Import, Warnungen landen im Bericht (docs/14 §Prüfregeln).
- `apply(session, tenant_id, plan, ...)` gleicht die Datenbank an. Alles in
  einer Transaktion: ein Fehler mittendrin hinterlässt keine halbe Karte.

Die Datei ist die Wahrheit für jedes Gericht, das sie nennt: Optionen werden
angeglichen, Aliase aus einem früheren Import ebenso. Aliase aus Anrufen oder
von Hand bleiben, sie sind gewachsenes Wissen. Gerichte, die in der Datenbank
stehen, aber nicht in der Datei, bleiben unangetastet - bestellte Gerichte
lassen sich nicht löschen, und ein vergessenes Gericht soll nicht still
verschwinden. Der Bericht nennt sie.

Preise aus der Datei ersetzen einen bestehenden Preis nur mit
`apply_price_changes`; ohne stehen sie als "Preisänderung" im Bericht.
Geld nur als Cent-Ganzzahl, nie über float (CLAUDE.md §8).
"""

import csv
import io
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.core.time import utcnow
from api.domain.menu.normalize import normalize_alias, normalize_query
from api.models import AuditLog, ItemAlias, ItemAllergen, ItemOption, MenuItem
from api.models.menu import ALLERGEN_CODES

MENU_FILE = "menu_items.csv"
OPTIONS_FILE = "item_options.csv"
ALLERGENS_FILE = "item_allergens.csv"
ALIASES_FILE = "item_aliases.csv"
FILES = (MENU_FILE, OPTIONS_FILE, ALLERGENS_FILE, ALIASES_FILE)

ACTOR_IMPORT = "import"
ACTION_IMPORTED = "menu.imported"
SOURCE_IMPORT = "import"

_COLUMNS = {
    MENU_FILE: ("number", "name", "category", "price_eur"),
    OPTIONS_FILE: (
        "number",
        "group_name",
        "option_name",
        "price_delta_eur",
        "is_default",
        "required",
    ),
    ALLERGENS_FILE: ("number", "allergen_codes", "confirmed_by"),
    ALIASES_FILE: ("number", "alias"),
}
# "6,90", "6,9", "6" - nur Ziffern und Komma (docs/14). Punkt als Dezimal- oder
# Tausendertrenner wäre mehrdeutig und wird abgelehnt statt geraten.
_EUR = re.compile(r"^(-?)(\d+)(?:,(\d{1,2}))?$")
# Kartennummern, die search_menu eindeutig auflöst: höchstens drei Stellen ohne
# führende Nullen (numberwords.MAX_VALUE = 999), optional ein Buchstabe a bis f
# (numberwords._SUFFIXES). Geprüft wird die klein geschriebene Nummer: 23a und
# 23A wären sonst zwei Gerichte, die die Suche nie auseinanderhält.
_CARD_NUMBER = re.compile(r"0*\d{1,3}[a-f]?")


@dataclass(frozen=True)
class ItemRow:
    number: str
    name: str
    category: str
    price_cents: int
    description: str | None
    active: bool


@dataclass(frozen=True)
class OptionRow:
    group_name: str
    option_name: str
    price_delta_cents: int
    is_default: bool
    required: bool


@dataclass(frozen=True)
class AllergenRow:
    # Leer heisst "keine Auskunft", nicht "keine Allergene" (docs/03).
    codes: tuple[str, ...]
    confirmed_by: str | None


@dataclass
class Plan:
    items: dict[str, ItemRow] = field(default_factory=dict)
    options: dict[str, list[OptionRow]] = field(default_factory=dict)
    allergens: dict[str, AllergenRow] = field(default_factory=dict)
    aliases: dict[str, set[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Report:
    dry_run: bool
    items_new: list[str] = field(default_factory=list)
    items_updated: list[str] = field(default_factory=list)
    items_not_in_file: list[str] = field(default_factory=list)
    # (Nummer, alt, neu) in Cent
    price_changes: list[tuple[str, int, int]] = field(default_factory=list)
    price_changes_applied: bool = False
    options_added: int = 0
    options_removed: int = 0
    options_changed: int = 0
    allergens_changed: list[str] = field(default_factory=list)
    aliases_added: int = 0
    aliases_removed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.items_new
            or self.items_updated
            or (self.price_changes and self.price_changes_applied)
            or self.options_added
            or self.options_removed
            or self.options_changed
            or self.allergens_changed
            or self.aliases_added
            or self.aliases_removed
        )

    def as_text(self) -> str:
        head = (
            "Probelauf, nichts gespeichert." if self.dry_run else "Import gespeichert."
        )
        lines = [
            head,
            f"Gerichte neu: {len(self.items_new)}, geändert: {len(self.items_updated)}",
            f"Optionen neu: {self.options_added}, geändert: {self.options_changed}, "
            f"entfernt: {self.options_removed}",
            f"Aliase neu: {self.aliases_added}, entfernt: {self.aliases_removed}",
        ]
        if self.allergens_changed:
            lines.append("Allergene geändert bei: " + ", ".join(self.allergens_changed))
        for number, old, new in self.price_changes:
            if not self.price_changes_applied:
                state = "NICHT übernommen"
            elif self.dry_run:
                # Probelauf rollt zurück: "übernommen" wäre gelogen (Codex PR #115).
                state = "würde übernommen"
            else:
                state = "übernommen"
            lines.append(
                f"Preisänderung {number}: {_eur(old)} -> {_eur(new)} ({state})"
            )
        if self.price_changes and not self.price_changes_applied:
            lines.append("Preise übernehmen mit --apply-price-changes.")
        if self.items_not_in_file:
            lines.append(
                "In der Datenbank, aber nicht in der Datei (unverändert): "
                + ", ".join(self.items_not_in_file)
            )
        lines += [f"Warnung: {w}" for w in self.warnings]
        if not self.changed and not self.price_changes:
            lines.append("Keine Änderung - die Karte ist schon auf diesem Stand.")
        return "\n".join(lines)


def _eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100},{cents % 100:02d} €"


def parse_eur(value: str) -> int | None:
    """ "6,90" -> 690. None, wenn nicht eindeutig lesbar. Ganzzahlig, nie float."""
    match = _EUR.match(value.strip())
    if match is None:
        return None
    sign, euros, cents = match.groups()
    total = int(euros) * 100 + int((cents or "0").ljust(2, "0"))
    return -total if sign else total


def _bool(value: str, default: bool | None = None) -> bool | None:
    value = value.strip().lower()
    if value == "" and default is not None:
        return default
    return {"ja": True, "nein": False}.get(value)


def _rows(plan: Plan, name: str, text: str | None) -> list[tuple[int, dict[str, str]]]:
    """Zeilen mit ihrer Zeilennummer in der Datei (Kopfzeile = 1)."""
    if text is None:
        return []
    # Excel speichert UTF-8 gern mit BOM; ohne lstrip hiesse die erste Spalte
    # "BOM-Zeichen + number" und fehlte scheinbar.
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    missing = [c for c in _COLUMNS[name] if c not in (reader.fieldnames or [])]
    if missing:
        plan.errors.append(f"{name}: Spalte fehlt: {', '.join(missing)}")
        return []
    rows = []
    for index, raw in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        rows.append((index, row))
    return rows


def parse(files: Mapping[str, str | None]) -> Plan:
    """Die vier Dateien lesen und prüfen. Ohne menu_items.csv kein Import."""
    plan = Plan()
    if files.get(MENU_FILE) is None:
        plan.errors.append(f"{MENU_FILE} fehlt")
        return plan

    first_line: dict[str, int] = {}
    spelled: dict[str, str] = {}
    for line, row in _rows(plan, MENU_FILE, files[MENU_FILE]):
        where = f"{MENU_FILE} Zeile {line}"
        number = row["number"].lower()
        if not number:
            plan.errors.append(f"{where}: Nummer fehlt")
            continue
        if not _CARD_NUMBER.fullmatch(number):
            # Nur was search_menu eindeutig auflösen kann. Sonst würde "Nummer
            # 23g" still die 23 finden oder "A12" die 12 (Codex PR #117, P1).
            plan.errors.append(
                f"{where}: Kartennummer „{row['number']}“ versteht die Suche nicht "
                "(erlaubt: bis 999, optional ein Buchstabe a bis f, z. B. 23 oder 23a)"
            )
            continue
        # Dublette nach der Form, in der die Suche vergleicht: "7" und "07" sind
        # eine Nummer (Codex PR #117). Die Schreibweise aus der Datei bleibt.
        key = _canonical(number)
        if key in first_line:
            plan.errors.append(
                f"{where}: Nummer {number} doppelt (zuerst in Zeile {first_line[key]})"
            )
            continue
        first_line[key] = line
        spelled[key] = number
        price = parse_eur(row["price_eur"])
        active = _bool(row.get("active", ""), default=True)
        problems = []
        if not row["name"]:
            problems.append("Name fehlt")
        if not row["category"]:
            problems.append("Kategorie fehlt")
        if price is None or price < 0:
            problems.append(
                f"Preis „{row['price_eur']}“ nicht lesbar (nur Ziffern und Komma)"
            )
        if active is None:
            problems.append(f"active „{row['active']}“ ist weder ja noch nein")
        if problems:
            plan.errors.append(f"{where}: {'; '.join(problems)}")
            continue
        plan.items[number] = ItemRow(
            number=number,
            name=row["name"],
            category=row["category"],
            price_cents=price,
            description=row.get("description") or None,
            active=active,
        )

    _parse_options(plan, files.get(OPTIONS_FILE), spelled)
    _parse_allergens(plan, files.get(ALLERGENS_FILE), spelled)
    _parse_aliases(plan, files.get(ALIASES_FILE), spelled)
    return plan


def _canonical(number: str) -> str:
    """Kartennummer so, wie search_menu sie vergleicht: klein, ohne führende Nullen."""
    return number.lower().lstrip("0") or "0"


def _known(plan: Plan, where: str, number: str, known: Mapping[str, str]) -> str | None:
    """Die Schreibweise aus menu_items.csv zu einer Nummer, auch als "7" für "07"."""
    spelled = known.get(_canonical(number)) if number else None
    if spelled is not None:
        return spelled
    plan.errors.append(
        f"{where}: Nummer {number or '(leer)'} gibt es nicht in {MENU_FILE}"
    )
    return None


def _parse_options(plan: Plan, text: str | None, known: Mapping[str, str]) -> None:
    seen: set[tuple[str, str, str]] = set()
    for line, row in _rows(plan, OPTIONS_FILE, text):
        where = f"{OPTIONS_FILE} Zeile {line}"
        number = _known(plan, where, row["number"].lower(), known)
        if number is None:
            continue
        delta = parse_eur(row["price_delta_eur"] or "0")
        is_default = _bool(row["is_default"], default=False)
        required = _bool(row["required"], default=False)
        problems = []
        if not row["group_name"] or not row["option_name"]:
            problems.append("Gruppe oder Option fehlt")
        if delta is None:
            problems.append(f"Preisdifferenz „{row['price_delta_eur']}“ nicht lesbar")
        if is_default is None or required is None:
            problems.append("is_default und required nur ja oder nein")
        key = (number, row["group_name"], row["option_name"])
        if key in seen:
            problems.append("Option doppelt")
        if problems:
            plan.errors.append(f"{where}: {'; '.join(problems)}")
            continue
        seen.add(key)
        plan.options.setdefault(number, []).append(
            OptionRow(
                row["group_name"], row["option_name"], delta, is_default, required
            )
        )

    for number, options in plan.options.items():
        groups: dict[str, list[OptionRow]] = {}
        for option in options:
            groups.setdefault(option.group_name, []).append(option)
        for group, members in groups.items():
            flags = {m.required for m in members}
            if len(flags) > 1:
                plan.errors.append(
                    f"{OPTIONS_FILE}: {number}/{group}: required ist in der Gruppe nicht einheitlich"
                )
                continue
            defaults = sum(m.is_default for m in members)
            if flags == {True} and defaults != 1:
                plan.errors.append(
                    f"{OPTIONS_FILE}: {number}/{group}: Pflichtgruppe braucht genau einen "
                    f"Default, hat {defaults}"
                )


def _parse_allergens(plan: Plan, text: str | None, known: Mapping[str, str]) -> None:
    for line, row in _rows(plan, ALLERGENS_FILE, text):
        where = f"{ALLERGENS_FILE} Zeile {line}"
        number = _known(plan, where, row["number"].lower(), known)
        if number is None:
            continue
        if number in plan.allergens:
            plan.errors.append(f"{where}: Nummer {number} doppelt")
            continue
        raw = [c.strip().upper() for c in row["allergen_codes"].split(",") if c.strip()]
        unknown = [c for c in raw if c not in ALLERGEN_CODES]
        if unknown:
            # Nie raten: ein unbekannter Buchstabe wird kein bestätigtes Allergen.
            plan.errors.append(
                f"{where}: unbekannter LMIV-Code {', '.join(unknown)} "
                f"(erlaubt: {', '.join(ALLERGEN_CODES)})"
            )
            continue
        if raw and not row["confirmed_by"]:
            plan.errors.append(f"{where}: confirmed_by fehlt - wer hat es geprüft?")
            continue
        plan.allergens[number] = AllergenRow(
            codes=tuple(sorted(set(raw))), confirmed_by=row["confirmed_by"] or None
        )


def _parse_aliases(plan: Plan, text: str | None, known: Mapping[str, str]) -> None:
    for line, row in _rows(plan, ALIASES_FILE, text):
        where = f"{ALIASES_FILE} Zeile {line}"
        number = _known(plan, where, row["number"].lower(), known)
        if number is None:
            continue
        alias = normalize_alias(row["alias"])
        if not alias:
            plan.errors.append(f"{where}: Alias leer")
            continue
        plan.aliases.setdefault(number, set()).add(alias)

    # Gewarnt wird nach derselben Kennung, mit der search_menu spaeter vergleicht:
    # dort faellt vor dem Alias-Vergleich das Fuellwort weg. "Ente" und "die
    # Ente" an zwei Gerichten sind deshalb eine Kollision, auch wenn die beiden
    # Zeichenketten verschieden sind - ohne das meldet der Import "keine
    # Kollision" und jede Anfrage nach beiden Schreibweisen wird ambiguous
    # (Codex PR #117, P2).
    owners: dict[str, dict[str, set[str]]] = {}
    for number, aliases in plan.aliases.items():
        for alias in aliases:
            key = normalize_query(alias) or alias
            spellings = owners.setdefault(key, {})
            spellings.setdefault(number, set()).add(alias)
    for key, by_number in sorted(owners.items()):
        if len(by_number) < 2:
            continue
        # Die Schreibweisen nur nennen, wenn sie sich unterscheiden - sonst
        # stuende dreimal dasselbe Wort in der Meldung.
        abweichend = any(s != key for ss in by_number.values() for s in ss)
        genannt = ", ".join(
            f"{number} ({', '.join(sorted(spellings))})" if abweichend else number
            for number, spellings in sorted(by_number.items())
        )
        plan.warnings.append(f"Alias „{key}“ führt zu mehreren Gerichten: {genannt}")
    without = sorted(n for n in plan.items if n not in plan.aliases)
    if without:
        plan.warnings.append("Gericht ohne Alias: " + ", ".join(without))


def apply(
    session: Session,
    tenant_id: uuid.UUID,
    plan: Plan,
    *,
    apply_price_changes: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Report:
    """Datenbank an den Plan angleichen. Bei dry_run wird am Ende zurückgerollt."""
    if not plan.ok:
        raise ValueError("Plan mit Fehlern wird nicht eingespielt")
    now = now or utcnow()
    report = Report(
        dry_run=dry_run,
        warnings=list(plan.warnings),
        price_changes_applied=apply_price_changes,
    )
    rows = list(
        session.scalars(
            select(MenuItem).where(MenuItem.tenant_id == tenant_id).with_for_update()
        )
    )
    # In der Form der Suche (klein, ohne führende Nullen): ein früher als "23A"
    # oder "07" importiertes Gericht ist dasselbe wie "23a" oder "7" und wird
    # angeglichen, nicht verdoppelt (Codex #117).
    # Stehen beide Schreibweisen schon im Bestand, entscheidet ein Mensch, welche
    # gilt - still eine zu verdecken hiesse, die andere nie wieder zu finden.
    by_key: dict[str, list[MenuItem]] = {}
    for row in rows:
        by_key.setdefault(_canonical(row.number), []).append(row)
    clashes = [
        sorted(r.number for r in group) for group in by_key.values() if len(group) > 1
    ]
    if clashes:
        session.rollback()
        listed = "; ".join(" und ".join(c) for c in sorted(clashes))
        raise ValueError(
            f"Kartennummer doppelt im Bestand (nur Schreibweise verschieden): "
            f"{listed}. Eine davon von Hand zusammenführen, dann erneut importieren."
        )
    existing = {key: group[0] for key, group in by_key.items()}
    in_plan = {_canonical(n) for n in plan.items}
    report.items_not_in_file = sorted(
        item.number for key, item in existing.items() if key not in in_plan
    )

    items: dict[str, MenuItem] = {}
    for number, row in plan.items.items():
        item = existing.get(_canonical(number))
        if item is None:
            item = MenuItem(
                tenant_id=tenant_id,
                number=number,
                name=row.name,
                category=row.category,
                price_cents=row.price_cents,
                description=row.description,
                active=row.active,
            )
            session.add(item)
            report.items_new.append(number)
        else:
            fields = {
                "number": row.number,
                "name": row.name,
                "category": row.category,
                "description": row.description,
                "active": row.active,
            }
            changed = [k for k, v in fields.items() if getattr(item, k) != v]
            for key in changed:
                setattr(item, key, fields[key])
            if changed:
                report.items_updated.append(number)
            if item.price_cents != row.price_cents:
                report.price_changes.append((number, item.price_cents, row.price_cents))
                if apply_price_changes:
                    item.price_cents = row.price_cents
        items[number] = item
    session.flush()

    for number, item in items.items():
        _sync_options(session, item, plan.options.get(number, []), report)
        if number in plan.allergens:
            _sync_allergens(session, item, plan.allergens[number], now, report)
        _sync_aliases(session, item, plan.aliases.get(number, set()), report)

    if dry_run:
        session.rollback()
        return report
    if report.changed:
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=ACTOR_IMPORT,
                action=ACTION_IMPORTED,
                entity="menu",
                payload={
                    "items_new": len(report.items_new),
                    "items_updated": len(report.items_updated),
                    "price_changes": [
                        {"number": n, "from": old, "to": new}
                        for n, old, new in report.price_changes
                    ],
                    "price_changes_applied": apply_price_changes,
                    "allergens_changed": report.allergens_changed,
                },
            )
        )
    session.commit()
    return report


def _sync_options(
    session: Session, item: MenuItem, wanted: list[OptionRow], report: Report
) -> None:
    current = {
        (o.group_name, o.option_name): o
        for o in session.scalars(
            select(ItemOption).where(ItemOption.menu_item_id == item.id)
        )
    }
    desired = {(o.group_name, o.option_name): o for o in wanted}
    for key, option in current.items():
        if key not in desired:
            session.delete(option)
            report.options_removed += 1
    for key, row in desired.items():
        option = current.get(key)
        if option is None:
            session.add(
                ItemOption(
                    menu_item_id=item.id,
                    group_name=row.group_name,
                    option_name=row.option_name,
                    price_delta_cents=row.price_delta_cents,
                    is_default=row.is_default,
                    required=row.required,
                )
            )
            report.options_added += 1
            continue
        values = (row.price_delta_cents, row.is_default, row.required)
        if (option.price_delta_cents, option.is_default, option.required) != values:
            option.price_delta_cents, option.is_default, option.required = values
            report.options_changed += 1


def _sync_allergens(
    session: Session, item: MenuItem, row: AllergenRow, now: datetime, report: Report
) -> None:
    current = {
        a.allergen_code: a
        for a in session.scalars(
            select(ItemAllergen).where(ItemAllergen.menu_item_id == item.id)
        )
    }
    desired = set(row.codes)
    same_codes = set(current) == desired
    same_confirmer = all(a.confirmed_by == row.confirmed_by for a in current.values())
    if same_codes and same_confirmer:
        # Unveränderter Nachweis behält seinen ursprünglichen Zeitpunkt.
        return
    # Die Zeile der Datei ist ein neuer Nachweis für das ganze Gericht: auch
    # behaltene Codes tragen danach Prüfer und Zeitpunkt dieses Imports, sonst
    # widerspräche die Datenbank der Datei, aus der sie stammt (Codex PR #115).
    for code, allergen in current.items():
        if code not in desired:
            session.delete(allergen)
        else:
            allergen.confirmed_by = row.confirmed_by
            allergen.confirmed_at = now
    for code in sorted(desired - set(current)):
        session.add(
            ItemAllergen(
                menu_item_id=item.id,
                allergen_code=code,
                confirmed_by=row.confirmed_by,
                confirmed_at=now,
            )
        )
    report.allergens_changed.append(item.number)


def _sync_aliases(
    session: Session, item: MenuItem, wanted: set[str], report: Report
) -> None:
    current = {
        a.alias: a
        for a in session.scalars(
            select(ItemAlias).where(ItemAlias.menu_item_id == item.id)
        )
    }
    for alias, row in current.items():
        # Nur, was ein früherer Import gebracht hat; Anrufe und Handarbeit bleiben.
        if row.source == SOURCE_IMPORT and alias not in wanted:
            session.delete(row)
            report.aliases_removed += 1
    for alias in sorted(wanted - set(current)):
        session.add(ItemAlias(menu_item_id=item.id, alias=alias, source=SOURCE_IMPORT))
        report.aliases_added += 1
