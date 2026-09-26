"""Eval-Suite v1 (T-5.2): Umfang und Pflichtabdeckung aus docs/08 §6, ohne Datenbank.

Ob die Faelle gruen sind, prueft `test_evals_runner.py` mit dem ganzen Lauf. Hier
steht, dass die Suite vollstaendig bleibt: faellt ein Pflichtfall weg, wird ein
Tag umbenannt oder verliert eine bekannte Luecke ihren Grund, ist das rot.
"""

import json
import re
from pathlib import Path

from evals import runner

CASES = Path(runner.__file__).parent / "cases"

DOCS_08 = Path(runner.__file__).parents[1] / "docs" / "08_EVALS.md"
MINDESTENS = 100


def _cases() -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(CASES.glob("*.json"))
    ]


def test_mindestens_hundert_faelle():
    assert len(_cases()) >= MINDESTENS


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
    tags = {tag for _, case in _cases() for tag in case.get("tags", [])}
    missing = [line for line, tag in _pflicht().items() if tag not in tags]
    assert missing == [], f"docs/08 §6 ohne Fall: {missing}"


def test_dateiname_passt_zur_id():
    for path, case in _cases():
        assert path.stem.startswith(case["id"] + "_"), path.name


def test_bekannte_luecke_nennt_ihre_aufgabe():
    for path, case in _cases():
        if "pending" in case:
            assert re.search(r"\bT-\d+\.\d+\b", case["pending"]), path.name


def test_offene_luecken_bleiben_eine_minderheit():
    """Die Suite misst vor allem, was heute geht: hoechstens jeder zehnte Fall
    darf eine bekannte Luecke sein, sonst verdeckt sie Rueckschritte."""
    cases = [case for _, case in _cases()]
    gaps = sum(1 for case in cases if "pending" in case)
    assert gaps * 10 <= len(cases), gaps
