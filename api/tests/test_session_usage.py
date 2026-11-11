"""Tests for scripts/session_usage.py: usage per model from session transcripts,
recorded per task once, and the estimate for the split question."""

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "session_usage",
    Path(__file__).resolve().parents[2] / "scripts" / "session_usage.py",
)
assert _SPEC is not None and _SPEC.loader is not None
su = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(su)

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"


def reply(mid, model, inp=1, write=0, read=0, out=10, hour=0):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": mid,
                "role": "assistant",
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": write,
                    "cache_read_input_tokens": read,
                    "output_tokens": out,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": hour,
                        "ephemeral_5m_input_tokens": write - hour,
                    },
                },
            },
        }
    )


def session(tmp_path, main_lines, sub_lines=()):
    (tmp_path / "s1.jsonl").write_text("\n".join(main_lines) + "\n")
    if sub_lines:
        sub = tmp_path / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text("\n".join(sub_lines) + "\n")
    return su.transcripts(tmp_path, "s1")


def test_usage_sums_per_model_and_counts_a_split_reply_once(tmp_path):
    paths = session(
        tmp_path,
        [
            reply("m1", OPUS, write=100, read=1000, out=20),
            reply("m1", OPUS, write=100, read=1000, out=20),  # same reply, 2nd block
            '{"type": "user", "message": {"role": "user"}}',
            "not json",
            reply("m2", OPUS, read=2000, out=5),
        ],
        [reply("a1", SONNET, write=50, read=500, out=30, hour=40)],
    )

    totals = su.usage(paths)

    assert totals[OPUS] == {
        "calls": 2,
        "input": 2,
        "cache_write": 100,
        "cache_write_1h": 0,
        "cache_read": 3000,
        "output": 25,
    }
    assert totals[SONNET]["calls"] == 1 and totals[SONNET]["output"] == 30
    # 1 + 1.25 * 10 (five minutes) + 2 * 40 (an hour) + 0.1 * 500 + 5 * 30
    assert su.weighted(totals[SONNET]) == round(1 + 12.5 + 80 + 50 + 150)


def test_a_second_task_of_the_same_session_records_only_its_own_part(tmp_path):
    history = tmp_path / "routing_history.csv"
    first = session(tmp_path, [reply("m1", OPUS, read=1000)])
    part = su.since_last_record(su.usage(first), su.read_history(history), "s1")
    su.record(history, "T-1.1", "no", "s1", part, "2026-11-06")

    later = session(
        tmp_path,
        [reply("m1", OPUS, read=1000), reply("m2", OPUS, read=4000)],
        [reply("a1", SONNET, out=7)],
    )
    part = su.since_last_record(su.usage(later), su.read_history(history), "s1")
    su.record(history, "T-1.2", "yes", "s1", part, "2026-11-06")

    rows = su.read_history(history)
    assert [(r["task"], r["model"], r["cache_read"]) for r in rows] == [
        ("T-1.1", OPUS, "1000"),
        ("T-1.2", OPUS, "4000"),
        ("T-1.2", SONNET, "0"),
    ]
    again = su.since_last_record(su.usage(later), su.read_history(history), "s1")
    assert again == {}
    assert su.record(history, "T-1.3", "no", "s1", again, "2026-11-06") == []
    assert len(su.read_history(history)) == 3


def test_estimate_says_no_data_until_a_split_task_is_recorded(tmp_path):
    history = tmp_path / "routing_history.csv"
    assert su.estimate(su.read_history(history)).startswith("no data yet")

    su.record(history, "T-1.1", "no", "s1", {OPUS: _counts(out=1000)}, "2026-11-06")
    assert su.estimate(su.read_history(history)).startswith("no data yet")

    su.record(
        history,
        "T-1.2",
        "yes",
        "s2",
        {OPUS: _counts(out=600), SONNET: _counts(out=400)},
        "2026-11-06",
    )
    line = su.estimate(su.read_history(history))
    assert line.startswith("1 split task(s): 40% of the weighted volume")
    assert "5,000 split, 5,000 not split (1 task(s))" in line


def test_a_fable_session_counts_as_the_main_model_of_a_split(tmp_path):
    history = tmp_path / "routing_history.csv"
    usage = {"claude-fable-5-2": _counts(out=600), SONNET: _counts(out=400)}
    su.record(history, "T-1.3", "yes", "s3", usage, "2026-11-11")
    line = su.estimate(su.read_history(history))
    assert line.startswith("1 split task(s): 40% of the weighted volume")


def test_a_missing_session_is_a_clear_error(tmp_path, capsys):
    code = su.main(["--session", "nope", "--projects-dir", str(tmp_path), "show"])

    assert code == 2
    assert "no transcript of nope" in capsys.readouterr().err


def test_project_folder_matches_claude_codes_naming():
    assert (
        su.project_folder("/home/u/Maex-Voice-Agent/.claude/worktrees/Block-2")
        == "-home-u-Maex-Voice-Agent--claude-worktrees-Block-2"
    )


def _counts(out=0):
    return dict.fromkeys(su.COUNTS, 0) | {"calls": 1, "output": out}
