"""Saved workflows in .claude/workflows load as /<name> only with a literal meta block first."""

import os
import re
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[2] / ".claude" / "workflows"
WORKFLOWS = sorted(DIR.glob("*.js"))


def test_workflows_exist():
    # The api container does not mount .claude/workflows (test_compose_mounts.py, CHECKOUT_ONLY).
    if not DIR.is_dir() and not os.environ.get("CI"):
        pytest.skip(".claude/workflows is read on the checkout only")
    assert {p.stem for p in WORKFLOWS} >= {"gate-review", "review-load-audit"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_meta_is_first_literal_and_names_every_phase(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("export const meta = {")
    meta = text[: text.index("\n}\n") + 2]
    # A variable, call, spread or template in meta drops the workflow from / autocomplete.
    assert "`" not in meta and "..." not in meta
    assert not re.search(r"\w\(", meta), "function call in meta"
    assert re.search(rf"name: '{re.escape(path.stem)}'", meta)
    assert re.search(r"description: '[^']+'", meta)
    titles = set(re.findall(r"title: '([^']+)'", meta))
    used = set(re.findall(r"phase\('([^']+)'\)", text)) | set(
        re.findall(r"phase: '([^']+)'", text)
    )
    assert used <= titles, f"phases missing in meta: {used - titles}"
