"""The floor plan editor's geometry in node (T-3.11): seat rule, upright labels, copies."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GEOMETRY = ROOT / "api" / "gui" / "static" / "raumplan-geometry.js"


def test_the_seat_rule_and_upright_labels_hold_in_the_editor():
    node = shutil.which("node")
    if not node:
        # The api image carries no node; CI's runner does, and must run this.
        if os.environ.get("CI"):
            pytest.fail("the floor plan geometry check needs node")
        pytest.skip("the floor plan geometry check needs node")
    out = subprocess.run(
        [node, str(Path(__file__).with_name("floor_plan_geometry.mjs")), str(GEOMETRY)],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
