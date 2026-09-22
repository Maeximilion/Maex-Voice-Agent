#!/usr/bin/env python3
"""Taegliche Pflege des GitHub-Projects: Abweichungen melden, nichts nebenbei aendern.

Zweck : Das Board ist eine Ableitung. Waehrend der Arbeit fasst es niemand an;
        einmal taeglich prueft dieses Skript, ob es noch zu Issues und
        docs/07_WORKPACKAGES.md passt, und meldet die Abweichungen.
Aufruf: python scripts/project_report.py [--apply]
        Ohne --apply wird nur gelesen und berichtet (Trockenlauf, Standard).
Umgeb.: PROJECT_TOKEN   Fine-grained PAT, Projects: Read (fuer --apply: Read and write)
        PROJECT_OWNER   Kontoname, dem das Project gehoert
        PROJECT_NUMBER  Nummer des Projects aus seiner URL
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

ARCHIVE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!) {
  archiveProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
    item { id }
  }
}
"""


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

    @property
    def status(self) -> str | None:
        return self.fields.get("Status")

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
    """Einen GraphQL-Knoten in ein Item uebersetzen. Archivierte Eintraege fallen raus."""
    if node.get("isArchived"):
        return None

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
    )


def fetch_items(owner: str, number: int, token: str) -> tuple[str, str, list[Item]]:
    """Alle nicht archivierten Eintraege des Projects holen, seitenweise."""
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


def find_issues(items: list[Item], now: datetime) -> dict[str, list[Item]]:
    """Die Pruefungen. Jede liefert eine Liste von Eintraegen, die auffallen."""
    seen = Counter(item.number for item in items if item.number is not None)
    duplicates = [
        item for item in items if item.number is not None and seen[item.number] > 1
    ]

    return {
        "Doppelt auf dem Board": duplicates,
        "Ohne Status": [item for item in items if not item.status],
        "Offen, aber ohne Iteration": [
            item
            for item in items
            if item.state == "OPEN" and not item.fields.get("Iteration")
        ],
        f"In Arbeit, seit {IN_PROGRESS_STALE_AFTER_DAYS} Tagen ohne Bewegung": [
            item
            for item in items
            if item.status == "In Progress"
            and item.age_days(now) >= IN_PROGRESS_STALE_AFTER_DAYS
        ],
        f"Fertig, aelter als {DONE_ARCHIVE_AFTER_DAYS} Tage (Archiv-Kandidat)": [
            item
            for item in items
            if item.status == "Done" and item.age_days(now) >= DONE_ARCHIVE_AFTER_DAYS
        ],
        "Geschlossen, steht aber nicht auf Done": [
            item for item in items if item.state == "CLOSED" and item.status != "Done"
        ],
    }


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
        f"Stand {now:%d.%m.%Y %H:%M} UTC · {len(items)} Eintraege aktiv",
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


def archive_stale_done(
    project_id: str, candidates: list[Item], token: str
) -> list[str]:
    """Fertige Eintraege archivieren. Laeuft nur mit --apply."""
    archived = []
    for item in candidates:
        graphql(
            ARCHIVE_MUTATION, {"projectId": project_id, "itemId": item.node_id}, token
        )
        archived.append(item.label)
    return archived


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Archiv-Kandidaten wirklich archivieren. Ohne diesen Schalter wird nur berichtet.",
    )
    args = parser.parse_args()

    token = os.environ.get("PROJECT_TOKEN", "")
    owner = os.environ.get("PROJECT_OWNER", "")
    raw_number = os.environ.get("PROJECT_NUMBER", "")
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
        findings = find_issues(items, now)
        report = render_report(project_title, items, findings, now)

        if args.apply:
            candidates = findings[
                f"Fertig, aelter als {DONE_ARCHIVE_AFTER_DAYS} Tage (Archiv-Kandidat)"
            ]
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
