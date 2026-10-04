"""Eval suite v2: size, difficulty levels and mandatory coverage from docs/08, no database.

Two folders (docs/08 §1, Maxi 03.10.2026): `evals/cases/` runs in CI and must be green
(level 1-2). `evals/targets/` holds level 3-5, delivery and known gaps and runs with
`make eval-targets`. Whether the CI cases are green is checked by
`test_evals_runner.py` with a full run. This file checks that the suite stays complete
and that every target case is at least valid, although CI does not play it.
"""

import csv
import json
import re
from pathlib import Path

import pytest

from evals import runner
from evals.judge import validate_case

CASES = runner.CASES
TARGETS = runner.HERE / "targets"
DOCS_08 = Path(runner.__file__).parents[1] / "docs" / "08_EVALS.md"
MIN_CI = 15
MIN_TOTAL = 40
LEVEL = re.compile(r"^level-([1-5])$")


def _cases(folder: Path) -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(folder.glob("*.json"))
    ]


def _all() -> list[tuple[Path, dict]]:
    return _cases(CASES) + _cases(TARGETS)


def _level(case: dict) -> int:
    levels = [int(m.group(1)) for t in case["tags"] if (m := LEVEL.match(t))]
    assert len(levels) == 1, f"{case['id']}: exactly one tag level-1 to level-5"
    return levels[0]


def test_suite_size():
    assert len(_cases(CASES)) >= MIN_CI
    assert len(_all()) >= MIN_TOTAL


def _mandatory() -> dict[str, str]:
    """docs/08 §6 is the source: mandatory case -> tag from the table."""
    doc = DOCS_08.read_text(encoding="utf-8")
    section = doc.split("## 6. Pflichtabdeckung", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| (.+?) \| `([a-z_]+)` \|$", section, re.MULTILINE)
    return dict(rows)


def test_mandatory_table_is_readable():
    # docs/08 §6 lists 18 mandatory cases; a broken table would otherwise be empty.
    assert len(_mandatory()) >= 18


def test_every_mandatory_case_has_a_case():
    """In cases/ or targets/: zone and minimum order value only exist with T-6.5,
    they are ready as target cases."""
    tags = {tag for _, case in _all() for tag in case.get("tags", [])}
    missing = [line for line, tag in _mandatory().items() if tag not in tags]
    assert missing == [], f"docs/08 §6 without a case: {missing}"


def test_file_name_matches_id():
    for path, case in _all():
        assert path.stem.startswith(case["id"] + "_"), path.name


def test_ids_are_unique_across_both_folders():
    """A target case moves to cases/ later; its id must not exist there already."""
    ids = [case["id"] for _, case in _all()]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_ci_suite_holds_only_level_one_and_two():
    for path, case in _cases(CASES):
        assert _level(case) <= 2, path.name


def test_target_case_is_hard_delivery_or_known_gap():
    for path, case in _cases(TARGETS):
        assert _level(case) >= 3 or "lieferung" in case["tags"] or "pending" in case, (
            path.name
        )


def test_known_gap_names_its_task():
    for path, case in _all():
        if "pending" in case:
            assert re.search(r"\bT-\d+\.\d+\b", case["pending"]), path.name


def test_open_gaps_stay_a_minority():
    """The CI suite measures mostly what works today: at most every tenth case may
    be a known gap, otherwise it hides regressions."""
    cases = [case for _, case in _cases(CASES)]
    gaps = sum(1 for case in cases if "pending" in case)
    assert gaps * 10 <= len(cases), gaps


def test_delivery_target_with_address_names_the_postal_code():
    """Codex PR #162: check_delivery requires the postal code and the dialogue asks
    for it first (docs/04, docs/05). A target without one would be unreachable for
    T-6.5."""
    for path, case in _cases(TARGETS):
        address = case["expected"].get("address")
        if address is not None:
            assert "postal_code" in address, path.name


def _menu_numbers() -> set[str]:
    with (runner.MENU / "menu_items.csv").open(encoding="utf-8") as f:
        return {row["number"].lower() for row in csv.DictReader(f, delimiter=";")}


def _menu_options() -> dict[str, set[str]]:
    with (runner.MENU / "item_options.csv").open(encoding="utf-8") as f:
        options: dict[str, set[str]] = {}
        for row in csv.DictReader(f, delimiter=";"):
            options.setdefault(row["number"].lower(), set()).add(row["option_name"])
        return options


@pytest.mark.parametrize("path", sorted(TARGETS.glob("*.json")), ids=lambda p: p.stem)
def test_target_case_is_valid(path):
    """CI does not play targets/; a broken target would only show up with T-2.4."""
    case = json.loads(path.read_text(encoding="utf-8"))
    validate_case(case, path.name)
    numbers = _menu_numbers()
    wanted = [i["number"] for i in case["expected"].get("items", [])]
    wanted += case.get("sold_out", [])
    assert [n for n in wanted if n.lower() not in numbers] == [], path.name
    # Review PR #162: an option the dish does not have makes the target unreachable
    # ("Erdnuss" instead of "Erdnusssauce").
    options = _menu_options()
    unknown = [
        (i["number"], o)
        for i in case["expected"].get("items", [])
        for o in i.get("options", [])
        if o not in options.get(i["number"].lower(), set())
    ]
    assert unknown == [], path.name


def test_each_case_folder_has_its_own_baseline():
    """The regression rule compares with the last passing run; a run over targets/
    must never become the baseline of the CI suite (and vice versa)."""
    assert runner.default_report_dir(CASES) == runner.REPORTS
    assert runner.default_report_dir(TARGETS) == runner.REPORTS / "targets"
    relative = Path("evals") / "targets"
    assert runner.default_report_dir(TARGETS.parent / ".." / relative) == (
        runner.REPORTS / "targets"
    )
