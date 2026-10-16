#!/usr/bin/env python3
"""Assignee und Label fuer Pull Requests nachtragen, wo sie fehlen.

Zweck : Jeder Pull Request bekommt den Repo-Eigentuemer als Assignee und ein
        Typ-Label aus seinem Titel (Conventional Commits: feat -> feature,
        fix -> bug, ...). Das deckt auch alte Pull Requests und solche von Bots ab.
        Gesetzt wird nur, was fehlt; vorhandene Labels bleiben, ein zweiter Lauf
        aendert nichts.
Aufruf: python scripts/pr_metadata.py [--dry-run]
Umgeb.: GITHUB_TOKEN       Token mit Schreibrecht auf Issues und Pull Requests;
                           in der Action der eingebaute
        GITHUB_REPOSITORY  owner/name, in einer Action automatisch gesetzt
Abhaeng.: nur Standardbibliothek.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_URL = "https://api.github.com"

# Conventional-Commit-Typ -> vorhandenes Typ-Label des Repos. Nur Labels, die es gibt:
# ein unbekanntes legte GitHub beim Setzen stillschweigend neu an.
TYPE_LABELS = {
    "feat": "feature",
    "fix": "bug",
    "docs": "docs",
    "chore": "chore",
    "ci": "chore",
    "build": "chore",
    "refactor": "refactor",
    "perf": "refactor",
    "test": "test",
}
TYPE_PREFIX = re.compile(r"^(?P<type>[a-z]+)(\([^)]*\))?!?:")


class MetadataError(RuntimeError):
    """GitHub liess sich nicht lesen oder schreiben."""


def derive_label(title: str) -> str | None:
    """Das Typ-Label aus dem Titel. None, wenn der Titel keinen bekannten Typ traegt -
    lieber kein Label als ein geratenes."""
    match = TYPE_PREFIX.match(title.strip())
    return TYPE_LABELS.get(match.group("type")) if match else None


def missing(pull: dict, owner: str) -> dict[str, list[str]]:
    """Was an einem Pull Request fehlt. Leer, wenn nichts zu tun ist."""
    change: dict[str, list[str]] = {}
    if not pull.get("assignees"):
        change["assignees"] = [owner]
    if not pull.get("labels") and (label := derive_label(pull["title"])):
        change["labels"] = [label]
    return change


def rest(method: str, path: str, token: str, payload: dict | None = None):
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "maex-voice-agent-pr-metadata",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise MetadataError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise MetadataError(f"GitHub nicht erreichbar: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def all_pulls(repo: str, token: str) -> list[dict]:
    pulls: list[dict] = []
    page = 1
    while True:
        batch = rest(
            "GET", f"/repos/{repo}/pulls?state=all&per_page=100&page={page}", token
        )
        pulls.extend(batch)
        if len(batch) < 100:
            return pulls
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Nur zeigen, was gesetzt wuerde."
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print(
            "GITHUB_TOKEN und GITHUB_REPOSITORY muessen gesetzt sein.", file=sys.stderr
        )
        return 2
    owner = repo.split("/")[0]

    try:
        changes = [
            (pull, change)
            for pull in all_pulls(repo, token)
            if (change := missing(pull, owner))
        ]
        for pull, change in changes:
            summary = ", ".join(
                f"{key}={','.join(values)}" for key, values in change.items()
            )
            print(f"#{pull['number']}: {summary}")
            if args.dry_run:
                continue
            # Labels und Assignees laufen bei Pull Requests ueber die Issue-Schnittstelle.
            base = f"/repos/{repo}/issues/{pull['number']}"
            if "assignees" in change:
                rest(
                    "POST",
                    f"{base}/assignees",
                    token,
                    {"assignees": change["assignees"]},
                )
            if "labels" in change:
                rest("POST", f"{base}/labels", token, {"labels": change["labels"]})
    except MetadataError as exc:
        print(f"PR-Metadaten fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    verb = "wuerden ergaenzt" if args.dry_run else "ergaenzt"
    print(f"{len(changes)} Pull Requests {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
