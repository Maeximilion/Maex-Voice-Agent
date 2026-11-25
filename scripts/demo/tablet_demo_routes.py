"""Which /gui targets the real pages name, and whether the tablet demo answers them.

The public tablet demo (#892) runs the real templates and the real compiled
TypeScript in the visitor's browser with a fake backend instead of a server.
`tablet_demo_routes.json` says for every route of the view how the demo answers
it. This module reads every `/gui` target out of the templates and the
TypeScript of the view, so `api/tests/test_tablet_demo_routes.py` can fail when
one of them is not answered: the drift rule of docs/06 §11.

    python -m scripts.demo.tablet_demo_routes      # prints what is not answered

It reads text only: no database, no application.
"""

import json
import re
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("tablet_demo_routes.json")
TEMPLATES = ROOT / "api" / "gui" / "templates"
SCRIPTS = ROOT / "api" / "gui" / "ts"
STATIC = "/gui/static/"

# A quoted /gui path in a template, with the attribute it stands in, if any. A
# Jinja expression inside the path may hold spaces and quotes of its own.
TEMPLATE_TARGET = re.compile(
    r"""(?:\b(?P<attr>hx-(?:get|post|put|patch|delete)|href|action|src)\s*=\s*)?"""
    r"""(?P<q>["'])(?P<path>/gui(?:\{\{.*?\}\}|[^"'\s{])*)(?P=q)"""
)
# A whole target computed by Jinja, e.g. hx-post="{{ action }}".
DYNAMIC_TARGET = re.compile(r"""\bhx-(get|post|put|patch|delete)\s*=\s*["']\{\{""")
FORM = re.compile(r"<form\b[^>]*>", re.DOTALL)
FORM_METHOD = re.compile(r"""\bmethod\s*=\s*["'](\w+)["']""")
# A /gui path in TypeScript, with an HTTP method written right before it
# (`htmx.ajax("GET", "/gui/...")`, `send("PUT", "/gui/..." + id)`).
SCRIPT_TARGET = re.compile(
    r"""(?:["'](?P<method>GET|POST|PUT|PATCH|DELETE)["']\s*,\s*)?"""
    r"""(?P<q>["'`])(?P<path>/gui(?:/[^"'`\s]*)?)(?P=q)(?P<plus>\s*\+)?"""
)
JINJA = re.compile(r"\{\{.*?\}\}")
PARAMETER = re.compile(r"\{[^/}]*\}")


@dataclass(frozen=True)
class Target:
    method: str
    path: str  # Jinja expressions and computed parts as "{}"
    source: str  # file and line, for the failure message


@cache
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data


def _normal(path: str) -> str:
    return JINJA.sub("{}", path.split("?", 1)[0])


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _skipped(relative: str, prefixes: list[str]) -> bool:
    return any(relative.startswith(prefix) for prefix in prefixes)


def template_targets(folder: Path = TEMPLATES) -> list[Target]:
    """Every /gui target of the demo's templates; a dynamic one must be listed
    under `dynamic_targets` in the manifest, which names what it can be."""
    data = manifest()
    found: list[Target] = []
    for file in sorted(folder.rglob("*.html")):
        relative = file.relative_to(folder).as_posix()
        if _skipped(relative, data["not_in_demo"]["templates"]):
            continue
        text = file.read_text(encoding="utf-8")
        forms = [(m.start(), m.end(), m.group(0)) for m in FORM.finditer(text)]
        for m in TEMPLATE_TARGET.finditer(text):
            path, attr = m.group("path"), m.group("attr") or ""
            if path.startswith(STATIC):
                continue
            method = "GET"
            if attr.startswith("hx-"):
                method = attr[3:].upper()
            elif attr == "action":
                tag = next((t for s, e, t in forms if s <= m.start() < e), "")
                chosen = FORM_METHOD.search(tag)
                method = chosen.group(1).upper() if chosen else "GET"
            source = f"{relative}:{_line(text, m.start())}"
            found.append(Target(method, _normal(path), source))
        for m in DYNAMIC_TARGET.finditer(text):
            source = f"{relative}:{_line(text, m.start())}"
            listed = data["dynamic_targets"].get(relative)
            if listed is None:
                found.append(Target(m.group(1).upper(), "{}", source))
            else:
                method = m.group(1).upper()
                found.extend(Target(method, _normal(p), source) for p in listed)
    return found


def script_targets(folder: Path = SCRIPTS) -> list[Target]:
    """Every /gui target of the demo's TypeScript. A path followed by `+` gets
    a computed last part ("{}")."""
    skipped = set(manifest()["not_in_demo"]["scripts"])
    found: list[Target] = []
    for file in sorted(folder.glob("*.ts")):
        if file.name in skipped:
            continue
        text = file.read_text(encoding="utf-8")
        for m in SCRIPT_TARGET.finditer(text):
            path = m.group("path")
            if path.startswith(STATIC):
                continue
            if m.group("plus"):
                path += "{}"
            # A template literal's ${...} is a computed part too.
            path = re.sub(r"\$\{[^}]*\}", "{}", path)
            source = f"{file.name}:{_line(text, m.start())}"
            found.append(Target(m.group("method") or "GET", _normal(path), source))
    return found


def pattern(path: str) -> re.Pattern[str]:
    parts = PARAMETER.split(path)
    return re.compile("[^/]+".join(re.escape(p) for p in parts) + "/?")


def answer(target: Target) -> str | None:
    """How the demo answers a target, or None when no route of the manifest
    matches it. A computed part "{}" stands for one path segment."""
    for route in manifest()["routes"]:
        if route["method"] == target.method and pattern(route["path"]).fullmatch(
            target.path
        ):
            return str(route["answer"])
    return None


def unanswered(targets: list[Target]) -> list[str]:
    """One line per target the demo would not answer."""
    problems = []
    for target in targets:
        how = answer(target)
        if how is None:
            problems.append(f"{target.source}: {target.method} {target.path}")
        elif how == "absent":
            problems.append(
                f"{target.source}: {target.method} {target.path} is not in the demo"
            )
    return problems


def main() -> int:
    problems = unanswered(template_targets() + script_targets())
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
