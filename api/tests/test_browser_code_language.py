"""Browser code is TypeScript (CLAUDE.md §8, docs/22, Maxi 10.11.2026).

New logic that runs in the browser is written in `api/gui/ts/` and compiled. The
JavaScript files written by hand before that rule are named here, and the list
only shrinks: a new handwritten script fails, and so does a migrated file that
is still listed.
"""

from pathlib import Path

GUI = Path(__file__).resolve().parents[1] / "gui"
DASHBOARD = GUI.parent / "dashboard"

# Written by hand before the rule; each moves to TypeScript with #626.
NOT_YET_MIGRATED = {"app.js", "base.js", "raumplan.js"}
# Third-party code, shipped as its author built it.
VENDORED = {"htmx.min.js"}
# The simulator console, dev only (#626 decides whether it moves).
INLINE_SCRIPT_ALLOWED = {"dev/console.html"}


def _compiled() -> set[str]:
    return {source.stem + ".js" for source in (GUI / "ts").glob("*.ts")}


def test_every_script_is_compiled_from_typescript_or_on_the_shrinking_list():
    scripts = {path.name for path in (GUI / "static").glob("*.js")}
    handwritten = scripts - _compiled() - VENDORED

    assert handwritten == NOT_YET_MIGRATED, (
        "browser logic belongs in api/gui/ts/ (docs/22); "
        f"new handwritten: {sorted(handwritten - NOT_YET_MIGRATED)}, "
        f"listed but gone or migrated: {sorted(NOT_YET_MIGRATED - handwritten)}"
    )


def test_every_typescript_source_has_its_compiled_file():
    scripts = {path.name for path in (GUI / "static").glob("*.js")}

    assert _compiled() <= scripts


def test_no_template_carries_a_script_of_its_own():
    inline = set()
    for root in (GUI / "templates", DASHBOARD / "templates"):
        for template in root.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            tags = text.split("<script")[1:]
            if any("src=" not in tag.split(">", 1)[0] for tag in tags):
                inline.add(template.relative_to(root).as_posix())

    assert inline == INLINE_SCRIPT_ALLOWED
