"""Eval-Suite v2: Umfang, Stufen und Pflichtabdeckung aus docs/08, ohne Datenbank.

Zwei Ordner (docs/08 §1, Maxi 03.10.2026): `evals/cases/` laeuft in CI und muss gruen
sein (Stufe 1-2), `evals/ziel/` haelt Stufe 3-5, Lieferung und bekannte Luecken und
laeuft per `make eval-ziel`. Ob die CI-Faelle gruen sind, prueft
`test_evals_runner.py` mit dem ganzen Lauf. Hier steht, dass die Suite vollstaendig
bleibt und jeder Zielfall wenigstens gueltig ist, obwohl CI ihn nicht abspielt.
"""

import csv
import json
import re
from pathlib import Path

import pytest

from evals import runner
from evals.judge import validate_case

CASES = runner.CASES
ZIEL = runner.HERE / "ziel"
DOCS_08 = Path(runner.__file__).parents[1] / "docs" / "08_EVALS.md"
MIN_CI = 15
MIN_TOTAL = 40
LEVEL = re.compile(r"^schwer-([1-5])$")


def _cases(folder: Path) -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(folder.glob("*.json"))
    ]


def _all() -> list[tuple[Path, dict]]:
    return _cases(CASES) + _cases(ZIEL)


def _level(case: dict) -> int:
    levels = [int(m.group(1)) for t in case["tags"] if (m := LEVEL.match(t))]
    assert len(levels) == 1, f"{case['id']}: genau ein Tag schwer-1 bis schwer-5"
    return levels[0]


def test_umfang():
    assert len(_cases(CASES)) >= MIN_CI
    assert len(_all()) >= MIN_TOTAL


def _pflicht() -> dict[str, str]:
    """docs/08 §6 ist die Quelle: Pflichtfall -> Tag aus der Tabelle."""
    doc = DOCS_08.read_text(encoding="utf-8")
    section = doc.split("## 6. Pflichtabdeckung", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| (.+?) \| `([a-z_]+)` \|$", section, re.MULTILINE)
    return dict(rows)


def test_pflichttabelle_ist_lesbar():
    # 18 Pflichtfaelle stehen in docs/08 §6; eine kaputte Tabelle waere sonst leer.
    assert len(_pflicht()) >= 18


def test_jeder_pflichtfall_hat_einen_fall():
    """In cases/ oder ziel/: Zone und Mindestbestellwert gibt es erst mit T-6.5,
    sie stehen als Zielfaelle bereit."""
    tags = {tag for _, case in _all() for tag in case.get("tags", [])}
    missing = [line for line, tag in _pflicht().items() if tag not in tags]
    assert missing == [], f"docs/08 §6 ohne Fall: {missing}"


def test_dateiname_passt_zur_id():
    for path, case in _all():
        assert path.stem.startswith(case["id"] + "_"), path.name


def test_ids_sind_ueber_beide_ordner_eindeutig():
    """Ein Zielfall wandert spaeter nach cases/; die id darf dort nicht schon stehen."""
    ids = [case["id"] for _, case in _all()]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_ci_suite_haelt_nur_stufe_eins_und_zwei():
    for path, case in _cases(CASES):
        assert _level(case) <= 2, path.name


def test_zielfall_ist_schwer_lieferung_oder_luecke():
    for path, case in _cases(ZIEL):
        assert _level(case) >= 3 or "lieferung" in case["tags"] or "pending" in case, (
            path.name
        )


def test_bekannte_luecke_nennt_ihre_aufgabe():
    for path, case in _all():
        if "pending" in case:
            assert re.search(r"\bT-\d+\.\d+\b", case["pending"]), path.name


def test_offene_luecken_bleiben_eine_minderheit():
    """Die CI-Suite misst vor allem, was heute geht: hoechstens jeder zehnte Fall
    darf eine bekannte Luecke sein, sonst verdeckt sie Rueckschritte."""
    cases = [case for _, case in _cases(CASES)]
    gaps = sum(1 for case in cases if "pending" in case)
    assert gaps * 10 <= len(cases), gaps


def _menu_numbers() -> set[str]:
    with (runner.MENU / "menu_items.csv").open(encoding="utf-8") as f:
        return {row["number"].lower() for row in csv.DictReader(f, delimiter=";")}


def _menu_options() -> dict[str, set[str]]:
    with (runner.MENU / "item_options.csv").open(encoding="utf-8") as f:
        options: dict[str, set[str]] = {}
        for row in csv.DictReader(f, delimiter=";"):
            options.setdefault(row["number"].lower(), set()).add(row["option_name"])
        return options


@pytest.mark.parametrize("path", sorted(ZIEL.glob("*.json")), ids=lambda p: p.stem)
def test_zielfall_ist_gueltig(path):
    """CI spielt ziel/ nicht ab; ein kaputter Zielfall fiele sonst erst mit T-2.4 auf."""
    case = json.loads(path.read_text(encoding="utf-8"))
    validate_case(case, path.name)
    numbers = _menu_numbers()
    wanted = [i["number"] for i in case["expected"].get("items", [])]
    wanted += case.get("sold_out", [])
    assert [n for n in wanted if n.lower() not in numbers] == [], path.name
    # Review PR #162: eine Option, die das Gericht nicht hat, macht das Ziel
    # unerreichbar ("Erdnuss" statt "Erdnusssauce").
    options = _menu_options()
    unknown = [
        (i["number"], o)
        for i in case["expected"].get("items", [])
        for o in i.get("options", [])
        if o not in options.get(i["number"].lower(), set())
    ]
    assert unknown == [], path.name


def test_jeder_fallordner_hat_seine_eigene_baseline():
    """Die Regressionsregel vergleicht mit dem letzten bestandenen Lauf; ein Lauf
    ueber ziel/ darf nie die Baseline der CI-Suite werden (und umgekehrt)."""
    assert runner.default_report_dir(CASES) == runner.REPORTS
    assert runner.default_report_dir(ZIEL) == runner.REPORTS / "ziel"
    relative = Path("evals") / "ziel"
    assert runner.default_report_dir(ZIEL.parent / ".." / relative) == (
        runner.REPORTS / "ziel"
    )
