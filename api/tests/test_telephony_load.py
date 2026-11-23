"""The load test kit (#841): stand-in engines and model, and how a run is read
back and judged (`telephony/load.py`). The run itself needs Docker and the test
server; what it decides from its recordings and numbers is tested here."""

import argparse
import json
import os
import shutil
import threading
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from api.agent.llm import ChatCompletionsLLM, parse_turn
from api.telephony import load
from api.telephony.adapters.fake_speech import LoadEar, LoadMouth
from api.telephony.audio import _sounding, tone
from api.telephony.load_model import SAY, answer, completion, handler
from api.telephony.speech import Heard

FRAME = 640  # 20 ms at 16 kHz
LOUD = tone(440, 0.02, 16000)
QUIET = b"\x00" * FRAME


def _feed(ear: LoadEar, frames: list[bytes], start: float = 0.0) -> float:
    at = start
    for frame in frames:
        ear.feed(frame, at)
        at += 0.02
    return at


# -- the stand-in ear and mouth ----------------------------------------------------


def test_load_ear_hears_a_sentence_after_the_silence_that_ends_a_turn() -> None:
    ear = LoadEar()
    heard: list[Heard] = []
    ear.open(heard.append)
    _feed(ear, [LOUD] * 50 + [QUIET] * 40)  # 1 s of speech, 0.8 s of quiet
    finals = [h for h in heard if h.final]
    assert [h.text for h in finals] == [LoadEar.SENTENCES[0]]
    # The end of the speech, not of the report: the last loud frame.
    assert finals[0].ended_at == pytest.approx(0.98)
    # Partials while the caller speaks, at least once a second (speech.Heard).
    partials = [h for h in heard if not h.final]
    assert len(partials) >= 2 and all(h.text == "" for h in partials)


def test_load_ear_waits_for_the_whole_silence_and_takes_the_next_sentence() -> None:
    ear = LoadEar()
    heard: list[Heard] = []
    ear.open(heard.append)
    # 0.5 s of quiet is not yet the end: the final comes after END_SILENCE + DELAY.
    at = _feed(ear, [LOUD] * 10 + [QUIET] * 25)
    assert not [h for h in heard if h.final]
    at = _feed(ear, [QUIET] * 10 + [LOUD] * 10 + [QUIET] * 40, at)
    assert [h.text for h in heard if h.final] == list(LoadEar.SENTENCES[:2])


def test_load_ear_hears_nothing_in_silence() -> None:
    ear = LoadEar()
    heard: list[Heard] = []
    ear.open(heard.append)
    _feed(ear, [QUIET] * 200)
    assert heard == []


def test_load_mouth_answers_after_its_delay_as_long_as_the_sentence() -> None:
    slept: list[float] = []
    mouth = LoadMouth(sleep=slept.append)
    audio = b"".join(mouth.speak("x" * 20))
    assert slept == [LoadMouth.DELAY]
    assert len(audio) == int(20 * LoadMouth.SECONDS_PER_CHAR * 16000) * 2
    assert _sounding(audio, LoadMouth.FREQ_HZ, 16000)


def test_load_mouth_caps_a_long_sentence() -> None:
    audio = b"".join(LoadMouth(sleep=lambda _: None).speak("x" * 500))
    assert len(audio) == int(LoadMouth.MAX_SECONDS * 16000) * 2


# -- the stand-in model ------------------------------------------------------------


def test_model_searches_the_menu_for_what_the_guest_said() -> None:
    turn = answer('Zustand: {"greeted": true}\nEingabe: Die Nummer 23 bitte')
    assert turn["tool"] == "search_menu"
    assert turn["args"] == {"query": "Die Nummer 23 bitte"}
    assert turn["say"] is None


def test_model_answers_a_tool_result_with_its_sentence() -> None:
    result = json.dumps({"tool": "search_menu", "ok": True, "data": {}})
    turn = answer(f"Zustand: {{}}\nEingabe: {result}")
    assert turn["say"] == SAY and turn["tool"] is None


def test_model_answer_holds_the_contract_of_the_core() -> None:
    """Both answers parse as a turn of `agent/llm.py`, and a guest input without
    words still searches something."""
    for said in ("Und zweimal die 12", '{"tool": "search_menu"}', ""):
        body = {
            "messages": [{"role": "user", "content": f"Zustand: {{}}\nEingabe: {said}"}]
        }
        reply = completion(body)
        parse_turn(reply["choices"][0]["message"]["content"])
        assert reply["usage"]["prompt_tokens"] >= 0
    assert answer("Zustand: {}\nEingabe: ")["args"] == {"query": "Speisekarte"}


def test_model_over_http_is_what_the_client_of_the_core_reads() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(0.0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        llm = ChatCompletionsLLM(f"http://127.0.0.1:{port}/v1", "load", timeout=5)
        turn = llm.next_turn("system", {"greeted": True}, "Noch eine große Cola")
        assert turn.tool_call is not None
        assert turn.tool_call.name == "search_menu"
        assert llm.usage.requests == 1 and llm.usage.unmetered == 0
        llm.close()
    finally:
        server.shutdown()


# -- reading a call back -----------------------------------------------------------


RATE = 8000


def _track(parts: list[tuple[int, float]]) -> bytes:
    """Tones and silences (frequency 0) in a row, at the router's 8 kHz."""
    return b"".join(
        tone(freq, seconds, RATE) if freq else b"\x00" * int(seconds * RATE) * 2
        for freq, seconds in parts
    )


def test_turn_times_from_the_two_directions_of_a_call() -> None:
    said = _track([(0, 2.0), (440, 1.5), (0, 5.0), (440, 1.5), (0, 5.0)])
    # The greeting, then answers 1.3 s after the first turn (3.5 s) and 1.1 s
    # after the second (10.0 s).
    heard = _track([(1000, 1.5), (0, 3.3), (1000, 1.8), (0, 4.5), (1000, 1.8)])
    s = load.spans(_sounding(said, 440, RATE), RATE, load.ANSWER_GAP)
    h = load.spans(_sounding(heard, 1000, RATE), RATE, load.ANSWER_GAP)
    times = load.turn_times(s, h, 2)
    assert times[0] == pytest.approx(1.3, abs=0.03)
    assert times[1] == pytest.approx(1.1, abs=0.03)


def test_turn_without_an_answer_before_the_caller_speaks_again() -> None:
    said = [(1.0, 2.5), (7.5, 9.0)]
    heard = [(0.0, 1.0), (10.0, 11.0)]
    assert load.turn_times(said, heard, 2) == [None, 1.0]
    # Fewer stretches than turns: the caller never got to speak.
    assert load.turn_times(said[:1], heard, 2) == [None, None]


def test_lost_audio_inside_an_answer_is_counted() -> None:
    anywhere = [(0.0, float("inf"))]
    whole = _track([(1000, 2.0)])
    broken = _track([(1000, 0.8), (0, 0.1), (1000, 1.1)])
    assert load.lost_seconds(_sounding(whole, 1000, RATE), RATE, anywhere) == 0
    lost = load.lost_seconds(_sounding(broken, 1000, RATE), RATE, anywhere)
    assert lost == pytest.approx(0.1, abs=0.03)


def test_a_long_dropout_inside_an_answer_counts_and_between_answers_not() -> None:
    # The caller speaks 1.0 to 2.5 and 7.5 to 9.0: it listens before 1.0,
    # from 2.5 to 7.5 and after 9.0.
    said = [(1.0, 2.5), (7.5, 9.0)]
    ears = load.listening(said)
    assert ears == [(0.0, 1.0), (2.5, 7.5), (9.0, float("inf"))]
    # One answer from 3.8, a second of it missing; then the next turn's answer.
    heard = _track(
        [(0, 3.8), (1000, 0.5), (0, 1.0), (1000, 0.5), (0, 3.4), (1000, 1.0)]
    )
    lost = load.lost_seconds(_sounding(heard, 1000, RATE), RATE, ears)
    assert lost == pytest.approx(1.0, abs=0.03)


def test_a_call_without_recordings_misses_every_turn(tmp_path: Path) -> None:
    result = load.judge_call("101", tmp_path, 4)
    assert result.turns == [None] * 4


# -- judging a step ----------------------------------------------------------------


def _step(**changes: object) -> load.StepResult:
    values: dict[str, object] = {
        "name": "s1-c3",
        "stacks": 1,
        "calls": 3,
        "turns": [1.30, 1.35, 1.40],
        "missing_turns": 0,
        "lost_audio": 0.0,
        "tool_ms": [12.0, 20.0, 35.0],
        "team_rings": 0,
        "cpu_percent_mean": 50.0,
        "cpu_percent_peak": 90.0,
        "memory_peak_mb": 2000.0,
        "memory_limit_mb": 8192.0,
        "services": {},
        "restarts": [],
    }
    values.update(changes)
    result = load.StepResult(**values)  # type: ignore[arg-type]
    result.checks = load.judge_step(result, 1.5)
    return result


def test_a_step_inside_the_budget() -> None:
    assert _step().inside
    assert "Largest number of stacks with all calls inside the budget: 1" in (
        load.limit_line([_step()], None)
    )


@pytest.mark.parametrize(
    ("changes", "failed"),
    [
        ({"turns": [1.6, 1.7, 1.8]}, "turn time"),
        ({"missing_turns": 1}, "every turn answered"),
        ({"tool_ms": [10.0] * 18 + [400.0, 500.0]}, "tool calls"),
        ({"lost_audio": 0.2}, "audio steady"),
        ({"team_rings": 1}, "no team ring"),
        ({"restarts": ["maex-load1-voice-1 restarts=1"]}, "nothing restarted"),
        ({"memory_peak_mb": 8000.0}, "memory"),
        ({"turns": []}, "turn time"),
    ],
)
def test_each_limit_fails_the_step(changes: dict[str, object], failed: str) -> None:
    result = _step(**changes)
    assert not result.inside
    assert [name for name, ok, _ in result.checks if not ok] == [failed]


def test_the_warm_up_alone_is_no_capacity() -> None:
    steps = [
        _step(name="s1-c1", calls=1),
        _step(name="s1-c3", turns=[2.0]),
    ]
    assert "inside the budget: 0" in load.limit_line(steps, None)


def test_server_memory_in_the_test_server_and_on_a_vps(tmp_path: Path) -> None:
    cgroup = tmp_path / "cg"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text(str(3 * 2**30))
    (cgroup / "memory.max").write_text(str(8 * 2**30))
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal: 8000000 kB\nMemFree: 1 kB\nMemAvailable: 6000000 kB\n"
    )
    assert load.server_memory(cgroup, meminfo) == (3 * 2**30, 8 * 2**30)
    # A VPS's root cgroup has neither file: the kernel's count, without cache.
    bare = tmp_path / "root"
    bare.mkdir()
    assert load.server_memory(bare, meminfo) == (2000000 * 1024, 8000000 * 1024)


def test_a_dry_run_on_the_server_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(load, "docker_problem", lambda: None)
    monkeypatch.setattr(os, "getuid", lambda: 1000)

    def refused(*_: object, **__: object) -> None:
        raise AssertionError("a dry run must not start anything")

    monkeypatch.setattr(load, "Inside", refused)
    assert load.main(["--inside", "--dry-run", "--max-stacks", "2"]) == 0
    assert "step s2-c3" in capsys.readouterr().out


def test_the_limit_names_the_first_failed_step() -> None:
    steps = [
        _step(name="s1-c3"),
        _step(name="s2-c3", stacks=2, calls=6),
        _step(name="s3-c3", stacks=3, calls=9, turns=[2.0]),
    ]
    line = load.limit_line(steps, None)
    assert "inside the budget: 2" in line and "s3-c3: turn time" in line
    assert "| s3-c3 | 3 x 3 |" in load.table(steps)


# -- the run's pieces --------------------------------------------------------------


def test_plan_adds_one_stack_per_step() -> None:
    assert load.plan(3, 3) == [
        ("s1-c1", 1, 1),
        ("s1-c3", 1, 3),
        ("s2-c3", 2, 3),
        ("s3-c3", 3, 3),
    ]
    assert load.plan(1, 1) == [("s1-c1", 1, 1)]


def test_router_configuration_per_stack() -> None:
    stacks = [
        load.Stack(1, Path("/srv/maex/load1"), "aa;bb"),
        load.Stack(2, Path("/srv/maex/load2"), "cc"),
    ]
    header = (load.KIT_DIR / "router" / "pjsip.conf").read_text()
    text = load.router_pjsip(header, stacks)
    assert text.count("type=endpoint") == 2
    # ";" starts a comment in Asterisk's files (render.sh escapes it too).
    assert r"password=aa\;bb" in text and "username=ai2" in text
    plan = (load.KIT_DIR / "router" / "extensions.conf").read_text()
    dialplan = load.router_dialplan(plan, turns=4, speak=1.5, listen=5.0, greeting=10.0)
    assert "TURNS=4" in dialplan and "GREETING=10.0" in dialplan
    assert "@" not in dialplan.split("[dialout]")[0].split("[globals]")[1]


def test_customer_file_is_one_new_stack_accepts() -> None:
    spec = load.customer(3)
    assert spec["key"] == "load3"
    assert spec["team_phone"] == "+4972215583" + "00"
    assert load.VOICE_NUMBER.format(n=3) != spec["team_phone"]


def test_services_are_named_across_stacks() -> None:
    assert load.service_of("maex-load3-voice-1") == "voice"
    assert load.service_of("maex-ops-dashboard-1") == "ops dashboard"
    assert load.service_of("load-line-router-1") == "line router"
    assert load.service_of("maex-caddy-caddy-1") == "caddy"
    assert load.service_of("something") == "something"


def test_env_is_set_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('# comment\nCOMPOSE_FILE="a:b"\nACCESS_PASSWORD="x$$y"\n')
    load.set_env(path, {"COMPOSE_FILE": "a:b:c", "VOICE_EAR": "load"})
    env = load.read_env(path.read_text())
    assert env["COMPOSE_FILE"] == "a:b:c" and env["VOICE_EAR"] == "load"
    assert env["ACCESS_PASSWORD"] == "x$y"
    assert path.read_text().startswith("# comment\n")


def test_cgroup_numbers(tmp_path: Path) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec 1500000\nuser_usec 1000000\n")
    (tmp_path / "memory.current").write_text("1048576\n")
    (tmp_path / "memory.max").write_text("max\n")
    assert load.usec(tmp_path) == 1_500_000
    assert load.memory(tmp_path) == 2**20
    # "max" is no limit, and a container gone is nothing.
    assert load.read_int(tmp_path / "memory.max") == 0
    assert load.usec(tmp_path / "gone") == 0


def test_percentile_and_stand_ins() -> None:
    assert load.percentile([], 0.95) is None
    assert load.percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert load.stand_in_seconds(0.25) == 1.25


def test_memory_kills_of_the_server(tmp_path: Path) -> None:
    assert load.oom_kills(tmp_path) == 0
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 1\n")
    assert load.oom_kills(tmp_path) == 1


def test_lost_audio_may_be_allowed() -> None:
    result = _step(lost_audio=0.02)
    assert not result.inside
    result.checks = load.judge_step(result, 1.5, lost_audio=0.1)
    assert result.inside


def test_an_answer_cut_short_counts_as_lost() -> None:
    turn = [(2.5, 7.5)]
    whole = _track([(0, 3.8), (1000, load.ANSWER_SECONDS)])
    cut = _track([(0, 3.8), (1000, 0.8)])
    assert (
        load.cut_seconds(_sounding(whole, 1000, RATE), RATE, turn, load.ANSWER_SECONDS)
        == 0
    )
    short = load.cut_seconds(
        _sounding(cut, 1000, RATE), RATE, turn, load.ANSWER_SECONDS
    )
    assert short == pytest.approx(load.ANSWER_SECONDS - 0.8, abs=0.03)
    # A gap inside a whole answer is lost audio, counted there and not here.
    gap = _track([(0, 3.8), (1000, 0.8), (0, 0.4), (1000, load.ANSWER_SECONDS - 1.2)])
    assert (
        load.cut_seconds(_sounding(gap, 1000, RATE), RATE, turn, load.ANSWER_SECONDS)
        == 0
    )
    assert load.lost_seconds(_sounding(gap, 1000, RATE), RATE, turn) == pytest.approx(
        0.4, abs=0.03
    )
    # A turn without an answer is a missing turn, not a cut one.
    assert load.cut_seconds([], RATE, turn, load.ANSWER_SECONDS) == 0


def test_the_server_side_refuses_a_folder_it_did_not_make(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("mine")
    args = argparse.Namespace(run_dir=tmp_path)
    with pytest.raises(load.LoadError, match="not a run directory"):
        load.Inside(args).run()
    assert (tmp_path / "notes.txt").exists()


def test_a_call_whose_caller_never_spoke(tmp_path: Path) -> None:
    """Recordings without the caller's tone: every turn missing, no crash."""
    assert load.listening([]) == [(0.0, float("inf"))]
    for name, freq in (("101-said", 0), ("101-heard", 1000)):
        with wave.open(str(tmp_path / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(_track([(freq, 2.0)]))
    result = load.judge_call("101", tmp_path, 4)
    assert result.turns == [None] * 4 and result.lost_audio == 0


# -- what a run removes (#888 S6) -------------------------------------------------


class _Docker:
    """`load.sh` without Docker: the Compose projects it lists are `projects`
    (container `c<i>` is project i), `rm -rf` removes in the fake root only,
    every command is recorded."""

    def __init__(self, root: Path, projects: list[str], fail: str = "") -> None:
        self.root, self.projects, self.fail = root, projects, fail
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> str:
        self.calls.append(command)
        if self.fail and self.fail in " ".join(command):
            raise load.LoadError(f"{self.fail} failed")
        if command[0] == "rm":
            target = Path(command[-1])
            assert target.parent == self.root, target
            shutil.rmtree(target)
            return ""
        if command[:3] == ["docker", "rm", "-f"]:
            gone = {int(cid[1:]) for cid in command[3:]}
            self.projects = [p for i, p in enumerate(self.projects) if i not in gone]
            return ""
        if command[-1].startswith("{{.ID}}"):
            return "".join(f"c{i} {p}\n" for i, p in enumerate(self.projects))
        if "--format" in command:
            return "".join(f"{p}\n" for p in self.projects)
        return ""

    def removals(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[0] == "rm" or c[:3] == ["docker", "rm", "-f"] or "prune" in c
        ]


def _load_stack(root: Path, n: int, domain: str | None = None) -> Path:
    folder = root / f"load{n}"
    folder.mkdir(parents=True)
    domain = domain or load.DOMAIN.format(n=n)
    (folder / ".env").write_text(
        f'COMPOSE_PROJECT_NAME="maex-load{n}"\nAGENT_DOMAIN="agent.{domain}"\n'
    )
    return folder


def _kit_root(root: Path) -> None:
    """What a run of the kit leaves: two stacks (one stopped before its .env),
    the operator stack, the shared Caddy, the marker and new_stack's lock."""
    _load_stack(root, 1)
    (root / "load2").mkdir()
    (root / "ops" / "backups").mkdir(parents=True)
    (root / "ops" / ".env").write_text(f"OPERATOR_ORIGIN={load.LOAD_ORIGIN}\n")
    (root / "caddy" / "sites").mkdir(parents=True)
    (root / "caddy" / "sites" / "load1.caddy").write_text("agent.load1.test {}\n")
    (root / load.RUN_MARKER).touch()
    (root / load.ROOT_LOCK).touch()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "srv"
    root.mkdir()
    monkeypatch.setattr(load, "ROOT", root)
    return root


def test_the_marker_is_the_one_new_stack_refuses_customers_by() -> None:
    from scripts import new_stack

    assert load.RUN_MARKER == new_stack.LOAD_RUN


def test_a_server_with_customers_is_refused_though_a_marker_is_left(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding: a run left its marker, then a customer and the real
    operator stack were set up. Nothing is removed, nothing is stopped."""
    _kit_root(root)
    (root / "ops" / ".env").write_text("OPERATOR_ORIGIN=https://ops.example.org\n")
    (root / "pizzeria").mkdir()
    (root / "pizzeria" / ".env").write_text('AGENT_DOMAIN="agent.example.org"\n')
    docker = _Docker(root, ["maex-ops", "maex-caddy", "maex-pizzeria", "maex-load1"])
    monkeypatch.setattr(load, "sh", docker)
    with pytest.raises(load.LoadError) as refused:
        load.clean_slate(tmp_path / "run")
    assert str(root / "ops") in str(refused.value)
    assert str(root / "pizzeria") in str(refused.value)
    assert "Compose project maex-pizzeria" in str(refused.value)
    assert docker.removals() == []
    assert (root / "ops" / "backups").is_dir()
    assert (root / "pizzeria" / ".env").exists()


@pytest.mark.parametrize(
    "stranger", ["project", "load-key", "operator-site", "offboarding", "symlink"]
)
def test_anything_the_kit_cannot_tell_is_its_own_refuses_the_run(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stranger: str
) -> None:
    _kit_root(root)
    projects = ["maex-load1", "maex-ops", "maex-caddy", load.LINE_PROJECT]
    if stranger == "project":  # a customer's volume whose folder is gone
        projects.append("maex-pizzeria")
    elif stranger == "load-key":  # a customer whose key happens to be load3
        _load_stack(root, 3, domain="example.org")
    elif stranger == "operator-site":  # the operator's own site in the Caddy
        (root / "caddy" / "operator").mkdir()
        (root / "caddy" / "operator" / "ops.caddy").write_text("ops.example.org {}\n")
    elif stranger == "offboarding":  # remove_stack.py's exports and ledger
        (root / "offboarding").mkdir()
    else:
        (root / "load4").symlink_to(tmp_path)
    docker = _Docker(root, projects)
    monkeypatch.setattr(load, "sh", docker)
    with pytest.raises(load.LoadError, match="did not make"):
        load.clean_slate(tmp_path / "run")
    assert docker.removals() == []
    assert (root / "load1" / ".env").exists()
    assert (root / "ops" / "backups").is_dir()


def test_a_clean_slate_removes_only_the_kits_own(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kit_root(root)
    projects = ["maex-load1", "maex-ops", "maex-caddy", load.LINE_PROJECT, "other"]
    docker = _Docker(root, projects)
    monkeypatch.setattr(load, "sh", docker)
    run = tmp_path / "run"
    (run / "recordings").mkdir(parents=True)
    (run / "report.json").write_text("{}")

    load.clean_slate(run)

    assert sorted(p.name for p in root.iterdir()) == [load.ROOT_LOCK, load.RUN_MARKER]
    assert docker.projects == ["other"]  # not one of the kit's names: untouched
    pruned = {c[-1] for c in docker.calls if "prune" in c}
    assert pruned == {
        f"label=com.docker.compose.project={p}"
        for p in ("maex-load1", "maex-load2", "maex-ops", "maex-caddy", "load-line")
    }
    assert not (run / "recordings").exists()
    assert not (run / "report.json").exists()


def test_an_empty_server_is_used_and_marked(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(root, ["something-else"])
    monkeypatch.setattr(load, "sh", docker)
    load.clean_slate(tmp_path / "run")
    assert (root / load.RUN_MARKER).exists()
    assert [c for c in docker.calls if c[0] == "rm"] == []
    assert docker.projects == ["something-else"]


@pytest.mark.parametrize("keep", [False, True])
def test_a_run_removes_what_it_made_also_when_a_step_broke(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep: bool
) -> None:
    docker = _Docker(root, [])
    monkeypatch.setattr(load, "sh", docker)

    def broke(self: load.Inside) -> int:
        _kit_root(root)  # what the run set up before the step broke
        docker.projects += ["maex-load1", "maex-ops", "maex-caddy", load.LINE_PROJECT]
        raise load.LoadError("stack load2 did not come up")

    monkeypatch.setattr(load.Inside, "measure", broke)
    args = argparse.Namespace(run_dir=tmp_path / "run", keep=keep)
    with pytest.raises(load.LoadError, match="load2"):
        load.Inside(args).run()
    if keep:
        assert (root / load.RUN_MARKER).exists()
        assert (root / "load1").is_dir()
    else:
        assert sorted(p.name for p in root.iterdir()) == [load.ROOT_LOCK]
        assert docker.projects == []


def test_a_finished_run_removes_what_it_made(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _Docker(root, [])
    monkeypatch.setattr(load, "sh", docker)

    def measured(self: load.Inside) -> int:
        _kit_root(root)
        docker.projects += ["maex-load1", load.LINE_PROJECT]
        return 0

    monkeypatch.setattr(load.Inside, "measure", measured)
    args = argparse.Namespace(run_dir=tmp_path / "run", keep=False)
    assert load.Inside(args).run() == 0
    assert sorted(p.name for p in root.iterdir()) == [load.ROOT_LOCK]
    assert docker.projects == []


def test_a_failed_removal_keeps_the_marker(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _kit_root(root)
    (root / "pizzeria").mkdir()
    monkeypatch.setattr(load, "sh", _Docker(root, ["maex-load1"], fail="docker rm"))
    assert load.tear_down() is False
    assert (root / load.RUN_MARKER).exists()
    assert (root / "load1").is_dir()
    assert "not all removed" in capsys.readouterr().err
    # Removed once Docker works; a customer's folder stays where it is.
    docker = _Docker(root, ["maex-load1", "maex-pizzeria"])
    monkeypatch.setattr(load, "sh", docker)
    assert load.tear_down() is True
    assert sorted(p.name for p in root.iterdir()) == [load.ROOT_LOCK, "pizzeria"]
    assert docker.projects == ["maex-pizzeria"]
    out = capsys.readouterr().out
    assert "left in place" in out and "pizzeria" in out


def test_down_on_the_server_removes_a_runs_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(load, "docker_problem", lambda: None)
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    calls: list[str] = []

    def tear_down() -> bool:
        calls.append("down")
        return True

    monkeypatch.setattr(load, "tear_down", tear_down)
    assert load.main(["--inside", "--down"]) == 0
    assert calls == ["down"]
