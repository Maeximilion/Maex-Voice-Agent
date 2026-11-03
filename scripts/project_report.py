#!/usr/bin/env python3
"""Reconcile the GitHub project with repository facts; read-only by default.

--fix fills missing facts, restores completed status and extends iterations.
--history includes closed issues and merged PRs; with --fix, adds missing items
          after checking direct membership, including archives.
--status-only limits reconciliation to status (the hourly safety net).
--archive explicitly archives finished Done items idle for fourteen days.
--findings-json FILE supplies unresolved findings to decision_issue.py.

Environment: PROJECT_TOKEN (Projects read, write for --fix/--archive),
PROJECT_OWNER, PROJECT_NUMBER. PROJECT_REPO is owner/name, defaulting to
GITHUB_REPOSITORY. Without either, repository comparison is explicitly omitted.
Standard library only. Dates are displayed in Europe/Berlin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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
          type
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
              ... on ProjectV2ItemFieldDateValue {
                date
                field { ... on ProjectV2Field { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              id number title state url createdAt closedAt
              labels(first: 20) { nodes { name } }
              closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
                pageInfo { hasNextPage endCursor }
                nodes { state createdAt }
              }
            }
            ... on PullRequest {
              id number title state url createdAt mergedAt closedAt
              labels(first: 20) { nodes { name } }
            }
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
REPO_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    CONNECTION(states: STATES, first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        __typename id number title url state createdAt closedAt updatedAt
        labels(first: 100) { nodes { name } }
        EXTRA
      }
    }
  }
}
"""

MEMBERSHIP_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on Issue { projectItems(first: 100, after: $cursor, includeArchived: true) {
      ...Memberships
    } }
    ... on PullRequest { projectItems(first: 100, after: $cursor, includeArchived: true) {
      ...Memberships
    } }
  }
}
fragment Memberships on ProjectV2ItemConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    id isArchived updatedAt project { id }
    fieldValues(first: 100) { nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name field { ... on ProjectV2SingleSelectField { name } }
      }
      ... on ProjectV2ItemFieldIterationValue {
        title field { ... on ProjectV2IterationField { name } }
      }
      ... on ProjectV2ItemFieldDateValue {
        date field { ... on ProjectV2Field { name } }
      }
    } }
  }
}
"""

ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id isArchived }
  }
}
"""

CLOSING_PAGE_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) { ... on Issue {
    closedByPullRequestsReferences(first: 100, after: $cursor, includeClosedPrs: true) {
      pageInfo { hasNextPage endCursor }
      nodes { state createdAt }
    }
  } }
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

FIELDS_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      fields(first: 50) {
        nodes {
          ... on ProjectV2Field { id name dataType }
          ... on ProjectV2IterationField {
            id name
            configuration {
              iterations { id title startDate duration }
              completedIterations { id title startDate duration }
            }
          }
        }
      }
    }
  }
}
"""

SET_DATE_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $date: Date!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: {date: $date}
  }) { projectV2Item { id } }
}
"""

SET_ITERATION_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $iterationId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: {iterationId: $iterationId}
  }) { projectV2Item { id } }
}
"""

SET_ITERATIONS_MUTATION = """
mutation($fieldId: ID!, $config: ProjectV2IterationFieldConfigurationInput!) {
  updateProjectV2Field(input: {fieldId: $fieldId, iterationConfiguration: $config}) {
    projectV2Field { ... on ProjectV2IterationField { id } }
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


# GitHub schreibt die Optionen so, wie sie angelegt wurden: "In progress" aus der
# Vorlage, "In Progress" von Hand. Ein Vergleich auf den exakten Text laesst den
# Bericht still danebenliegen, darum wird normalisiert statt gleichgesetzt.
IN_PROGRESS_NAMES = frozenset({"in progress", "in arbeit"})
DONE_NAMES = frozenset({"done", "fertig", "erledigt"})
IN_REVIEW_NAMES = frozenset({"in review"})
BLOCKED_NAMES = frozenset({"blocked"})
STATUS_NAMES = {
    "Todo": frozenset({"todo"}),
    "Blocked": BLOCKED_NAMES,
    "In progress": IN_PROGRESS_NAMES,
    "In review": IN_REVIEW_NAMES,
    "Done": DONE_NAMES,
}

# Mirrors the blocked state of docs/07_WORKPACKAGES.md, the one status that is also
# a label (docs/16_GITHUB_PROJECT.md section 3). It wins over every other open state.
BLOCKED_LABEL = "status: blocked"

# Das Label des Entscheidungs-Issues (scripts/decision_issue.py). Das Issue landet
# ueber die automatische Aufnahme selbst auf dem Board; wieder geoeffnet steht es auf
# In progress ohne Iteration. Pruefte man es mit, meldete es sich selbst und koennte
# sich nie schliessen. Darum ist es von jeder Pruefung ausgenommen.
MAINTENANCE_LABEL = "projektpflege"

# Gespeichert wird in UTC, angezeigt in Berlin. Ein Datum auf dem Board ist Anzeige:
# ein Pull Request von 23:30 UTC gehoert in Berlin schon zum naechsten Tag.
BERLIN = ZoneInfo("Europe/Berlin")

# Board fields that follow from facts: a pull request starts when it opens, an
# issue when the first pull request that closes it opens (Maxi, 2026-10-28). An
# issue nobody works on yet gets none: its dates would be a guess.
DATE_FIELDS = ("Start date", "Target date")
ITERATION_FIELDS = ("Iteration", "Quarter")


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
    # "Issue", "PullRequest" oder "DraftIssue". Noetig, weil CLOSED bei einem Issue
    # erledigt heisst, bei einem Pull Request aber abgelehnt.
    kind: str | None = None
    labels: frozenset[str] = frozenset()
    created_at: datetime | None = None
    # Bei Pull Requests der Merge, sonst das Schliessen. None, solange offen.
    closed_at: datetime | None = None
    # When work on it began: a pull request's opening, for an issue the earliest
    # pull request that closes it (open or merged). None while nobody works on it.
    work_started: datetime | None = None
    # An issue with an open pull request that closes it is in progress.
    has_open_pr: bool = False
    content_id: str = ""
    body: str = ""

    @property
    def status(self) -> str | None:
        return self.fields.get("Status")

    @property
    def is_maintenance(self) -> bool:
        return MAINTENANCE_LABEL in self.labels

    @property
    def is_in_progress(self) -> bool:
        return _normalized(self.status) in IN_PROGRESS_NAMES

    @property
    def is_active(self) -> bool:
        """Someone is on it: being worked on or waiting for its review."""
        return self.is_in_progress or _normalized(self.status) in IN_REVIEW_NAMES

    @property
    def is_done(self) -> bool:
        return _normalized(self.status) in DONE_NAMES

    @property
    def is_closed(self) -> bool:
        """Auf GitHub zu, gleich auf welchem Weg. Reicht fuers Archiv: auch ein
        abgelehnter Pull Request ist vorbei."""
        return self.state in ("CLOSED", "MERGED")

    @property
    def is_finished(self) -> bool:
        """Erledigt im Sinne der Konvention: ein Issue geschlossen, ein Pull Request
        gemerged. Nur das darf auf Done. Ein Pull Request ohne Merge hat ebenfalls
        den Zustand CLOSED und waere sonst als erledigt durchgerutscht."""
        if self.kind == "PullRequest":
            return self.state == "MERGED"
        return self.is_closed

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
    if query.lstrip().startswith("mutation"):
        # Historical backfills can contain hundreds of field writes. Pace every
        # mutation, including status and dates, below the secondary write limit.
        time.sleep(1)
    return body["data"]


def _timestamp(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


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
        # Single-Select liefert "name", Iterationen "title", Datumsfelder "date".
        fields[field_name] = (
            value.get("name") or value.get("title") or value.get("date") or ""
        )

    content = node.get("content") or {}
    kind = content.get("__typename")
    closing = [
        ref
        for ref in (content.get("closedByPullRequestsReferences") or {}).get(
            "nodes", []
        )
        if ref.get("state") in ("OPEN", "MERGED")
    ]
    if kind == "PullRequest":
        work_started = _timestamp(content.get("createdAt"))
    else:
        opened = [t for ref in closing if (t := _timestamp(ref.get("createdAt")))]
        work_started = min(opened, default=None)
    return Item(
        node_id=node["id"],
        title=content.get("title", "(ohne Titel)"),
        updated_at=datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00")),
        number=content.get("number"),
        kind=kind,
        content_id=content.get("id", ""),
        body=content.get("body") or "",
        work_started=work_started,
        has_open_pr=any(ref["state"] == "OPEN" for ref in closing),
        created_at=_timestamp(content.get("createdAt")),
        closed_at=_timestamp(content.get("mergedAt") or content.get("closedAt")),
        labels=frozenset(
            label["name"] for label in (content.get("labels") or {}).get("nodes", [])
        ),
        state=content.get("state"),
        url=content.get("url"),
        fields=fields,
        is_archived=bool(node.get("isArchived")),
    )


# Laut Schema (Union ProjectV2ItemContent) gibt es genau diese drei Inhalte.
READABLE_CONTENT = frozenset({"Issue", "PullRequest", "DraftIssue"})


def _is_unreadable(node: dict) -> bool:
    """Ob der Token den Inhalt eines Board-Eintrags sehen darf.

    Das offizielle Signal ist ProjectV2Item.type = REDACTED. Dazu kommt content = null,
    und defensiv jeder Inhaltstyp ausserhalb des Schemas - lieber einmal zu laut als
    einen Eintrag still uebergehen.
    """
    if node.get("type") == "REDACTED":
        return True
    content = node.get("content")
    return content is None or content.get("__typename") not in READABLE_CONTENT


def fetch_items(owner: str, number: int, token: str) -> tuple[str, str, list[Item]]:
    """Alle Eintraege des Projects holen, archivierte markiert, seitenweise."""
    items: list[Item] = []
    cursor: str | None = None
    project_id = ""
    project_title = ""
    unreadable = 0

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
        for node in page["nodes"]:
            complete_closing_refs(node.get("content") or {}, token)
        # Unlesbare Eintraege haben weder Nummer noch Zustand, --fix faende nichts und
        # liefe still gruen durch.
        unreadable += sum(1 for node in page["nodes"] if _is_unreadable(node))
        items.extend(item for node in page["nodes"] if (item := parse_item(node)))

        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    if unreadable:
        raise ProjectError(
            f"Der Token sieht den Inhalt von {unreadable} Eintraegen nicht. Ihm fehlt "
            "der Lesezugriff auf das Repo dahinter - klassisch Scope 'repo', "
            "fine-grained 'Issues' und 'Pull requests' lesend. Siehe docs/16 Abschnitt 5."
        )
    return project_id, project_title, items


def complete_closing_refs(content: dict, token: str) -> None:
    """Do not derive a start from a truncated list of linked implementations."""
    closing = content.get("closedByPullRequestsReferences") or {}
    next_page = closing.get("pageInfo", {})
    while next_page.get("hasNextPage"):
        extra = graphql(
            CLOSING_PAGE_QUERY,
            {"id": content["id"], "cursor": next_page["endCursor"]},
            token,
        )["node"]["closedByPullRequestsReferences"]
        closing["nodes"].extend(extra["nodes"])
        next_page = extra["pageInfo"]


def fetch_in_repo(repo: str, token: str, *, history: bool) -> list[Item]:
    """Read the repository, including historical issues and merged PRs on request."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ProjectError(f"PROJECT_REPO muss owner/name sein, nicht {repo!r}")

    found: list[Item] = []
    for connection in ("issues", "pullRequests"):
        states = "OPEN"
        if history:
            states = "[OPEN, CLOSED]" if connection == "issues" else "[OPEN, MERGED]"
        query = (
            REPO_QUERY.replace("CONNECTION", connection)
            .replace("STATES", states)
            .replace(
                "EXTRA",
                "body mergedAt"
                if connection == "pullRequests"
                else """
                closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
                    pageInfo { hasNextPage endCursor }
                    nodes { state createdAt }
                }
            """,
            )
        )
        cursor: str | None = None
        while True:
            data = graphql(
                query, {"owner": owner, "name": name, "cursor": cursor}, token
            )
            repository = data.get("repository")
            if repository is None:
                raise ProjectError(f"Repo {repo} nicht gefunden oder nicht lesbar.")
            page = repository[connection]
            for node in page["nodes"]:
                complete_closing_refs(node, token)
                item = parse_item(
                    {"id": "", "updatedAt": node["updatedAt"], "content": node}
                )
                assert item is not None
                found.append(item)
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return found


def enrich_starts(items: list[Item], repo_items: list[Item]) -> None:
    """Explicit same-repository PR references can evidence an earlier partial step.

    They do not create closing links or change issue status. Code examples and
    comments are excluded; bare issue mentions are not implementation evidence.
    """
    starts: dict[str, datetime] = {}
    for pull in repo_items:
        if pull.kind != "PullRequest" or pull.state not in {"OPEN", "MERGED"}:
            continue
        if not pull.url or not pull.created_at:
            continue
        body = re.sub(
            r"<!--.*?-->|```.*?```|~~~.*?~~~|`[^`]*`", "", pull.body, flags=re.S
        )
        for number in re.findall(
            r"(?i)\b(?:refs?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", body
        ):
            url = pull.url.rsplit("/pull/", 1)[0] + "/issues/" + number
            starts[url] = min(starts.get(url, pull.created_at), pull.created_at)
    for item in items:
        if item.kind == "Issue" and item.url in starts:
            item.work_started = min(
                item.work_started or starts[item.url], starts[item.url]
            )


def reconcile_history(
    project_id: str, items: list[Item], repo_items: list[Item], token: str, *, fix: bool
) -> list[str]:
    """Check direct membership before adding missing history; never unarchive it.

    GitHub's project-wide connection has omitted an existing item in practice.
    The content's membership connection is the second check before any write.
    """
    added = []
    for missing in find_missing(items, repo_items):
        if missing.is_maintenance:
            continue
        cursor = None
        memberships: list[dict] = []
        while True:
            page = graphql(
                MEMBERSHIP_QUERY, {"id": missing.content_id, "cursor": cursor}, token
            )["node"]["projectItems"]
            memberships.extend(
                n for n in page["nodes"] if n["project"]["id"] == project_id
            )
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        for membership in memberships:
            # Keep repository facts and recover the existing board fields.
            recovered = parse_item(membership)
            assert recovered is not None
            items.append(
                replace(
                    missing,
                    node_id=recovered.node_id,
                    fields=recovered.fields,
                    is_archived=recovered.is_archived,
                    updated_at=recovered.updated_at,
                )
            )
        if memberships or not fix:
            continue
        result = graphql(
            ADD_ITEM_MUTATION,
            {"projectId": project_id, "contentId": missing.content_id},
            token,
        )["addProjectV2ItemById"]["item"]
        missing.node_id = result["id"]
        missing.is_archived = result["isArchived"]
        items.append(missing)
        added.append(missing.label)
    return added


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
    board = [item for item in items if not item.is_archived and not item.is_maintenance]
    archived = [item for item in items if item.is_archived and not item.is_maintenance]

    seen = Counter(item.url for item in board if item.url)
    duplicates = [item for item in board if item.url and seen[item.url] > 1]

    findings = {
        "Start date after completion": [
            item
            for item in board
            if item.closed_at
            and item.fields.get("Start date", "")
            > item.closed_at.astimezone(BERLIN).date().isoformat()
        ],
        "Implementation starts after completion": [
            item
            for item in board
            if item.work_started
            and item.closed_at
            and item.work_started > item.closed_at
            and not item.fields.get("Start date")
        ],
        "Target date differs from completion": [
            item
            for item in board
            if item.closed_at
            and item.fields.get("Target date")
            and item.fields["Target date"]
            != item.closed_at.astimezone(BERLIN).date().isoformat()
        ],
        "Start date after target date": [
            item
            for item in board
            if item.fields.get("Start date")
            and item.fields.get("Target date")
            and item.fields["Start date"] > item.fields["Target date"]
        ],
        "Doppelt auf dem Board": duplicates,
        "Ohne Status": [item for item in board if not item.status],
        # Nur was wirklich in Arbeit ist, braucht eine Iteration. Die Prueflinie auf
        # jeden offenen Eintrag zu legen hiesse, den ganzen Backlog taeglich zu melden.
        "In Arbeit, aber ohne Iteration": [
            item
            for item in board
            if item.is_active and not item.fields.get("Iteration")
        ],
        f"In Arbeit, seit {IN_PROGRESS_STALE_AFTER_DAYS} Tagen ohne Bewegung": [
            item
            for item in board
            if item.is_active and item.age_days(now) >= IN_PROGRESS_STALE_AFTER_DAYS
        ],
        f"Fertig, aelter als {DONE_ARCHIVE_AFTER_DAYS} Tage (Archiv-Kandidat)": [
            item
            for item in board
            # is_finished zusaetzlich: ein wieder geoeffneter Eintrag, der auf Done
            # haengen blieb, ist aktive Arbeit und darf nie aus der Sicht verschwinden.
            if item.is_done
            and item.is_closed
            and item.age_days(now) >= DONE_ARCHIVE_AFTER_DAYS
        ],
        "Abgeschlossen, steht aber nicht auf Done": [
            item for item in board if item.is_finished and not item.is_done
        ],
        "Offen, steht aber auf Done": [
            item for item in board if item.state == "OPEN" and item.is_done
        ],
        # Wieder geoeffnet, aber archiviert: gehoert zurueckgeholt, nicht neu angelegt.
        # Weder erledigt noch Drift: ob ein abgelehnter Pull Request auf Done oder ins
        # Archiv gehoert, entscheidet ein Mensch. Steht er schon auf Done, ist das
        # entschieden, und er meldet sich nicht jeden Tag neu.
        "Pull Request ohne Merge geschlossen": [
            item
            for item in board
            if item.kind == "PullRequest"
            and item.state == "CLOSED"
            and not item.is_done
        ],
        # Nur ohne aktiven Zwilling: eine archivierte Dopplung teilt Zustand und URL mit
        # dem Eintrag, der stehen blieb. Sie zurueckzuholen hiesse, die eben beseitigte
        # Dopplung wiederherzustellen.
        "Archiviert, aber wieder offen": [
            item for item in archived if item.state == "OPEN" and item.url not in seen
        ],
    }
    if open_in_repo is not None:
        findings["Offen im Repo, fehlt auf dem Board"] = find_missing(
            items,
            [
                ref
                for ref in open_in_repo
                if not ref.is_maintenance and ref.state == "OPEN"
            ],
        )
        findings["Completed in repository, missing from project"] = find_missing(
            items,
            [ref for ref in open_in_repo if not ref.is_maintenance and ref.is_finished],
        )
    return findings


def findings_to_json(findings: dict[str, list[Item]]) -> dict[str, list[dict]]:
    """Befunde als schlichtes JSON: Ueberschrift -> Eintraege mit Label und URL.
    Leere Befunde fallen weg, damit 'nichts offen' eindeutig ein leeres Objekt ist."""
    return {
        # id: die Board-ID, damit zwei Kopien desselben Issues unterscheidbar bleiben.
        # Leer bei Eintraegen, die auf dem Board fehlen - die haben noch keine.
        heading: [
            {"id": item.node_id or None, "label": item.label, "url": item.url}
            for item in found
        ]
        for heading, found in findings.items()
        if found
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


def wanted_status(item: Item) -> str | None:
    """The status an open item should have, None to leave it alone: an issue set to
    In progress by hand stays. Never an item on Done: an open item on Done is drift
    for a human, not for this run."""
    if item.is_archived or item.is_maintenance or item.state != "OPEN" or item.is_done:
        return None
    if BLOCKED_LABEL in item.labels:
        return "Blocked"
    if item.kind == "PullRequest":
        return "In review"
    if item.has_open_pr:
        return "In progress"
    # The label came off: an issue without an open pull request starts over.
    return "Todo" if _normalized(item.status) in BLOCKED_NAMES else None


def status_moves(items: list[Item]) -> dict[str, list[Item]]:
    """The open items whose status differs from what the facts say, by target."""
    moves: dict[str, list[Item]] = {}
    for item in items:
        wanted = wanted_status(item)
        if wanted and _normalized(item.status) not in STATUS_NAMES[wanted]:
            moves.setdefault(wanted, []).append(item)
    return moves


def pick_option(options: list[dict], names: frozenset[str], wanted: str) -> str:
    """The status option for `wanted`, whatever its spelling on the board."""
    for option in options:
        if _normalized(option.get("name")) in names:
            return option["id"]
    present = ", ".join(option.get("name", "?") for option in options) or "keine"
    raise ProjectError(
        f"Status-Feld hat keine Option fuer {wanted} (vorhanden: {present})"
    )


def pick_done_option(options: list[dict]) -> str:
    """Die Done-Option des Status-Felds finden, gleich welche Schreibweise sie hat."""
    return pick_option(options, DONE_NAMES, "Done")


def mark_done(
    owner: str, number: int, project_id: str, items: list[Item], token: str
) -> list[str]:
    """Abgeschlossene Eintraege auf Done setzen. Idempotent: ein zweiter Lauf findet nichts."""
    return set_status(owner, number, project_id, items, token, "Done")


def set_status(
    owner: str,
    number: int,
    project_id: str,
    items: list[Item],
    token: str,
    wanted: str,
) -> list[str]:
    """Set `wanted`, a key of STATUS_NAMES, on the items. Idempotent."""
    if not items:
        return []
    data = graphql(STATUS_FIELD_QUERY, {"owner": owner, "number": number}, token)
    status_field = ((data.get("user") or {}).get("projectV2") or {}).get("field")
    if not status_field or "options" not in status_field:
        raise ProjectError("Das Project hat kein Single-Select-Feld namens Status.")
    option_id = pick_option(status_field["options"], STATUS_NAMES[wanted], wanted)

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
        item.fields["Status"] = wanted
        # GitHub setzt updatedAt bei jeder Feldaenderung neu. Ohne das hier galte ein
        # eben korrigierter Eintrag im selben Lauf als 14 Tage alt und wuerde mit
        # --archive sofort wieder verschwinden, statt erst sichtbar Done zu sein.
        item.updated_at = datetime.now(UTC)
        fixed.append(item.label)
    return fixed


def pick_iteration(iterations: list[dict], day: date) -> str | None:
    """Die Iteration, in deren Zeitraum der Tag liegt. Keine, wenn er davor oder
    dazwischen liegt - dann wird nichts erfunden."""
    for iteration in iterations:
        start = date.fromisoformat(iteration["startDate"])
        if start <= day < start + timedelta(days=iteration["duration"]):
            return iteration["id"]
    return None


def derive_fields(item: Item) -> dict[str, str]:
    """Fill empty fields from facts, preserving manual values.

    Completion does not depend on a linked PR. Start and periods require evidence
    of implementation no later than completion. Values are ISO Berlin dates;
    iteration fields use that date to select the configured period.
    """
    derived = {}
    if item.work_started is not None and (
        item.closed_at is None or item.work_started <= item.closed_at
    ):
        started = item.work_started.astimezone(BERLIN).date().isoformat()
        derived = dict.fromkeys(("Start date", *ITERATION_FIELDS), started)
    # Completion is known even when no implementation PR is linked.
    if item.closed_at is not None:
        derived["Target date"] = item.closed_at.astimezone(BERLIN).date().isoformat()
    return {name: value for name, value in derived.items() if not item.fields.get(name)}


def fill_fields(
    owner: str, number: int, project_id: str, items: list[Item], token: str
) -> list[str]:
    """Leere Board-Felder nachtragen. Idempotent: gefuellte Felder werden nie
    ueberschrieben, ein zweiter Lauf findet nichts mehr."""
    todo = [
        (item, derived)
        for item in items
        if not item.is_archived and (derived := derive_fields(item))
    ]
    if not todo:
        return []

    data = graphql(FIELDS_QUERY, {"owner": owner, "number": number}, token)
    nodes = ((data.get("user") or {}).get("projectV2") or {}).get("fields", {})
    fields = {node["name"]: node for node in nodes.get("nodes", []) if node.get("name")}

    filled = []
    for item, derived in todo:
        done = []
        for name, day in derived.items():
            field_def = fields.get(name)
            if field_def is None:
                continue  # Das Board hat dieses Feld nicht - nichts zu tun.
            variables = {
                "projectId": project_id,
                "itemId": item.node_id,
                "fieldId": field_def["id"],
            }
            if name in ITERATION_FIELDS:
                config = field_def.get("configuration") or {}
                iterations = config.get("iterations", []) + config.get(
                    "completedIterations", []
                )
                iteration_id = pick_iteration(iterations, date.fromisoformat(day))
                if iteration_id is None:
                    continue
                graphql(
                    SET_ITERATION_MUTATION,
                    {**variables, "iterationId": iteration_id},
                    token,
                )
            else:
                graphql(SET_DATE_MUTATION, {**variables, "date": day}, token)
            item.fields[name] = day
            done.append(name)
        if done:
            # Wie in mark_done: GitHub setzt updatedAt bei jeder Feldaenderung neu. Sonst
            # galte ein eben ergaenzter PR im selben Lauf als alt und --archive nahme
            # ihn sofort mit.
            item.updated_at = datetime.now(UTC)
            filled.append(f"{item.label}: {', '.join(done)}")
    return filled


# Iterations are added this far ahead, so a pull request opened today always finds
# one. GitHub does not roll iteration fields on by itself (Iteration 3 ended on
# 2026-10-27 with none after it).
ITERATIONS_AHEAD_DAYS = 28


def next_iterations(known: list[dict], today: date) -> list[dict]:
    """Iterations to append so that one covers `today + ITERATIONS_AHEAD_DAYS`.

    Each new one continues the last: same duration, the next number in the title
    ("Iteration 3" -> "Iteration 4"). Nothing when the field already reaches far
    enough, or has no iteration to continue from."""
    if not known:
        return []
    last = max(known, key=lambda it: it["startDate"])
    duration = last["duration"]
    start = date.fromisoformat(last["startDate"]) + timedelta(days=duration)
    prefix, _, count = last.get("title", "").rpartition(" ")
    number = int(count) if count.isdigit() else len(known)
    prefix = prefix or "Iteration"
    new = []
    while start <= today + timedelta(days=ITERATIONS_AHEAD_DAYS):
        number += 1
        new.append(
            {
                "startDate": start.isoformat(),
                "duration": duration,
                "title": f"{prefix} {number}",
            }
        )
        start += timedelta(days=duration)
    return new


def extend_iterations(owner: str, number: int, token: str) -> list[str]:
    """Append the iterations next_iterations asks for. Existing ones are sent back
    with their ids, so values already on items stay (checked on 2026-10-28)."""
    data = graphql(FIELDS_QUERY, {"owner": owner, "number": number}, token)
    nodes = ((data.get("user") or {}).get("projectV2") or {}).get("fields", {})
    added = []
    for node in nodes.get("nodes", []):
        if node.get("name") not in ITERATION_FIELDS or "configuration" not in node:
            continue
        config = node["configuration"]
        known = config.get("completedIterations", []) + config.get("iterations", [])
        new = next_iterations(known, datetime.now(BERLIN).date())
        if not new:
            continue
        iterations = [
            {key: it[key] for key in ("id", "title", "startDate", "duration")}
            for it in known
        ] + new
        graphql(
            SET_ITERATIONS_MUTATION,
            {
                "fieldId": node["id"],
                "config": {
                    "startDate": min(it["startDate"] for it in known),
                    "duration": new[0]["duration"],
                    "iterations": iterations,
                },
            },
            token,
        )
        added += [f"{node['name']}: {it['title']} ab {it['startDate']}" for it in new]
    return added


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
    parser.add_argument(
        "--findings-json",
        metavar="DATEI",
        help="Offene Befunde als JSON schreiben, Eingabe fuer scripts/decision_issue.py.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Reconcile all issues and merged PRs; --fix also adds missing items.",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Reconcile status only, for the hourly safety net.",
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
        if args.history and (not repo or args.status_only):
            raise ProjectError(
                "--history requires PROJECT_REPO and cannot be combined with --status-only."
            )
        project_id, project_title, items = fetch_items(owner, number, token)
        repo_items = (
            fetch_in_repo(repo, token, history=args.history)
            if repo and not args.status_only
            else None
        )
        added = (
            reconcile_history(project_id, items, repo_items or [], token, fix=args.fix)
            if args.history
            else []
        )
        enrich_starts(items, repo_items or [])

        # Erst korrigieren, dann pruefen: der Bericht zeigt, was danach noch offen ist.
        fixed = (
            mark_done(owner, number, project_id, items_to_mark_done(items), token)
            if args.fix
            else []
        )
        moved = (
            {
                wanted: set_status(owner, number, project_id, group, token, wanted)
                for wanted, group in status_moves(items).items()
            }
            if args.fix
            else {}
        )
        if args.status_only:
            print(
                f"Status reconciliation: {len(fixed)} completed items restored to Done; "
                f"{sum(len(group) for group in moved.values())} open items moved."
            )
            return 0
        # Before the fields: a pull request opened today needs an iteration to land in.
        extended = extend_iterations(owner, number, token) if args.fix else []
        filled = (
            fill_fields(owner, number, project_id, items, token) if args.fix else []
        )

        findings = find_issues(items, now, repo_items)
        archive_key = next(key for key in findings if "Archiv-Kandidat" in key)
        candidates = findings.pop(archive_key)
        # Erledigtes bleibt sichtbar, bis es jemand bewusst archiviert. Ohne --archive
        # wuerde die Liste taeglich mit jedem fertigen Eintrag laenger - reines Rauschen.
        if args.archive:
            findings[archive_key] = candidates

        report = render_report(project_title, items, findings, now)
        if repo_items is None:
            report += "\n\nHinweis: PROJECT_REPO nicht gesetzt, fehlende Eintraege wurden nicht gesucht."
        if added:
            report += f"\n\n## Historical items added ({len(added)})\n\n"
            report += "\n".join(f"- {label}" for label in added)
        if fixed:
            report += f"\n\n## Auf Done gesetzt ({len(fixed)})\n\n"
            report += "\n".join(f"- {label}" for label in fixed)
        for wanted, labels in moved.items():
            report += f"\n\n## Auf {wanted} gesetzt ({len(labels)})\n\n"
            report += "\n".join(f"- {label}" for label in labels)
        if extended:
            report += f"\n\n## Iterationen angehaengt ({len(extended)})\n\n"
            report += "\n".join(f"- {line}" for line in extended)
        if filled:
            report += f"\n\n## Felder nachgetragen ({len(filled)})\n\n"
            report += "\n".join(f"- {line}" for line in filled)
        if args.archive:
            archived = archive_stale_done(project_id, candidates, token)
            report += f"\n\n## Archiviert ({len(archived)})\n\n"
            report += "\n".join(f"- {label}" for label in archived) or "- nichts"
    except ProjectError as exc:
        print(f"Projektpflege fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    if args.findings_json:
        # Ohne Archiv-Kandidaten: die sind mit --archive erledigt und ohne es bewusst
        # kein Befund (siehe oben). Eine leere Liste heisst: das Issue darf zu.
        open_findings = {k: v for k, v in findings.items() if k != archive_key}
        with open(args.findings_json, "w", encoding="utf-8") as handle:
            json.dump(
                findings_to_json(open_findings), handle, ensure_ascii=False, indent=2
            )

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
