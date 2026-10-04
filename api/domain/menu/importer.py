"""Import the menu from the CSV files described in docs/14 (T-4.2).

Two steps, so each can be tested on its own:

- `parse(files)` reads and checks, without a database. Errors prevent any
  import, warnings end up in the report (docs/14 §Prüfregeln).
- `apply(session, tenant_id, plan, ...)` brings the database in line. All in
  one transaction: an error halfway leaves no half menu behind.

The file is the truth for every dish it names: options are brought in line,
and so are aliases from an earlier import. Aliases from calls or added by hand
stay; they are knowledge that has grown. Dishes that are in the database but
not in the file are left untouched - ordered dishes cannot be deleted, and a
forgotten dish should not vanish silently. The report names them. With
`deactivate_missing` (register as the source, T-4.11) they become inactive,
never deleted.

Prices from the file replace an existing price only with
`apply_price_changes`; without it they appear as "Preisänderung" in the
report. Money only as integer cents, never via float (CLAUDE.md §8).
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
from api.domain.menu.items import option_key
from api.domain.menu.normalize import normalize_alias, normalize_query
from api.domain.menu.numberwords import (
    CARD_PARTS,
    CARD_SUFFIXES,
    canonical_card,
    reserved_prefix,
)
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
# "6,90", "6,9", "6" - digits and comma only (docs/14). A dot as decimal or
# thousands separator would be ambiguous and is rejected instead of guessed.
_EUR = re.compile(r"^(-?)(\d+)(?:,(\d{1,2}))?$")
# Card numbers search_menu resolves unambiguously: at most three digits not
# counting leading zeros (numberwords.MAX_VALUE = 999), optionally one letter
# a to g after them (numberwords.CARD_SUFFIXES) and a prefix of one or two
# letters in front ("s12", "sm1", T-4.12). Which prefixes exist is read by the
# search from the menu (numberwords.CardFormat). The lower-case number is
# checked: 23a and 23A would otherwise be two dishes the search can never tell
# apart.
_CARD_NUMBER = re.compile(
    r"(?:[a-z]{1,2})?0*\d{1,3}[" + "".join(sorted(CARD_SUFFIXES)) + "]?"
)
# More than this share of the active menu is only dropped with an explicit
# switch: a broken export must never switch off the menu (T-4.11).
MAX_DEACTIVATE_SHARE = 0.5


class MassDeactivationError(ValueError):
    """More than MAX_DEACTIVATE_SHARE of the active menu would be deactivated.
    The interface (CLI, later GUI) says how to allow it on purpose."""


def is_card_number(number: str) -> bool:
    """Does search_menu understand this number unambiguously? Pass it in lower
    case.

    A prefix numberwords already reads as a quantity, marker or spoken word
    ("x12", "st1", "nr5", "ja1") is not unambiguous for the search (review PR
    #155)."""
    if _CARD_NUMBER.fullmatch(number) is None:
        return False
    parts = CARD_PARTS.fullmatch(number)
    return parts is None or not reserved_prefix(parts.group(1))


@dataclass(frozen=True)
class ItemRow:
    number: str
    name: str
    category: str
    price_cents: int
    # None: the file does not have the column (register), the stored text
    # stays; "": text deleted (review T-4.11).
    description: str | None
    active: bool
    # Number in the register's spelling (T-4.11). None: the file does not have
    # the column, the stored value stays; "": value deleted.
    pos_code: str | None = None


@dataclass(frozen=True)
class OptionRow:
    group_name: str
    option_name: str
    price_delta_cents: int
    is_default: bool
    required: bool
    # Optional column (T-4.10): why the option costs more. None: the file does
    # not have the column, the maintained reason stays; "": reason deleted.
    price_reason: str | None = None


@dataclass(frozen=True)
class AllergenRow:
    # Empty means "no information", not "no allergens" (docs/03).
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
    # From items_not_in_file, those that were still active; only with deactivate_missing.
    items_deactivated: list[str] = field(default_factory=list)
    # (Nummer, alt, neu) in Cent
    price_changes: list[tuple[str, int, int]] = field(default_factory=list)
    price_changes_applied: bool = False
    # Hint at --deactivate-missing only for a register file without the switch.
    suggest_deactivate: bool = False
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
            or self.items_deactivated
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
                # A dry run rolls back: "übernommen" would be a lie (Codex PR #115).
                state = "würde übernommen"
            else:
                state = "übernommen"
            lines.append(
                f"Preisänderung {number}: {_eur(old)} -> {_eur(new)} ({state})"
            )
        if self.price_changes and not self.price_changes_applied:
            lines.append("Preise übernehmen mit --apply-price-changes.")
        if self.items_deactivated:
            state = "würden deaktiviert" if self.dry_run else "deaktiviert"
            lines.append(
                f"Nicht in der Datei, {state}: " + ", ".join(self.items_deactivated)
            )
        unchanged = [
            n for n in self.items_not_in_file if n not in self.items_deactivated
        ]
        if unchanged:
            lines.append(
                "In der Datenbank, aber nicht in der Datei (unverändert): "
                + ", ".join(unchanged)
            )
            if self.suggest_deactivate:
                lines.append(
                    "Kasse als Quelle: fehlende aktive Gerichte deaktivieren mit "
                    "--deactivate-missing."
                )
        lines += [f"Warnung: {w}" for w in self.warnings]
        if not self.changed and not self.price_changes:
            lines.append("Keine Änderung - die Karte ist schon auf diesem Stand.")
        return "\n".join(lines)


def format_eur(cents: int) -> str:
    """690 -> "6,90", so wie parse_eur es liest."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100},{cents % 100:02d}"


def _eur(cents: int) -> str:
    return f"{format_eur(cents)} €"


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
    # Excel likes to save UTF-8 with a BOM; without lstrip the first column
    # would be called "BOM character + number" and would seem to be missing.
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
    pos_codes: dict[str, int] = {}
    for line, row in _rows(plan, MENU_FILE, files[MENU_FILE]):
        where = f"{MENU_FILE} Zeile {line}"
        number = row["number"].lower()
        if not number:
            plan.errors.append(f"{where}: Nummer fehlt")
            continue
        parts = CARD_PARTS.fullmatch(number)
        if parts is not None and reserved_prefix(parts.group(1)):
            # Otherwise the message would say "up to two letters in front" and
            # the operator could not see what is wrong (code review PR #155).
            plan.errors.append(
                f'{where}: card number "{row["number"]}" has the prefix '
                f'"{parts.group(1)}", which already means something else on the '
                "phone (a quantity, a marker or a spoken word such as x, st, nr, "
                "no, ja, es)"
            )
            continue
        if not is_card_number(number):
            # Only what search_menu can resolve unambiguously. Otherwise
            # "Nummer 23h" would silently find the 23, or "A12" the 12 (Codex
            # PR #117, P1).
            plan.errors.append(
                f"{where}: Kartennummer „{row['number']}“ versteht die Suche nicht "
                "(erlaubt: bis 999, optional ein Buchstabe a bis g dahinter und bis "
                "zu zwei Buchstaben davor, z. B. 23, 23a, 25g, s12 oder sm1)"
            )
            continue
        # Duplicate by the form the search compares: "7" and "07" are one
        # number (Codex PR #117). The spelling from the file stays.
        key = canonical_card(number)
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
        pos_code = row.get("pos_code")
        if pos_code:
            if pos_code in pos_codes:
                problems.append(
                    f"pos_code {pos_code} doppelt (zuerst in Zeile {pos_codes[pos_code]})"
                )
            pos_codes.setdefault(pos_code, line)
        if problems:
            plan.errors.append(f"{where}: {'; '.join(problems)}")
            continue
        plan.items[number] = ItemRow(
            number=number,
            name=row["name"],
            category=row["category"],
            price_cents=price,
            description=row.get("description"),
            active=active,
            pos_code=pos_code,
        )

    _parse_options(plan, files.get(OPTIONS_FILE), spelled)
    _parse_allergens(plan, files.get(ALLERGENS_FILE), spelled)
    _parse_aliases(plan, files.get(ALIASES_FILE), spelled)
    return plan


def _known(plan: Plan, where: str, number: str, known: Mapping[str, str]) -> str | None:
    """Die Schreibweise aus menu_items.csv zu einer Nummer, auch als "7" für "07"."""
    spelled = known.get(canonical_card(number)) if number else None
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
        key = (number, option_key(row["group_name"]), option_key(row["option_name"]))
        if key in seen:
            problems.append("Option doppelt")
        if problems:
            plan.errors.append(f"{where}: {'; '.join(problems)}")
            continue
        seen.add(key)
        plan.options.setdefault(number, []).append(
            OptionRow(
                row["group_name"],
                row["option_name"],
                delta,
                is_default,
                required,
                row.get("price_reason"),
            )
        )

    for number, options in plan.options.items():
        groups: dict[str, list[OptionRow]] = {}
        spellings: dict[str, str] = {}
        for option in options:
            first = spellings.setdefault(
                option_key(option.group_name), option.group_name
            )
            if first != option.group_name:
                plan.errors.append(
                    f"{OPTIONS_FILE}: {number}/{option.group_name}: Gruppe „{first}“ "
                    "steht in zwei Schreibweisen, bitte eine verwenden"
                )
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
            # Never guess: an unknown letter does not become a confirmed allergen.
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
        # If nothing is left after the search normalisation ("bitte", "die",
        # "x", "23"), the alias would be stored but could never be found:
        # search_menu aborts with not_found before that. An error instead of a
        # warning - for the search this is the same as an empty alias (Codex
        # PR #117, P2).
        if not normalize_query(alias):
            plan.errors.append(
                f"{where}: Alias „{alias}“ besteht nur aus Füll- oder "
                "Zahlwörtern und wäre nie zu finden"
            )
            continue
        plan.aliases.setdefault(number, set()).add(alias)

    # The warning uses the same key search_menu compares with later: there the
    # filler word is dropped before the alias comparison. "Ente" and "die
    # Ente" on two dishes are therefore a collision, even though the two
    # strings differ - without this the import reports "no collision" and
    # every request in either spelling becomes ambiguous (Codex PR #117, P2).
    owners: dict[str, dict[str, set[str]]] = {}
    for number, aliases in plan.aliases.items():
        for alias in aliases:
            # Never empty: the check above has rejected such aliases.
            key = normalize_query(alias)
            spellings = owners.setdefault(key, {})
            spellings.setdefault(number, set()).add(alias)
    for key, by_number in sorted(owners.items()):
        if len(by_number) < 2:
            continue
        # Name the spellings only if they differ - otherwise the same word
        # would appear three times in the message.
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
    deactivate_missing: bool = False,
    allow_large_deactivation: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> Report:
    """Bring the database in line with the plan. With dry_run everything is sent
    to the database and rolled back at the end.

    deactivate_missing: dishes that are not in the file become inactive
    (register as master, docs/14). Without the switch they stay as they are.
    A file without dishes, or one that would drop more than half of the active
    menu, is refused unless allow_large_deactivation is set.
    """
    if not plan.ok:
        raise ValueError("Plan mit Fehlern wird nicht eingespielt")
    now = now or utcnow()
    report = Report(
        dry_run=dry_run,
        warnings=list(plan.warnings),
        price_changes_applied=apply_price_changes,
        suggest_deactivate=not deactivate_missing
        and any(r.pos_code for r in plan.items.values()),
    )
    rows = list(
        session.scalars(
            select(MenuItem).where(MenuItem.tenant_id == tenant_id).with_for_update()
        )
    )
    # In the form of the search (lower case, without leading zeros): a dish
    # imported earlier as "23A" or "07" is the same as "23a" or "7" and is
    # brought in line, not duplicated (Codex #117).
    # If both spellings already exist, a person decides which one applies -
    # silently hiding one would mean never finding the other again.
    by_key: dict[str, list[MenuItem]] = {}
    for row in rows:
        by_key.setdefault(canonical_card(row.number), []).append(row)
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
    in_plan = {canonical_card(n) for n in plan.items}
    missing = [item for key, item in existing.items() if key not in in_plan]
    report.items_not_in_file = sorted(item.number for item in missing)
    # Dishes outside the file stay, deactivated ones too: their register
    # number must not go to a second dish, or the handover to the register
    # becomes ambiguous (Codex PR #149).
    wanted_codes = {r.pos_code: n for n, r in plan.items.items() if r.pos_code}
    taken = sorted(
        f"{item.pos_code} ({item.number}, in der Datei bei {wanted_codes[item.pos_code]})"
        for item in missing
        if item.pos_code in wanted_codes
    )
    if taken:
        session.rollback()
        raise ValueError(
            "pos_code gehört schon einem Gericht, das nicht in der Datei steht: "
            + "; ".join(taken)
            + ". Kassennummer dort erst leeren oder das Gericht mit in die Datei."
        )
    if deactivate_missing:
        active_missing = [item for item in missing if item.active]
        active_total = sum(item.active for item in existing.values())
        if not plan.items:
            session.rollback()
            raise ValueError(
                "Datei enthält keine Gerichte - fehlende Gerichte werden nicht "
                "deaktiviert. Export und Umwandler prüfen."
            )
        if (
            active_missing
            and len(active_missing) > MAX_DEACTIVATE_SHARE * active_total
            and not allow_large_deactivation
        ):
            session.rollback()
            raise MassDeactivationError(
                f"{len(active_missing)} von {active_total} aktiven Gerichten würden "
                "deaktiviert, mehr als die Hälfte der Karte. Export prüfen."
            )
        for item in missing:
            if item.active:
                item.active = False
                report.items_deactivated.append(item.number)
        report.items_deactivated.sort()

    items: dict[str, MenuItem] = {}
    for number, row in plan.items.items():
        item = existing.get(canonical_card(number))
        if item is None:
            item = MenuItem(
                tenant_id=tenant_id,
                number=number,
                name=row.name,
                category=row.category,
                price_cents=row.price_cents,
                description=row.description or None,
                active=row.active,
                pos_code=row.pos_code or None,
            )
            session.add(item)
            report.items_new.append(number)
        else:
            fields = {
                "number": row.number,
                "name": row.name,
                "category": row.category,
                "active": row.active,
            }
            if row.description is not None:
                fields["description"] = row.description or None
            if row.pos_code is not None:
                fields["pos_code"] = row.pos_code or None
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
        # Send everything to the database before rolling back. The script's
        # session has autoflush off: options, allergens and aliases would
        # never reach the database, and a row it rejects would pass the dry
        # run and fail only in the real import.
        session.flush()
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
                    "items_deactivated": report.items_deactivated,
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
                    price_reason=row.price_reason or None,
                )
            )
            report.options_added += 1
            continue
        # Old file without the column: the reason stays (review PR #139).
        reason = (
            option.price_reason
            if row.price_reason is None
            else row.price_reason or None
        )
        values = (row.price_delta_cents, row.is_default, row.required, reason)
        current_values = (
            option.price_delta_cents,
            option.is_default,
            option.required,
            option.price_reason,
        )
        if current_values != values:
            (
                option.price_delta_cents,
                option.is_default,
                option.required,
                option.price_reason,
            ) = values
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
        # Unchanged evidence keeps its original time.
        return
    # The row in the file is new evidence for the whole dish: codes that are
    # kept also carry the checker and time of this import afterwards, otherwise
    # the database would contradict the file it comes from (Codex PR #115).
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
        # Only what an earlier import brought; calls and manual work stay.
        if row.source == SOURCE_IMPORT and alias not in wanted:
            session.delete(row)
            report.aliases_removed += 1
    for alias in sorted(wanted - set(current)):
        session.add(ItemAlias(menu_item_id=item.id, alias=alias, source=SOURCE_IMPORT))
        report.aliases_added += 1
