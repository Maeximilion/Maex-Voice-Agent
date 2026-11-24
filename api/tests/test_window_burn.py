"""Tests for scripts/window_burn.py: window readings, buckets and the per-model fit."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "window_burn",
    Path(__file__).resolve().parents[2] / "scripts" / "window_burn.py",
)
assert _SPEC is not None and _SPEC.loader is not None
window_burn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(window_burn)

START = datetime(2026, 11, 23, 18, tzinfo=UTC)
END = datetime(2026, 11, 23, 23, tzinfo=UTC)


def _request(msg_id: str, when: str, model: str, output: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": when,
            "message": {
                "id": msg_id,
                "model": model,
                "usage": {"output_tokens": output},
            },
        }
    )


def _reading(when: str, percent: int, label: str = "5-hour limit") -> str:
    text = json.dumps({"plan": {"windows": [{"label": label, "percentUsed": percent}]}})
    return json.dumps(
        {
            "type": "user",
            "timestamp": when,
            "message": {"content": [{"content": [{"type": "text", "text": text}]}]},
        }
    )


def _project(tmp_path: Path, rows: list[str]) -> Path:
    folder = tmp_path / "C--x-Maex-Voice-Agent"
    folder.mkdir()
    (folder / "s1.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return tmp_path


def test_readings_keep_only_the_five_hour_window_inside_the_range(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        [
            _reading("2026-11-23T18:10:00Z", 5),
            _reading("2026-11-23T18:10:30Z", 9, label="Weekly"),
            _reading("2026-11-23T17:00:00Z", 1),
            "not json",
        ],
    )
    points = window_burn.readings(window_burn.transcripts(root), START, END)
    assert [p for _, p in points] == [5]


def test_fit_recovers_the_points_per_million_units_of_each_model(
    tmp_path: Path,
) -> None:
    # Opus 1 point per million units, Sonnet 0.5; output tokens weigh 5 units.
    rows = []
    percent = 0
    for i in range(6):
        t = START + timedelta(minutes=10 * i)
        rows.append(_reading(t.isoformat().replace("+00:00", "Z"), percent))
        opus, sonnet = 4_000_000 * (i + 1), 4_000_000 * (6 - i)
        for n, (model, out) in enumerate(
            (("claude-opus-5-5", opus), ("claude-sonnet-5-5", sonnet))
        ):
            rows.append(
                _request(
                    f"{i}{n}",
                    (t + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    model,
                    out,
                )
            )
        percent += round(opus * 5 / 1e6 * 1.0 + sonnet * 5 / 1e6 * 0.5)
    rows.append(
        _reading(
            (START + timedelta(minutes=60)).isoformat().replace("+00:00", "Z"), percent
        )
    )
    root = _project(tmp_path, rows)
    paths = window_burn.transcripts(root)
    coef, r2, n = window_burn.fit(
        window_burn.readings(paths, START, END), window_burn.load(paths, START, END)
    )
    assert n == 6
    assert abs(coef["opus"] - 1.0) < 0.1 and abs(coef["sonnet"] - 0.5) < 0.1
    assert r2 > 0.95


def test_buckets_count_active_sessions_and_report_runs_end_to_end(
    tmp_path: Path, capsys
) -> None:
    root = _project(
        tmp_path,
        [
            _reading("2026-11-23T18:00:00Z", 0),
            _request("a", "2026-11-23T18:03:00Z", "claude-opus-5-5", 1000),
            _request("b", "2026-11-23T18:04:00Z", "claude-haiku-5-5", 10),
            _reading("2026-11-23T18:30:00Z", 3),
        ],
    )
    paths = window_burn.transcripts(root)
    table = window_burn.buckets(START, END, window_burn.load(paths, START, END))
    assert len(table[START]["sessions"]) == 1 and table[START]["sessions"]["s1"] == 2
    assert (
        window_burn.at(
            [(START, 0), (START + timedelta(minutes=30), 3)],
            START + timedelta(minutes=10),
        )
        == 1.0
    )
    assert window_burn.main(["2026-11-23T18:00", "2026-11-23T23:00", str(root)]) == 0
    assert "fit:" in capsys.readouterr().out


def test_main_without_arguments_prints_usage_and_fails(capsys) -> None:
    assert window_burn.main([]) == 2
    assert "Usage" in capsys.readouterr().out
