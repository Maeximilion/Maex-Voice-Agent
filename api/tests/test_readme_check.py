"""The structural README checks (`scripts/readme_check.py`)."""

from __future__ import annotations

from pathlib import Path

from scripts import readme_check as rc

REPO = Path(__file__).resolve().parents[2]
CLEAN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<defs><linearGradient id="g1"/></defs><rect fill="url(#g1)"/><use xlink:href="#g1"/></svg>'
)


def make_tree(tmp_path: Path, readme: str, **files: str | bytes) -> Path:
    """A small repository: README.md plus files given by path (`/` written as `__`)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    for name, content in files.items():
        path = tmp_path / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return tmp_path


def test_clean_tree_passes(tmp_path: Path) -> None:
    """Links, images, anchors and a harmless SVG produce no finding."""
    readme = (
        "# Title\n\n## How a call flows\n\n"
        '<img src=".github/assets/a.svg" alt="a">\n'
        "[flow](#how-a-call-flows) [doc](docs/x.md#setup) ![i](.github/assets/a.svg)\n"
        "[web](https://example.com/missing) [mail](mailto:a@example.com)\n"
    )
    root = make_tree(
        tmp_path,
        readme,
        **{".github__assets__a.svg": CLEAN_SVG, "docs__x.md": "# Setup\n"},
    )
    assert rc.run(root) == []


def test_missing_image_is_reported_with_line(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "# T\n\n<img src='.github/assets/none.svg'>\n")
    assert rc.run(root) == ["README.md:3: missing file .github/assets/none.svg"]


def test_broken_anchor_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "# T\n\n[x](#nowhere)\n")
    assert rc.run(root) == ["README.md:3: broken anchor #nowhere"]


def test_duplicate_heading_slugs_get_suffixes(tmp_path: Path) -> None:
    """The second heading of the same name is `-1`; `-2` does not exist yet."""
    root = make_tree(
        tmp_path, "# Setup\n\n## Setup\n\n[a](#setup) [b](#setup-1) [c](#setup-2)\n"
    )
    assert rc.run(root) == ["README.md:5: broken anchor #setup-2"]


def test_slug_drops_punctuation_and_inline_markup() -> None:
    assert rc.slugify("What works (v1.2) & `code`!") == "what-works-v12--code"


def test_headings_in_code_fences_are_not_anchors(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "# T\n\n```\n# Hidden\n```\n\n[x](#hidden)\n")
    assert rc.run(root) == ["README.md:7: broken anchor #hidden"]


def test_anchor_into_another_file(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path,
        "# T\n\n[ok](docs/x.md#real) [bad](docs/x.md#fake)\n",
        **{"docs__x.md": "# Real\n"},
    )
    assert rc.run(root) == ["README.md:3: broken anchor #fake in docs/x.md"]


def test_links_in_code_are_ignored(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path, "# T\n\nUse `[x](gone.md)` here.\n\n```\n[y](gone2.md)\n```\n"
    )
    assert rc.run(root) == []


def test_svg_with_script_is_reported(tmp_path: Path) -> None:
    svg = '<svg xmlns="http://www.w3.org/2000/svg">\n<script>alert(1)</script></svg>'
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": svg})
    assert rc.run(root) == [".github/assets/a.svg:2: <script> in SVG"]


def test_svg_with_external_href_and_event_attribute(tmp_path: Path) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="https://evil.example/a.png"/><rect onclick="x()"/></svg>'
    )
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": svg})
    messages = rc.run(root)
    assert any("external href https://evil.example/a.png" in m for m in messages)
    assert any("event attribute onclick" in m for m in messages)


def test_svg_with_external_css_url_and_foreign_object(tmp_path: Path) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://x.example/a.css";'
        "</style><rect style=\"fill:url('//x.example/p')\"/><foreignObject/></svg>"
    )
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": svg})
    messages = rc.run(root)
    assert any("external reference https://x.example/a.css" in m for m in messages)
    assert any("external reference //x.example/p" in m for m in messages)
    assert any("<foreignObject>" in m for m in messages)


def test_svg_with_only_internal_references_passes(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": CLEAN_SVG})
    assert rc.run(root) == []


def test_svg_with_data_html_href_is_reported(tmp_path: Path) -> None:
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><a href="data:text/html,x"/></svg>'
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": svg})
    assert len(rc.run(root)) == 1


def test_malformed_svg_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": "<svg><g></svg>"})
    assert "not well-formed" in rc.run(root)[0]


def test_oversized_svg_gif_and_png(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rc, "MAX_SVG_BYTES", 10)
    monkeypatch.setattr(rc, "MAX_GIF_BYTES", 10)
    monkeypatch.setattr(rc, "MAX_PNG_BYTES", 10)
    files: dict[str, str | bytes] = {
        ".github__assets__a.svg": CLEAN_SVG,
        ".github__assets__a.gif": b"x" * 11,
        ".github__assets__a.png": b"x" * 11,
        ".github__assets__ok.gif": b"x" * 10,
    }
    root = make_tree(
        tmp_path,
        "# T\n",
        **files,
    )
    messages = rc.run(root)
    assert [m.split(": ")[1].split(" larger")[0] for m in messages] == [
        "GIF",
        "PNG",
        "SVG",
    ]


def test_main_exit_codes(tmp_path: Path, capsys) -> None:
    clean = make_tree(tmp_path / "clean", "# T\n")
    assert rc.main(["--root", str(clean)]) == 0
    broken = make_tree(tmp_path / "broken", "[x](#nope)\n")
    assert rc.main(["--root", str(broken)]) == 1
    assert capsys.readouterr().out == "README.md:1: broken anchor #nope\n"


def test_real_repository_passes() -> None:
    assert rc.run(REPO) == []


# --- blocked terms ----------------------------------------------------------------


def test_a_blocked_term_is_found_without_being_printed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        rc.BLOCKLIST_ENV, "# real names\nGasthaus Beispiel\n\nMusterdorf\n"
    )
    root = make_tree(
        tmp_path,
        "# T\n\nWelcome to gasthaus beispiel.\n",
        **{
            ".github__assets__a.svg": CLEAN_SVG.replace(
                "</svg>", "<text>MUSTERDORF</text></svg>"
            )
        },
    )
    messages = rc.run(root)
    assert "README.md:3: a blocked term (entry 1 of the list)" in messages
    assert any(
        m.startswith(".github/assets/a.svg:") and "entry 2" in m for m in messages
    )
    assert not any(
        "beispiel" in m.casefold() or "musterdorf" in m.casefold() for m in messages
    )


def test_the_local_list_counts_like_the_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(rc.BLOCKLIST_ENV, raising=False)
    root = make_tree(
        tmp_path, "# T\n\nMusterdorf\n", **{".readme_blocklist": "musterdorf\n"}
    )
    assert rc.run(root) == ["README.md:3: a blocked term (entry 1 of the list)"]


def test_without_a_list_the_scan_is_skipped_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv(rc.BLOCKLIST_ENV, raising=False)
    root = make_tree(tmp_path, "# T\n\nMusterdorf\n")
    assert rc.main(["--root", str(root)]) == 0
    assert "scan skipped" in capsys.readouterr().err


def test_an_svg_that_points_at_a_local_file_is_refused(tmp_path: Path) -> None:
    svg = CLEAN_SVG.replace("</svg>", '<image href="file:///C:/x.png"/></svg>')
    root = make_tree(tmp_path, "# T\n", **{".github__assets__a.svg": svg})
    assert any("a.svg" in message for message in rc.run(root))
