#!/usr/bin/env python3
"""Offene Befunde der Projektpflege in genau einem Issue sammeln.

Zweck : Die taegliche Projektpflege korrigiert, was sie darf, und meldet den Rest in
        der Job-Zusammenfassung - die liest niemand von selbst. Dieses Skript pflegt
        daraus ein einziges Issue: anlegen, wenn etwas offen ist, taeglich
        aktualisieren, schliessen, wenn nichts mehr offen ist. Erwaehnt wird nur bei
        neuen Befunden, nicht jeden Tag fuer dieselben.
Aufruf: python scripts/decision_issue.py BEFUNDE.json [--dry-run]
        BEFUNDE.json schreibt scripts/project_report.py --findings-json.
Umgeb.: GITHUB_TOKEN       der eingebaute Token der Action. Bewusst nicht der PAT:
                           eine Erwaehnung vom eigenen Konto loest bei GitHub keine
                           Benachrichtigung aus, eine von github-actions schon.
        GITHUB_REPOSITORY  owner/name, in einer Action automatisch gesetzt
        DECISION_MENTION   wer erwaehnt wird, Vorgabe @<Repo-Eigentuemer>
Abhaeng.: nur Standardbibliothek.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.github.com"
LABEL = "projektpflege"
TITLE = "Projektpflege: offene Entscheidungen"
INTRO = "Die taegliche Projektpflege hat Punkte, die sie nicht selbst entscheiden kann."
# Die Schluessel der gemeldeten Befunde stehen unsichtbar im Issue. Nur so erkennt
# der naechste Lauf, was neu ist, ohne eigenen Speicher.
KEYS_MARKER = re.compile(r"<!-- projektpflege-keys: ([0-9a-f,]*) -->")


class IssueError(RuntimeError):
    """Das Issue liess sich nicht lesen oder schreiben."""


@dataclass
class Plan:
    """Was mit dem Issue passieren soll. action: none, create, update, reopen oder close."""

    action: str
    body: str | None = None
    comment: str | None = None


def finding_keys(findings: dict[str, list[dict]]) -> dict[str, str]:
    """Stabiler Schluessel je Befund und Eintrag, Wert ist die Anzeigezeile.

    Gehasht, weil Titel beliebigen Text enthalten und den HTML-Kommentar sprengen
    koennten. Die URL traegt die Identitaet, bei Entwuerfen ohne URL das Label.
    """
    keys = {}
    for heading, entries in findings.items():
        for entry in entries:
            identity = f"{heading}|{entry.get('url') or entry['label']}"
            key = hashlib.sha1(identity.encode()).hexdigest()[:12]
            keys[key] = f"{heading}: {entry['label']}"
    return keys


def parse_keys(body: str | None) -> set[str]:
    match = KEYS_MARKER.search(body or "")
    return set(filter(None, match.group(1).split(","))) if match else set()


def render_body(findings: dict[str, list[dict]], intro: str) -> str:
    lines = [intro, ""]
    for heading, entries in findings.items():
        lines.append(f"### {heading} ({len(entries)})")
        lines.append("")
        for entry in entries:
            link = (
                f"[{entry['label']}]({entry['url']})"
                if entry.get("url")
                else entry["label"]
            )
            lines.append(f"- [ ] {link}")
        lines.append("")
    lines.append(
        "Erledigen ueber `/project` oder direkt am Board. Das Issue aktualisiert sich "
        "taeglich und schliesst sich, sobald nichts mehr offen ist."
    )
    keys = ",".join(sorted(finding_keys(findings)))
    lines.append(f"<!-- projektpflege-keys: {keys} -->")
    return "\n".join(lines)


def plan(
    existing_body: str | None,
    findings: dict[str, list[dict]],
    mention: str,
    closed: bool = False,
) -> Plan:
    """Die ganze Entscheidung, ohne Netz.

    existing_body=None heisst: es gab nie ein Issue. closed=True heisst: es gibt eins,
    aber es ist zu. Dann wird es wieder geoeffnet statt neu angelegt - sonst sammelte
    jeder Zyklus aus "erledigt" und "wieder etwas offen" ein Issue mehr.
    """
    if not findings:
        if existing_body is None or closed:
            return Plan("none")
        return Plan(
            "close", comment="Alle Punkte sind erledigt. Das Issue schliesst sich."
        )

    if closed:
        # Beim Schliessen galt alles als erledigt. Was wiederkommt, ist wieder neu und
        # wird erwaehnt, auch wenn sein Schluessel noch im alten Text steht.
        current = finding_keys(findings)
        listing = "\n".join(f"- {current[key]}" for key in sorted(current))
        return Plan(
            "reopen",
            body=render_body(findings, INTRO),
            comment=f"{mention} Wieder offen:\n\n{listing}",
        )

    if existing_body is None:
        # Nur beim Anlegen steht die Erwaehnung im Text. Bei spaeteren Aenderungen des
        # Texts nicht - wer bei jeder Aktualisierung erwaehnt wird, liest bald gar nichts mehr.
        return Plan("create", body=render_body(findings, f"{mention} {INTRO}"))

    body = render_body(findings, INTRO)
    current = finding_keys(findings)
    new = sorted(key for key in current if key not in parse_keys(existing_body))
    if new:
        listing = "\n".join(f"- {current[key]}" for key in new)
        return Plan(
            "update", body=body, comment=f"{mention} Neu dazugekommen:\n\n{listing}"
        )
    if body != existing_body:
        return Plan("update", body=body)
    return Plan("none")


def rest(
    method: str, path: str, token: str, payload: dict | None = None
) -> dict | list:
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "maex-voice-agent-decision-issue",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise IssueError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise IssueError(f"GitHub nicht erreichbar: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def apply(repo: str, token: str, issue: dict | None, todo: Plan) -> str:
    base = f"/repos/{repo}"
    if todo.action == "none":
        return "Issue unveraendert."
    if todo.action == "create":
        try:
            rest("POST", f"{base}/labels", token, {"name": LABEL, "color": "ededed"})
        except IssueError as exc:
            # 422: das Label gibt es schon. Jeder andere Fehler ist echt.
            if "422" not in str(exc):
                raise
        created = rest(
            "POST",
            f"{base}/issues",
            token,
            {"title": TITLE, "body": todo.body, "labels": [LABEL]},
        )
        return f"Issue angelegt: {created['html_url']}"

    assert issue is not None
    number = issue["number"]
    # Erst erwaehnen, dann den Text schreiben. Der Text traegt die Schluessel dessen, was
    # als gemeldet gilt. Schrieben wir ihn zuerst und scheiterte danach der Kommentar,
    # hielte der naechste Lauf die Punkte fuer gemeldet und erwaehnte nie. So herum
    # gibt es im Fehlerfall hoechstens eine doppelte Erwaehnung - die kleinere Panne.
    if todo.comment:
        rest("POST", f"{base}/issues/{number}/comments", token, {"body": todo.comment})
    if todo.body is not None:
        update: dict = {"body": todo.body}
        if todo.action == "reopen":
            update["state"] = "open"
        rest("PATCH", f"{base}/issues/{number}", token, update)
    if todo.action == "close":
        rest(
            "PATCH",
            f"{base}/issues/{number}",
            token,
            {"state": "closed", "state_reason": "completed"},
        )
        return f"Issue #{number} geschlossen."
    return f"Issue #{number} aktualisiert" + (
        " mit Erwaehnung." if todo.comment else "."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("findings", help="JSON von project_report.py --findings-json")
    parser.add_argument(
        "--dry-run", action="store_true", help="Nur zeigen, was passieren wuerde."
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or (not token and not args.dry_run):
        print(
            "GITHUB_TOKEN und GITHUB_REPOSITORY muessen gesetzt sein.", file=sys.stderr
        )
        return 2
    mention = os.environ.get("DECISION_MENTION") or f"@{repo.split('/')[0]}"

    with open(args.findings, encoding="utf-8") as handle:
        findings = json.load(handle)

    try:
        issues = (
            rest(
                "GET",
                # state=all: auch ein geschlossenes Issue wird wiedergefunden und wieder
                # geoeffnet. Neueste zuerst, damit genau eines gilt.
                f"/repos/{repo}/issues?state=all&labels={LABEL}&sort=created&direction=desc&per_page=5",
                token,
            )
            if token
            else []
        )
        # Pull Requests tragen dieselbe Liste, ein Issue hat kein pull_request-Feld.
        issue = next((i for i in issues if "pull_request" not in i), None)
        # Ein Issue mit leerem Text liefert body = null. Das ist nicht dasselbe wie
        # "kein Issue" und darf nicht zu einem zweiten Issue fuehren.
        todo = plan(
            (issue.get("body") or "") if issue else None,
            findings,
            mention,
            closed=bool(issue) and issue.get("state") == "closed",
        )
        if args.dry_run:
            print(f"Geplant: {todo.action}")
            if todo.comment:
                print(f"Kommentar:\n{todo.comment}")
            return 0
        print(apply(repo, token, issue, todo))
    except IssueError as exc:
        print(f"Entscheidungs-Issue fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
