"""CI splits the suite over parallel jobs with `PYTEST_SHARD` (#609): together the
shards must run every test exactly once, in parts of about equal size."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from api.tests.conftest import in_shard

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("total", [1, 3, 5])
def test_every_test_id_lands_in_exactly_one_shard(total):
    ids = [f"api/tests/test_x.py::test_{n}[case-{n % 7}]" for n in range(500)]
    for nodeid in ids:
        hits = [index for index in range(total) if in_shard(nodeid, f"{index}/{total}")]
        assert len(hits) == 1, (nodeid, hits)


def test_shards_are_of_about_equal_size():
    ids = [f"api/tests/test_{n % 200}.py::test_{n}" for n in range(9000)]
    sizes = [
        sum(in_shard(nodeid, f"{index}/3") for nodeid in ids) for index in range(3)
    ]
    assert all(2700 <= size <= 3300 for size in sizes), sizes


@pytest.mark.parametrize("spec", ["3/3", "-1/3", "a/3", "1", "0/0"])
def test_a_spec_outside_its_range_is_refused(spec):
    with pytest.raises(ValueError):
        in_shard("api/tests/test_x.py::test_a", spec)


def collected(shard: str | None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_SHARD"}
    if shard:
        env["PYTEST_SHARD"] = shard
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "api/tests/test_ci_shards.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Exit 5: a shard of this small file may hold no test at all.
    assert run.returncode in (0, 5), run.stdout + run.stderr
    return [line for line in run.stdout.splitlines() if "::" in line]


def test_the_shards_of_a_real_collection_add_up_to_the_whole_run():
    whole = collected(None)
    parts = [collected(f"{index}/2") for index in range(2)]
    assert len(whole) > 5
    assert sorted(parts[0] + parts[1]) == sorted(whole)
    assert not set(parts[0]) & set(parts[1])
