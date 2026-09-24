#!/usr/bin/env python3
"""Taegliche Pflege des GitHub-Projects: Abweichungen melden, nichts nebenbei aendern.

Zweck : Das Board ist eine Ableitung. Waehrend der Arbeit fasst es niemand an;
        einmal taeglich prueft dieses Skript, ob es noch zu Issues und
        docs/07_WORKPACKAGES.md passt, und meldet die Abweichungen.
Aufruf: python scripts/project_report.py [--fix] [--archive]
        Ohne Schalter wird nur gelesen und berichtet (Trockenlauf).
        --fix      setzt Abgeschlossenes auf Done - der Lauf, den die Action taeglich macht
        --archive  archiviert Done-Eintraege nach 14 Tagen, nur von Hand
Umgeb.: PROJECT_TOKEN   Token mit Projects-Recht (Lesen; fuer --fix/--archive Schreiben)
        PROJECT_OWNER   Kontoname, dem das Project gehoert
        PROJECT_NUMBER  Nummer des Projects aus seiner URL
        PROJECT_REPO    owner/name des Repos fuer den Abgleich offener Issues und
                        Pull Requests; in einer Action faellt es auf GITHUB_REPOSITORY
                        zurueck. Fehlt beides, entfaellt dieser eine Abgleich - laut.
Abhaeng.: nur Standardbibliothek.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

GRAPHQL_URL = "https://api.github.com/graphql"

# Ab wann ein Eintrag auffaellt. Bewusst grosszuegig: der Bericht soll auf echte
# Unordnung zeigen, nicht auf jeden Vorgang, der zwei Tage liegen bleibt.
DONE_ARCHIVE_AFTER_DAYS = 14
IN_PROGRESS_STALE_AFTER_DAYS = 7

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      title
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isArchived
          updatedAt
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                field { ... on ProjectV2IterationField { name } }
              }
            }
          }
          content {
            ... on Issue { number title state url }
            ... on PullRequest { number title state url }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""

# Ein Platzhalter statt zwei fast gleicher Abfragen: issues und pullRequests haben
# dieselbe Form. Getrennt blaettern, weil ein gemeinsamer Cursor die fertige Seite
# der kuerzeren Liste erneut holen und Eintraege doppeln wuerde.
REPO_OPEN_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    CONNECTION(states: OPEN, first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url updatedAt }
    }
  }
}
"""

STATUS_FIELD_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}
"""

SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) { projectV2Item { id } }
}
"""

ARCHIVE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!) {
  archiveProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
    item { id }
  }
}
"""


# GitHub schreibt die Optionen so, wie sie angelegt wurden: "In progress" aus der
# Vorlage, "In Progress" von Hand. Ein Vergleich auf den exakten Text laesst den
# Bericht still danebenliegen, darum wird normalisiert statt gleichgesetzt.
IN_PROGRESS_NAMES = frozenset({"in progress", "in arbeit"})
DONE_NAMES = frozenset({"done", "fertig", "erledigt"})


def _normalized(name: str | None) -> str:
    return (name or "").strip().casefold()


class ProjectError(RuntimeError):
    """Das Project liess sich nicht lesen oder aendern."""


@dataclass
class Item:
    """Ein Eintrag auf dem Board, auf die Felder reduziert, die der Bericht braucht."""

    node_id: str
    title: str
    updated_at: datetime
    number: int | None = None
    state: str | None = None
    url: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    is_archived: bool = False

    @property
    def status(self) -> str | None:
        return self.fields.get("Status")

    @property
    def is_in_progress(self) -> bool:
        return _normalized(self.status) in IN_PROGRESS_NAMES

    @property
    def is_done(self) -> bool:
        return _normalized(self.status) in DONE_NAMES

    @property
    def is_finished(self) -> bool:
        """Abgeschlossen im Sinne von GitHub. Ein Pull Request landet auf MERGED, nicht auf CLOSED."""
        return self.state in ("CLOSED", "MERGED")

    @property
    def label(self) -> str:
        return (
            f"#{self.number} {self.title}" if self.number else f"(Entwurf) {self.title}"
        )

    def age_days(self, now: datetime) -> int:
        return (now - self.updated_at).days


def graphql(query: str, variables: dict, token: str) -> dict:
    """Eine GraphQL-Anfrage stellen. Fehler kommen als ProjectError mit Klartext zurueck."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "maex-voice-agent-project-report",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise ProjectError(f"GitHub antwortete mit {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProjectError(f"GitHub nicht erreichbar: {exc.reason}") from exc

    if body.get("errors"):
        messages = "; ".join(error.get("message", "?") for error in body["errors"])
        raise ProjectError(f"GraphQL-Fehler: {messages}")
    return body["data"]


def parse_item(node: dict) -> Item | None:
    """Einen GraphQL-Knoten in ein Item uebersetzen.

    Archivierte Eintraege werden markiert, nicht verworfen: der Abgleich mit dem Repo
    braucht ihre URL. Sonst gilt ein wieder geoeffnetes Issue als fehlend, und
    /project wuerde es neu hinzufuegen, statt den archivierten Eintrag zurueckzuholen.
    """
    fields: dict[str, str] = {}
    for value in node.get("fieldValues", {}).get("nodes", []):
        field_name = (value.get("field") or {}).get("name")
        if not field_name:
            continue
        # Single-Select liefert "name", Iterationen liefern "title".
        fields[field_name] = value.get("name") or value.get("title") or ""

    content = node.get("content") or {}
    return Item(
        node_id=node["id"],
        title=content.get("title", "(ohne Titel)"),
        updated_at=datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00")),
        number=content.get("number"),
        state=content.get("state"),
        url=content.get("url"),
        fields=fields,
        is_archived=bool(node.get("isArchived")),
    )


def fetch_items(owner: str, number: int, token: str) -> tuple[str, str, list[Item]]:
    """Alle Eintraege des Projects holen, archivierte markiert, seitenweise."""
    items: list[Item] = []
    cursor: str | None = None
    project_id = ""
    project_title = ""

    while True:
        data = graphql(
            ITEMS_QUERY, {"owner": owner, "number": number, "cursor": cursor}, token
        )
        project = (data.get("user") or {}).get("projectV2")
        if project is None:
            raise ProjectError(
                f"Kein Project {number} beim Konto {owner} gefunden. "
                "Nummer, Kontoname und die Project-Berechtigung des Tokens pruefen."
            )
        project_id = project["id"]
        project_title = project["title"]

        page = project["items"]
        items.extend(item for node in page["nodes"] if (item := parse_item(node)))

        if not page["pageInfo"]["hasNextPage"]:
            return project_id, project_title, items
        cursor = page["pageInfo"]["endCursor"]


def fetch_open_in_repo(repo: str, token: str) -> list[Item]:
    """Alle offenen Issues und Pull Requests des Repos, als Items ohne Board-Felder."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ProjectError(f"PROJECT_REPO muss owner/name sein, nicht {repo!r}")

    found: list[Item] = []
    for connection in ("issues", "pullRequests"):
        query = REPO_OPEN_QUERY.replace("CONNECTION", connection)
        cursor: str | None = None
        while True:
            data = graphql(
                query, {"owner": owner, "name": name, "cursor": cursor}, token
            )
            repository = data.get("repository")
            if repository is None:
                raise ProjectError(f"Repo {repo} nicht gefunden oder nicht lesbar.")
            page = repository[connection]
            found.extend(
                Item(
                    node_id="",
                    title=node["title"],
                    updated_at=datetime.fromisoformat(
                        node["updatedAt"].replace("Z", "+00:00")
                    ),
                    number=node["number"],
                    state="OPEN",
                    url=node["url"],
                )
                for node in page["nodes"]
            )
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return found


def find_missing(items: list[Item], open_in_repo: list[Item]) -> list[Item]:
    """Offen im Repo, aber nicht auf dem Board. Verglichen wird die URL: Issue- und
    PR-Nummern teilen sich einen Zaehler, aber das Board kann Eintraege aus anderen
    Repos tragen, und dort waere #12 etwas anderes."""
    on_board = {item.url for item in items if item.url}
    return [ref for ref in open_in_repo if ref.url not in on_board]


def find_issues(
    items: list[Item], now: datetime, open_in_repo: list[Item] | None = None
) -> dict[str, list[Item]]:
    """Die Pruefungen. Jede liefert eine Liste von Eintraegen, die auffallen.

    open_in_repo=None heisst: der Abgleich mit dem Repo lief nicht. Dann fehlt der
    Befund ganz, statt faelschlich "nichts fehlt" zu behaupten.
    """
    # Nach URL, nicht nach Nummer: das Board kann Eintraege aus mehreren Repos tragen,
    # und #5 aus zwei Repos sind zwei Vorgaenge. Entwuerfe haben keine URL und keinen
    # Zwilling, sie bleiben aussen vor.
    # Archiviert heisst weggelegt: fuer Drift, Dopplung und Archiv zaehlt nur das
    # sichtbare Board. Beim Abgleich mit dem Repo zaehlen archivierte dagegen mit.
    board = [item for item in items if not item.is_archived]
    archived = [item for item in items if item.is_archived]

    seen = Counter(item.url for item in board if item.url)
    duplicates = [item for item in board if item.url and seen[item.url] > 1]

    findings = {
        "Doppelt auf dem Board": duplicates,
        "Ohne Status": [item for item in board if not item.status],
        # Nur was wirklich in Arbeit ist, braucht eine Iteration. Die Prueflinie auf
        # jeden offenen Eintrag zu legen hiesse, den ganzen Backlog taeglich zu melden.
        "In Arbeit, aber ohne Iteration": [
            item
            for item in board
            if item.is_in_progress and not item.fields.get("Iteration")
        ],
        f"In Arbeit, seit {IN_PROGRESS_STALE_AFTER_DAYS} Tagen ohne Bewegung": [
            item
            for item in board
            if item.is_in_progress
            and item.age_days(now) >= IN_PROGRESS_STALE_AFTER_DAYS
        ],
        f"Fertig, aelter als {DONE_ARCHIVE_AFTER_DAYS} Tage (Archiv-Kandidat)": [
            item
            for item in board
            # is_finished zusaetzlich: ein wieder geoeffneter Eintrag, der auf Done
            # haengen blieb, ist aktive Arbeit und darf nie aus der Sicht verschwinden.
            if item.is_done
            and item.is_finished
            and item.age_days(now) >= DONE_ARCHIVE_AFTER_DAYS
        ],
        "Abgeschlossen, steht aber nicht auf Done": [
            item for item in board if item.is_finished and not item.is_done
        ],
        "Offen, steht aber auf Done": [
            item for item in board if item.state == "OPEN" and item.is_done
        ],
        # Wieder geoeffnet, aber archiviert: gehoert zurueckgeholt, nicht neu angelegt.
        "Archiviert, aber wieder offen": [
            item for item in archived if item.state == "OPEN"
        ],
    }
    if open_in_repo is not None:
        findings["Offen im Repo, fehlt auf dem Board"] = find_missing(
            items, open_in_repo
        )
    return findings


def render_report(
    project_title: str,
    items: list[Item],
    findings: dict[str, list[Item]],
    now: datetime,
) -> str:
    """Den Bericht als Markdown bauen - eine Zusammenfassung, kein Eintrag-fuer-Eintrag-Geplapper."""
    lines = [
        f"# Projektpflege - {project_title}",
        "",
        f"Stand {now:%d.%m.%Y %H:%M} UTC · {sum(not i.is_archived for i in items)} Eintraege aktiv",
        "",
    ]

    total = sum(len(found) for found in findings.values())
    if total == 0:
        lines.append("Keine Abweichungen. Das Board passt zu den Issues.")
        return "\n".join(lines)

    for heading, found in findings.items():
        if not found:
            continue
        lines.append(f"## {heading} ({len(found)})")
        lines.append("")
        for item in sorted(found, key=lambda i: i.number or 0):
            age = item.age_days(now)
            lines.append(f"- {item.label} - zuletzt bewegt vor {age} Tagen")
        lines.append("")

    return "\n".join(lines)


def items_to_mark_done(items: list[Item]) -> list[Item]:
    """Was GitHub abgeschlossen hat, das Board aber nicht. Genau die Luecke, die die
    eingebauten Workflows rueckwirkend nicht schliessen."""
    return [
        item
        for item in items
        if not item.is_archived and item.is_finished and not item.is_done
    ]


def pick_done_option(options: list[dict]) -> str:
    """Die Done-Option des Status-Felds finden, gleich welche Schreibweise sie hat."""
    for option in options:
        if _normalized(option.get("name")) in DONE_NAMES:
            return option["id"]
    names = ", ".join(option.get("name", "?") for option in options) or "keine"
    raise ProjectError(f"Status-Feld hat keine Option fuer Done (vorhanden: {names})")


def mark_done(
    owner: str, number: int, project_id: str, items: list[Item], token: str
) -> list[str]:
    """Abgeschlossene Eintraege auf Done setzen. Idempotent: ein zweiter Lauf findet nichts."""
    if not items:
        return []
    data = graphql(STATUS_FIELD_QUERY, {"owner": owner, "number": number}, token)
    status_field = ((data.get("user") or {}).get("projectV2") or {}).get("field")
    if not status_field or "options" not in status_field:
        raise ProjectError("Das Project hat kein Single-Select-Feld namens Status.")
    option_id = pick_done_option(status_field["options"])

    fixed = []
    for item in items:
        graphql(
            SET_STATUS_MUTATION,
            {
                "projectId": project_id,
                "itemId": item.node_id,
                "fieldId": status_field["id"],
                "optionId": option_id,
            },
            token,
        )
        # Lokal nachziehen, damit der Bericht den Stand nach der Korrektur zeigt.
        item.fields["Status"] = "Done"
        # GitHub setzt updatedAt bei jeder Feldaenderung neu. Ohne das hier galte ein
        # eben korrigierter Eintrag im selben Lauf als 14 Tage alt und wuerde mit
        # --archive sofort wieder verschwinden, statt erst sichtbar Done zu sein.
        item.updated_at = datetime.now(UTC)
        fixed.append(item.label)
    return fixed


def archive_stale_done(
    project_id: str, candidates: list[Item], token: str
) -> list[str]:
    """Fertige Eintraege archivieren. Laeuft nur mit --archive."""
    archived = []
    for item in candidates:
        graphql(
            ARCHIVE_MUTATION, {"projectId": project_id, "itemId": item.node_id}, token
        )
        archived.append(item.label)
    return archived


def main() -> int:
    # Ohne das schreibt Windows den Bericht in cp1252 und zerlegt jedes Sonderzeichen.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Abgeschlossene Eintraege auf Done setzen. Sicher und idempotent.",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Done-Eintraege nach 14 Tagen archivieren. Nur von Hand, blendet sie aus der Roadmap aus.",
    )
    args = parser.parse_args()

    token = os.environ.get("PROJECT_TOKEN", "")
    owner = os.environ.get("PROJECT_OWNER", "")
    raw_number = os.environ.get("PROJECT_NUMBER", "")
    repo = os.environ.get("PROJECT_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
    missing = [
        name
        for name, value in (
            ("PROJECT_TOKEN", token),
            ("PROJECT_OWNER", owner),
            ("PROJECT_NUMBER", raw_number),
        )
        if not value
    ]
    if missing:
        print(f"Fehlende Umgebungsvariablen: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        number = int(raw_number)
    except ValueError:
        print(f"PROJECT_NUMBER ist keine Zahl: {raw_number!r}", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    try:
        project_id, project_title, items = fetch_items(owner, number, token)

        # Erst korrigieren, dann pruefen: der Bericht zeigt, was danach noch offen ist.
        fixed = (
            mark_done(owner, number, project_id, items_to_mark_done(items), token)
            if args.fix
            else []
        )

        open_in_repo = fetch_open_in_repo(repo, token) if repo else None
        findings = find_issues(items, now, open_in_repo)
        archive_key = next(key for key in findings if "Archiv-Kandidat" in key)
        candidates = findings.pop(archive_key)
        # Erledigtes bleibt sichtbar, bis es jemand bewusst archiviert. Ohne --archive
        # wuerde die Liste taeglich mit jedem fertigen Eintrag laenger - reines Rauschen.
        if args.archive:
            findings[archive_key] = candidates

        report = render_report(project_title, items, findings, now)
        if open_in_repo is None:
            report += "\n\nHinweis: PROJECT_REPO nicht gesetzt, fehlende Eintraege wurden nicht gesucht."
        if fixed:
            report += f"\n\n## Auf Done gesetzt ({len(fixed)})\n\n"
            report += "\n".join(f"- {label}" for label in fixed)
        if args.archive:
            archived = archive_stale_done(project_id, candidates, token)
            report += f"\n\n## Archiviert ({len(archived)})\n\n"
            report += "\n".join(f"- {label}" for label in archived) or "- nichts"
    except ProjectError as exc:
        print(f"Projektpflege fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
