"""Tests for scripts/wake_cost.py: requests grouped by the idle gap before them."""

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "wake_cost",
    Path(__file__).resolve().parents[2] / "scripts" / "wake_cost.py",
)
assert _SPEC is not None and _SPEC.loader is not None
wake_cost = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wake_cost)


def _row(msg_id: str, when: str, **usage: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": when,
            "message": {"id": msg_id, "usage": usage},
        }
    )


def _transcript(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_a_wake_after_twenty_five_minutes_lands_in_its_bucket(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        [
            _row("a", "2026-11-23T18:00:00Z", cache_read_input_tokens=1000),
            _row(
                "b",
                "2026-11-23T18:25:00Z",
                cache_read_input_tokens=1000,
                output_tokens=10,
            ),
        ],
    )
    stats = wake_cost.by_gap([path])
    assert len(stats["< 2 min (working)"]) == 1
    assert stats["20-40 min"] == [(150.0, 1000, 0, 0)]


def test_streamed_chunks_of_one_message_count_once_with_their_last_usage(
    tmp_path: Path,
) -> None:
    path = _transcript(
        tmp_path,
        [
            _row("a", "2026-11-23T18:00:00Z", output_tokens=1),
            _row("a", "2026-11-23T18:00:00Z", output_tokens=40),
            "not json",
            json.dumps({"type": "user", "timestamp": "2026-11-23T18:00:01Z"}),
        ],
    )
    stats = wake_cost.by_gap([path])
    assert stats["< 2 min (working)"] == [(200.0, 0, 0, 0)]


def test_a_one_hour_cache_write_is_priced_double(tmp_path: Path) -> None:
    units, write_5m, write_1h = wake_cost.cost(
        {
            "cache_creation": {
                "ephemeral_1h_input_tokens": 100,
                "ephemeral_5m_input_tokens": 0,
            }
        }
    )
    assert (units, write_5m, write_1h) == (200.0, 0, 100)
    # Old transcripts carry only the total; it is taken as a 5-minute write.
    assert wake_cost.cost({"cache_creation_input_tokens": 100}) == (125.0, 100, 0)


def test_main_without_files_prints_usage_and_fails(capsys) -> None:
    assert wake_cost.main([]) == 2
    assert "Usage" in capsys.readouterr().out
