"""api/telephony/drill.py: the kit of the outage drills (T-1.11), without Docker.

The drills themselves run on the workbench (`python3 -m scripts.voice_drill`).
Here: the judgement of a drill from the lines the containers print, the
rendering of the router's configuration, and the refusals before anything is
touched.
"""

import stat

import pytest

from api.telephony import drill


class Stage(drill.Drill):
    """A drill whose containers are lines a test writes: placing call n adds
    what `script[n]` says the router and the AI telephone print."""

    def __init__(self, tmp_path, script: dict[int, dict[str, list[str]]]) -> None:
        super().__init__(tmp_path, dry_run=False)
        self.script = script
        self.logs: dict[str, list[str]] = {"router": [], "asterisk": [], "voice": []}
        self.commands: list[tuple[str, ...]] = []

    def compose(self, *args: str) -> str:
        self.commands.append(args)
        return ""

    def cli(self, service: str, command: str) -> str:
        if command.startswith("channel originate"):
            n = int(command.split("/")[1].split("@")[0])
            for name, lines in self.script[n].items():
                self.logs[name] += lines
        return "0 active channels"

    def sql(self, statement: str) -> str:
        return "callback"

    def log(self, service: str) -> list[str]:
        return list(self.logs[service])


def router(n: int, *events: str) -> list[str]:
    return [f"NoOp(DRILL {n} {event})" for event in events]


TEAM_RINGS = f"NoOp(DRILL team rings at {drill.TEAM_PHONE} for ai)"


def test_a_silent_caller_who_is_ended_by_the_ai_passes(tmp_path):
    stage = Stage(
        tmp_path,
        {
            101: {
                "router": [
                    *router(101, "dials", "answered"),
                    "NoOp(DRILL 101 caller leg ended dialstatus=ANSWER)",
                ],
                "asterisk": ["call 1.1: voice leg ANSWER ending [hangup]"],
            }
        },
    )
    [(_, ok, detail)] = drill.drill_silent(stage)
    assert ok, detail


def test_a_silent_caller_whose_call_rang_the_team_fails(tmp_path):
    stage = Stage(
        tmp_path,
        {
            101: {
                "router": [
                    *router(101, "dials", "answered"),
                    TEAM_RINGS,
                    "NoOp(DRILL 101 caller leg ended dialstatus=ANSWER)",
                ],
                "asterisk": ["call 1.1: voice leg ANSWER ending []"],
            }
        },
    )
    [(_, ok, _)] = drill.drill_silent(stage)
    assert not ok


def test_paused_passes_only_when_the_ai_never_answered(tmp_path):
    def paused(dialstatus: str) -> Stage:
        return Stage(
            tmp_path,
            {
                103: {
                    "router": [*router(103, "dials"), TEAM_RINGS],
                    "asterisk": [f"call 1.1: voice leg {dialstatus} ending [transfer]"],
                }
            },
        )

    rings, unanswered = drill.drill_paused(paused("NOANSWER"))
    assert rings[1] and unanswered[1]
    _, answered = drill.drill_paused(paused("ANSWER"))
    assert not answered[1]


def test_paused_sets_the_mode_back_when_the_team_never_rings(tmp_path, monkeypatch):
    """Or every later drill would find the AI switched off."""
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)  # gives up at once
    stage = Stage(tmp_path, {103: {"router": router(103, "dials")}})
    modes: list[str] = []
    monkeypatch.setattr(stage, "mode", modes.append)

    with pytest.raises(drill.DrillError):
        drill.drill_paused(stage)
    assert modes == ["paused", "primary"]


def test_voice_is_started_again_when_the_team_never_rings(tmp_path, monkeypatch):
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = Stage(tmp_path, {104: {"router": router(104, "dials")}})

    with pytest.raises(drill.DrillError):
        drill.drill_voice_down(stage)
    assert stage.commands[-1] == ("up", "-d", "--wait", "voice")


def test_a_secret_in_a_containers_output_fails(tmp_path):
    stage = Stage(tmp_path, {})
    assert stage.no_secret_in_the_logs()[1]
    stage.logs["asterisk"].append(
        f"Set(CURLOPT(userpwd)=asterisk:{stage.secrets['VOICE_MEDIA_TOKEN']})"
    )
    _, ok, detail = stage.no_secret_in_the_logs()
    assert not ok and "asterisk" in detail


def test_a_callers_number_in_the_output_of_voice_fails(tmp_path):
    stage = Stage(tmp_path, {})
    # The call to a `voice` that is down is left out: Asterisk's warning
    # repeats the dial string then.
    stage.logs["asterisk"].append("WARNING: failed to voice/v(caller=%2B4972215550104)")
    assert stage.no_caller_in_the_logs()[1]
    stage.logs["voice"].append('"WebSocket /media?call=1.0&caller=%2B4972215550101"')
    _, ok, detail = stage.no_caller_in_the_logs()
    assert not ok and detail == "found in voice"


def test_the_team_number_in_the_output_of_asterisk_fails(tmp_path):
    """It can be somebody's own mobile (Codex on PR #297)."""
    stage = Stage(tmp_path, {})
    stage.logs["asterisk"].append(f"call 1.1: to the team at {drill.TEAM_PHONE}")
    _, ok, detail = stage.no_caller_in_the_logs()
    assert not ok and detail == "found in asterisk"


def test_every_run_makes_new_secrets_and_a_project_per_folder(tmp_path):
    one, two = drill.Drill(tmp_path, dry_run=True), drill.Drill(tmp_path, dry_run=True)
    assert one.secrets != two.secrets
    assert one.project == two.project
    assert drill.Drill(tmp_path / "x", dry_run=True).project != one.project


def test_the_router_gets_the_password_of_the_run_and_nothing_else_is_shared(
    tmp_path,
):
    secret = {"ACCESS_PASSWORD": "pw-of;this-run", "VOICE_MEDIA_TOKEN": "token"}
    drill.render(tmp_path, secret)
    router_dir = tmp_path / "router"
    pjsip = (router_dir / "pjsip.conf").read_text()
    # Escaped: ";" starts a comment in Asterisk's configuration.
    assert r"password=pw-of\;this-run" in pjsip and drill.PASSWORD_MARK not in pjsip
    assert (tmp_path / drill.RUN_MARKER).exists()
    for rendered in router_dir.iterdir():
        assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
        assert "token" not in rendered.read_text()


def test_root_is_refused_before_anything_is_touched(monkeypatch, capsys):
    monkeypatch.setattr(drill.os, "getuid", lambda: 0)
    assert drill.main(["--dry-run"]) == 2
    assert "unprivileged" in capsys.readouterr().err


def test_a_folder_with_somebodys_files_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(drill.os, "getuid", lambda: 1000)
    (tmp_path / "notes.txt").write_text("mine")
    assert drill.main(["--dry-run", "--run-dir", str(tmp_path)]) == 2
    assert (tmp_path / "notes.txt").read_text() == "mine"
