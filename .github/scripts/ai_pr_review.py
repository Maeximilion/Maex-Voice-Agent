#!/usr/bin/env python3
"""Automatische PR-Analyse mit einem LLM ueber eine OpenAI-kompatible API.

Standard ist der lokale llama-server (Runner-Distro gh-runner, Modell aus der
Repo-Variable REVIEW_MODEL). Liest den Diff eines Pull Requests, laesst ihn
reviewen und stellt das
Ergebnis als GitHub-Review mit Inline-Kommentaren (P0-P3) dar - wie das
Codex-Review-Bot-Beispiel. Nur Standardbibliothek, damit der Runner nichts
zusaetzlich installieren muss.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

MAX_DIFF_CHARS = 150_000
MAX_INLINE_COMMENTS = 20
SEVERITIES = ("P0", "P1", "P2", "P3")

# Endpunkt und Modell sind ueber Environment/Repository-Variablen ueberschreibbar,
# damit kein Workflow-Edit noetig ist, wenn z. B. ein neues Modell kommt.
ZAI_BASE = os.environ.get("ZAI_API_BASE") or "https://api.z.ai/api/paas/v4"
MODEL = os.environ.get("REVIEW_MODEL") or "glm-4.6"
# Qwen3.6 denkt vor der Antwort; bei llama-server landet das in reasoning_content
# und verbraucht max_tokens, bevor das JSON in content kommt. Standard: Denken aus.
# Repo-Variable REVIEW_DISABLE_THINKING=false schaltet es wieder ein.
DISABLE_THINKING = os.environ.get("REVIEW_DISABLE_THINKING", "").lower() != "false"
# Ein lokales Review kann einige Minuten dauern; der Job hat 20 Minuten.
LLM_TIMEOUT_S = 900

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

SYSTEM_PROMPT = """You are a rigorous code reviewer embedded in a GitHub pull request workflow.
Review ONLY what the diff shows. Report real, actionable problems: bugs, logic errors,
security issues, race conditions, breaking API changes, meaningful performance problems,
and clearly wrong or misleading names/comments. Do NOT report style nitpicks, filler
like "consider adding tests", or hypothetical issues you cannot ground in the shown code.
Severity: P0 = critical (breaks functionality or security, must not merge), P1 = likely bug,
P2 = should be fixed before merge, P3 = minor/optional.
Answer with JSON only, no markdown fences:
{"summary": "2-4 sentence overall assessment", "findings": [{"file": "path exactly as in the diff", "line": <line number in the NEW version of the file>, "severity": "P0"|"P1"|"P2"|"P3", "comment": "2-4 sentences, concrete, name the fix"}]}
If the diff has no real problems, return an empty findings list and say so in the summary."""


def parse_diff(text: str) -> dict[str, list[int]]:
    """Datei -> Zeilennummern der Diff-Inhaltszeilen (Rechtsseite, neue Version)."""
    anchors: dict[str, list[int]] = {}
    path: str | None = None
    new_line = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            path = None
            new_line = 0
        elif line.startswith("+++ b/") and line[6:] != "/dev/null":
            path = line[6:]
            anchors.setdefault(path, [])
        elif (m := HUNK_RE.match(line)) and path is not None:
            new_line = int(m.group(1))
        elif path is not None and new_line:
            if line.startswith(("+", " ")):
                anchors[path].append(new_line)
                new_line += 1
            # "-" zaehlt nur die Linksseite, "\" (no newline) gar nicht.
    return anchors


def snap_to_diff(lines: list[int], wanted: int) -> int | None:
    """Naechste gültige Anker-Zeile; LLM-Zeilenangaben sind oft leicht daneben."""
    if not lines:
        return None
    return min(lines, key=lambda n: (abs(n - wanted), n))


def find_file(anchors: dict[str, list[int]], name: str) -> str | None:
    if name in anchors:
        return name
    # Toleranter Fallback: Modell schreibt evtl. a/... oder nur den Dateinamen.
    for candidate in anchors:
        if candidate.endswith(name) or name.endswith(candidate):
            return candidate
    return None


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"kein JSON in der Modellantwort: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def call_llm(pr_title: str, pr_body: str, diff: str) -> dict[str, Any]:
    user_msg = (
        f"Pull request title: {pr_title}\n\n"
        f"Pull request description:\n{pr_body[:4000] or '(none)'}\n\n"
        f"Unified diff to review:\n\n{diff}"
    )
    payload: dict[str, Any] = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 6144,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
    }
    if DISABLE_THINKING:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    for attempt in (0, 1):
        if attempt:  # Aeltere/andere Modelle kennen response_format evtl. nicht.
            payload.pop("response_format", None)
        req = urllib.request.Request(
            f"{ZAI_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                # Der lokale llama-server ignoriert den Bearer-Key; nur der Z.ai-Fallback
                # braucht das Secret ZAI_API_KEY, daher hier tolerant bleiben.
                "Authorization": f"Bearer {os.environ.get('ZAI_API_KEY') or 'local'}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                body = json.load(resp)
            return extract_json(body["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if attempt == 0 and exc.code == 400:
                continue
            raise RuntimeError(f"LLM-API HTTP {exc.code}: {detail}") from exc
    raise RuntimeError("LLM-API nicht erreichbar")


def post_review(summary: str, comments: list[dict[str, Any]]) -> None:
    """Review mit Inline-Kommentaren posten; Rueckfalloption: normaler PR-Kommentar."""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ["GITHUB_REPOSITORY"]
    number = os.environ["PR_NUMBER"]
    headers = {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    def request(path: str, data: dict[str, Any]) -> urllib.error.HTTPError | None:
        req = urllib.request.Request(
            f"{api}/repos/{repo}/{path}",
            data=json.dumps(data).encode(),
            headers=headers,
        )
        try:
            urllib.request.urlopen(req, timeout=60)
            return None
        except urllib.error.HTTPError as exc:
            return exc

    err = request(
        f"pulls/{number}/reviews",
        {"body": summary, "event": "COMMENT", "comments": comments},
    )
    if err is None:
        return
    print(
        f"::warning::Review-Posting fehlgeschlagen ({err.code}), Rueckfalloption PR-Kommentar",
        file=sys.stderr,
    )
    err2 = request(f"issues/{number}/comments", {"body": summary})
    if err2 is not None:
        raise RuntimeError(
            f"GitHub-API HTTP {err2.code}: {err2.read().decode(errors='replace')[:500]}"
        )


def main() -> int:
    diff_file = os.environ.get("DIFF_FILE", "pr.diff")
    try:
        with open(diff_file, encoding="utf-8", errors="replace") as fh:
            diff = fh.read()
    except OSError as exc:
        raise RuntimeError(f"Diff-Datei {diff_file} nicht lesbar: {exc}") from exc

    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"
    anchors = parse_diff(diff)

    result = call_llm(
        os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), diff
    )
    findings = result.get("findings") or []
    summary_text = str(result.get("summary", "")).strip() or "(no summary)"

    inline: list[dict[str, Any]] = []
    unanchored: list[str] = []
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev = str(f.get("severity", "P3")).upper()[:2]
        if sev not in SEVERITIES:
            sev = "P3"
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
        body = f"**[{sev}]** {f.get('comment', '').strip()}"
        if len(inline) >= MAX_INLINE_COMMENTS:
            unanchored.append(body)
            continue
        file_key = find_file(anchors, str(f.get("file", "")))
        line = (
            snap_to_diff(anchors[file_key], int(f.get("line", 0) or 0))
            if file_key
            else None
        )
        if file_key and line:
            inline.append({"path": file_key, "line": line, "body": body})
        else:
            unanchored.append(body)

    count_line = (
        " · ".join(f"{sev_counts[s]}x {s}" for s in SEVERITIES if sev_counts.get(s))
        or "no findings"
    )
    parts = [f"## AI Review (`{MODEL}`)\n\n{summary_text}\n\n**{count_line}**"]
    if truncated:
        parts.append(
            "Hinweis: Der Diff war größer als das Limit und wurde gekürzt — Befunde können unvollständig sein."
        )
    if unanchored:
        parts.append(
            "<details><summary>Weitere Befunde ohne Zeilenanker</summary>\n\n"
            + "\n\n".join(f"- {b}" for b in unanchored)
            + "\n</details>"
        )
    parts.append(
        "<sub>Automated review via GitHub Actions + local LLM. Suggestions may be wrong.</sub>"
    )
    summary_md = "\n\n".join(parts)

    if inline or unanchored:
        post_review(summary_md, inline)
    else:
        # Ohne Befunde steht die Zusammenfassung trotzdem als Kommentar im PR.
        post_review(summary_md, [])
    print(f"Review gepostet: {len(inline)} inline, {len(unanchored)} im Summary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
