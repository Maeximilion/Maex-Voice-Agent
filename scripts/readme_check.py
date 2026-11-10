#!/usr/bin/env python3
"""Hard structural checks for the README and its images (stdlib only).

Checks, each finding printed as `path:line: what is wrong`, exit 1 when any:

- every local link or image the README references exists (`src`, `srcset`, `href`,
  Markdown links and images; `http(s):` and `mailto:` are not followed);
- every `#anchor` resolves to a heading (GitHub slug rules), in the README itself or,
  for `file.md#anchor`, in that Markdown file;
- every SVG in `.github/assets/` is inert (no script, no foreignObject, no event
  attributes, no external reference) and small; every GIF and PNG there is within its limit.

Usage:
    python scripts/readme_check.py [--root <dir>]
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Size limits for the images in .github/assets/; the README is the first thing a visitor loads.
MAX_SVG_BYTES = 150 * 1024  # the largest generated SVG is about 59 KB
MAX_GIF_BYTES = 3 * 1024 * 1024  # animated GIFs are the heavy kind
MAX_PNG_BYTES = 1024 * 1024  # logo and social preview only
# Schemes that never point at a repository file.
EXTERNAL_SCHEMES = ("http", "https", "mailto", "tel")
# Embedded images may stay inline in an SVG; any other data: URL could carry script.
SAFE_DATA_PREFIXES = (
    "data:image/png",
    "data:image/jpeg",
    "data:image/gif",
    "data:image/webp",
)
# Prefixes that make an SVG reference external or executable.
UNSAFE_REF_PREFIXES = ("http:", "https:", "//", "javascript:", "vbscript:")

_ATTR_RE = re.compile(r"""\b(src|srcset|href)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# Matches from the closing bracket so a linked image `[![a](b)](c)` yields both targets.
_MD_LINK_RE = re.compile(r"""\]\(\s*<?([^)\s>]+)>?(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)""")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ID_RE = re.compile(r"""\b(?:id|name)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_URL_FN_RE = re.compile(r"""url\(\s*['"]?\s*([^)'"\s]*)""", re.I)
_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?['"]?\s*([^)'";\s]*)""", re.I)


def _blank(text: str, pattern: str, flags: int = 0) -> str:
    """Replace matches by spaces, keeping newlines so line numbers stay right."""
    return re.sub(
        pattern, lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=flags
    )


def _strip_code(text: str) -> str:
    """Drop fenced blocks, inline code and HTML comments: links inside are not links."""
    out: list[str] = []
    fence = ""
    for line in text.split("\n"):
        marker = line.lstrip()[:3]
        if fence:
            if marker == fence:
                fence = ""
            out.append("")
        elif marker in ("```", "~~~"):
            fence = marker
            out.append("")
        else:
            out.append(line)
    cleaned = "\n".join(out)
    cleaned = _blank(cleaned, r"<!--.*?-->", re.S)
    return _blank(cleaned, r"`[^`\n]*`")


def slugify(heading: str) -> str:
    """GitHub's anchor for a heading, before the `-1`, `-2` suffix for duplicates."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links keep their text
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_of(text: str) -> set[str]:
    """All anchors a Markdown text offers: heading slugs and explicit HTML ids or names."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    cleaned = _strip_code(text)
    for line in cleaned.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            slug = slugify(m.group(2))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            found.add(slug if n == 0 else f"{slug}-{n}")
    for m in _ID_RE.finditer(cleaned):
        found.add((m.group(1) or m.group(2)).lower())
    return found


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _targets(text: str) -> Iterator[tuple[int, str]]:
    """Yield (line, target) for every link, image and srcset entry."""
    cleaned = _strip_code(text)
    for m in _ATTR_RE.finditer(cleaned):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        if m.group(1).lower() == "srcset":
            for part in value.split(","):
                if part.strip():
                    yield _line(cleaned, m.start()), part.split()[0]
        else:
            yield _line(cleaned, m.start()), value.strip()
    for m in _MD_LINK_RE.finditer(cleaned):
        yield _line(cleaned, m.start()), m.group(1)


def check_links(root: Path, readme: Path) -> list[str]:
    findings: list[str] = []
    rel = readme.relative_to(root).as_posix()
    text = readme.read_text(encoding="utf-8")
    anchor_cache: dict[Path, set[str]] = {readme: anchors_of(text)}
    for line, target in _targets(text):
        parts = urlsplit(target)
        if not target or parts.scheme in EXTERNAL_SCHEMES:
            continue
        path_part, fragment = unquote(parts.path), unquote(parts.fragment)
        if not path_part:
            dest = readme
        else:
            base = root if path_part.startswith("/") else readme.parent
            dest = (base / path_part.lstrip("/")).resolve()
            if not dest.exists():
                findings.append(f"{rel}:{line}: missing file {path_part}")
                continue
        if fragment and dest.is_file() and dest.suffix.lower() == ".md":
            if dest not in anchor_cache:
                anchor_cache[dest] = anchors_of(dest.read_text(encoding="utf-8"))
            if fragment.lower() not in anchor_cache[dest]:
                where = "" if dest == readme else f" in {path_part}"
                findings.append(f"{rel}:{line}: broken anchor #{fragment}{where}")
    return findings


def _unsafe_ref(value: str) -> bool:
    v = value.strip().lower()
    if not v or v.startswith("#"):
        return False
    if v.startswith("data:"):
        return not v.startswith(SAFE_DATA_PREFIXES)
    return v.startswith(UNSAFE_REF_PREFIXES)


def _css_refs(css: str, rel: str, at: Callable[[str], int]) -> list[str]:
    refs = [*_URL_FN_RE.findall(css), *_IMPORT_RE.findall(css)]
    return [
        f"{rel}:{at(r)}: external reference {r[:60]}" for r in refs if _unsafe_ref(r)
    ]


def check_svg(path: Path, rel: str) -> list[str]:
    raw = path.read_text(encoding="utf-8")

    def at(needle: str) -> int:
        idx = raw.lower().find(needle.lower()) if needle else -1
        return _line(raw, idx) if idx >= 0 else 1

    findings: list[str] = []
    if len(raw.encode("utf-8")) > MAX_SVG_BYTES:
        findings.append(f"{rel}:1: SVG larger than {MAX_SVG_BYTES // 1024} KB")
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [
            *findings,
            f"{rel}:{exc.position[0]}: SVG is not well-formed XML ({exc})",
        ]
    for el in tree.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower() if isinstance(el.tag, str) else ""
        if tag == "script":
            findings.append(f"{rel}:{at('<script')}: <script> in SVG")
        elif tag == "foreignobject":
            findings.append(f"{rel}:{at('<foreignObject')}: <foreignObject> in SVG")
        elif tag == "style" and el.text:
            findings += _css_refs(el.text, rel, at)
        for name, value in el.attrib.items():
            local = name.rsplit("}", 1)[-1].lower()
            if local.startswith("on"):
                findings.append(f"{rel}:{at(local)}: event attribute {local} in SVG")
            elif local == "href":
                if _unsafe_ref(value):
                    findings.append(
                        f"{rel}:{at(value[:40])}: external href {value[:60]}"
                    )
            else:
                findings += _css_refs(value, rel, at)
    return findings


def check_assets(root: Path) -> list[str]:
    findings: list[str] = []
    assets = root / ".github" / "assets"
    if not assets.is_dir():
        return findings
    limits = {".gif": MAX_GIF_BYTES, ".png": MAX_PNG_BYTES}
    for path in sorted(assets.iterdir()):
        rel = path.relative_to(root).as_posix()
        ext = path.suffix.lower()
        if ext == ".svg":
            findings += check_svg(path, rel)
        elif ext in limits and path.stat().st_size > limits[ext]:
            findings.append(
                f"{rel}:1: {ext[1:].upper()} larger than {limits[ext] // 1024} KB"
            )
    return findings


def run(root: Path) -> list[str]:
    readme = root / "README.md"
    if not readme.is_file():
        return ["README.md:1: README.md not found"]
    return [*check_links(root, readme), *check_assets(root)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Structural checks for the README and its images"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    args = parser.parse_args(argv)
    findings = run(args.root.resolve())
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
