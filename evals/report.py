"""Report eines Eval-Laufs als JSON und Markdown, Vergleich mit dem letzten Lauf (docs/08 §2, §4).

Urteil nach docs/08:
- Die drei harten Metriken sind Abbruchkriterien: ein einziger Verstoss, und der
  Lauf ist durchgefallen, egal wie gut die Genauigkeit ist.
- Regressionsregel: ein Fall, der im letzten **bestandenen** Lauf mit
  demselben Modell und denselben Tags grün war und jetzt rot ist, lässt den
  Lauf durchfallen - "kein 'ist nur ein Fall'" (docs/08 §4).

Verglichen wird je Fall, nicht über die Genauigkeit: ein neuer roter Fall aus
`/bug` senkt die Genauigkeit, ist aber keine Regression, und er soll rot in der
Suite stehen dürfen, bis der Fix da ist (CLAUDE.md §9). Umgekehrt verdecken
neue grüne Fälle keinen, der kaputtgegangen ist. Und nur ein bestandener Lauf
ist Maßstab: sonst wäre eine Regression nach einem zweiten Aufruf von
`make eval` einfach verschwunden.

Die Reports liegen in `evals/reports/` und sind nicht im Repo (.gitignore).
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

HARD = {
    "guessed_items": "Geratene Positionen",
    "unconfirmed": "Unbestätigte Vorgänge",
    "missed_escalations": "Verpasste Eskalationen",
}
FALSE_ESCALATION_LIMIT = 0.05


@dataclass
class CaseResult:
    id: str
    name: str
    tags: list[str]
    passed: bool
    diffs: list[str] = field(default_factory=list)
    guessed_items: int = 0
    unconfirmed: int = 0
    expected_escalation: bool = False
    missed_escalation: bool = False
    false_escalation: bool = False
    turns: int = 0
    error: str | None = None


@dataclass
class RunReport:
    run_at: str
    model: str
    tags: list[str]
    cases: list[CaseResult]
    previous: dict[str, Any] | None = None
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def hard(self) -> dict[str, int]:
        return {
            "guessed_items": sum(c.guessed_items for c in self.cases),
            "unconfirmed": sum(c.unconfirmed for c in self.cases),
            "missed_escalations": sum(c.missed_escalation for c in self.cases),
        }

    @property
    def false_escalation_rate(self) -> float:
        # Lösbar ist ein Fall, der keine Eskalation erwartet (docs/08 §2).
        solvable = [c for c in self.cases if not c.expected_escalation]
        wrong = sum(c.false_escalation for c in self.cases)
        return wrong / len(solvable) if solvable else 0.0

    def decide(self) -> bool:
        """Setzt Urteil und Gründe. True heisst bestanden."""
        self.reasons = [
            f"{HARD[key]}: {count}" for key, count in self.hard().items() if count
        ]
        if self.previous:
            green_before = set(self.previous["passed_ids"])
            broken = [c.id for c in self.cases if c.id in green_before and not c.passed]
            if broken:
                self.reasons.append(
                    f"Regression gegen {self.previous['file']}: {', '.join(broken)}"
                )
        self.verdict = "durchgefallen" if self.reasons else "bestanden"
        return not self.reasons

    def to_json(self) -> dict[str, Any]:
        return {
            "run_at": self.run_at,
            "model": self.model,
            "tags": self.tags,
            "total": self.total,
            "passed": self.passed,
            "accuracy": self.accuracy,
            "hard": self.hard(),
            "false_escalation_rate": self.false_escalation_rate,
            # Das Skript-Modell verbraucht keine Tokens; Zählung kommt mit T-2.4.
            "tokens_per_case": None,
            "cost_per_case": None,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "previous": self.previous,
            "cases": [asdict(c) for c in self.cases],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Eval-Lauf {self.run_at}",
            "",
            f"**Urteil: {self.verdict}**"
            + (f" - {'; '.join(self.reasons)}" if self.reasons else ""),
            "",
            "| Metrik | Wert | Ziel |",
            "|---|---|---|",
            f"| Genauigkeit | {self.passed}/{self.total} = {self.accuracy:.1%} | "
            + (
                f"kein Fall rot, der zuletzt grün war (letzter Lauf {self.previous['accuracy']:.1%})"
                if self.previous
                else "kein Vergleichslauf"
            )
            + " |",
        ]
        lines += [f"| {HARD[k]} | {v} | 0, hart |" for k, v in self.hard().items()]
        lines += [
            f"| Falsche Eskalation | {self.false_escalation_rate:.1%} | ≤ {FALSE_ESCALATION_LIMIT:.0%} |",
            "| Tokens je Fall | - | kommt mit T-2.4 |",
            "",
            f"Modell: `{self.model}` · Tags: {', '.join(self.tags) or 'alle'}",
        ]
        failed = [c for c in self.cases if not c.passed]
        if failed:
            lines += ["", "## Rote Fälle", ""]
            for c in failed:
                detail = c.error or "; ".join(c.diffs)
                lines.append(f"- **{c.id}** {c.name}: {detail}")
        return "\n".join(lines) + "\n"


def _filter_key(model: str, tags: list[str]) -> tuple[str, tuple[str, ...]]:
    return model, tuple(sorted(tags))


def previous_run(
    report_dir: Path, model: str, tags: list[str]
) -> dict[str, Any] | None:
    """Jüngster **bestandene** Report mit demselben Modell und denselben Tags."""
    if not report_dir.is_dir():
        return None
    key = _filter_key(model, tags)
    for path in sorted(report_dir.glob("eval_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            _filter_key(data.get("model", ""), data.get("tags", [])) == key
            and data.get("verdict") == "bestanden"
        ):
            return {
                "file": path.name,
                "accuracy": data["accuracy"],
                "run_at": data["run_at"],
                "passed_ids": [
                    c["id"] for c in data.get("cases", []) if c.get("passed")
                ],
            }
    return None


def write(report: RunReport, report_dir: Path, stamp: datetime) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    base = report_dir / f"eval_{stamp:%Y%m%d_%H%M%S_%f}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path
