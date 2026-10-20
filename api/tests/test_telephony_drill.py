"""api/telephony/drill.py: the kit of the outage drills (T-1.11), without Docker.

The drills themselves run on the workbench (`python3 -m scripts.voice_drill`).
Here: the judgement of a drill from the lines the containers print, the
rendering of the router's configuration, and the refusals before anything is
touched.
"""

import socket
import stat
import threading

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


def rings_at(number: str) -> str:
    return f"NoOp(DRILL team rings at {number} for ai)"


TEAM_RINGS = rings_at(drill.TEAM_PHONE)


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


OFF_THE_LIST = [
    "call 1.1: voice leg NOANSWER ending [transfer]",
    "call 1.1: transfer to a number not on the list",
]


def test_a_transfer_to_a_number_off_the_list_rings_the_team_phone(tmp_path):
    """#252 finding 3: `voice` names a number that is not on the dialplan's
    list; the dialplan says so, the team rings at TEAM_PHONE and the number is
    never dialled."""

    def transfer(*rings: str, asterisk: list[str] = OFF_THE_LIST) -> Stage:
        script = {"router": [*router(109, "dials"), *rings], "asterisk": asterisk}
        return Stage(tmp_path, {109: script})

    team, refused = drill.drill_unlisted(transfer(TEAM_RINGS))
    assert team[1] and refused[1]
    _, dialled = drill.drill_unlisted(
        transfer(rings_at(drill.UNLISTED_NUMBER), TEAM_RINGS)
    )
    assert not dialled[1]
    # The team rang, but not by the dialplan's refusal: an empty ending, say.
    _, other_way = drill.drill_unlisted(
        transfer(TEAM_RINGS, asterisk=["call 1.1: voice leg NOANSWER ending []"])
    )
    assert not other_way[1]


def busy_transfer(tmp_path, *rings: str) -> Stage:
    return Stage(tmp_path, {110: {"router": [*router(110, "dials"), *rings]}})


def test_a_busy_number_on_the_list_is_followed_by_the_team_phone(tmp_path):
    """#252 finding 1: the leg at another number of the list is not answered;
    the dialplan dials TEAM_PHONE once."""
    busy = rings_at(drill.LISTED_BUSY_NUMBER)

    stage = busy_transfer(tmp_path, busy, TEAM_RINGS)
    assert all(ok for _, ok, _ in drill.drill_listed_busy(stage))
    _, first = drill.drill_listed_busy(busy_transfer(tmp_path, TEAM_RINGS))
    assert not first[1]
    _, after = drill.drill_listed_busy(busy_transfer(tmp_path, TEAM_RINGS, busy))
    assert not after[1]


def test_the_transfer_drills_set_mode_and_team_number_back(tmp_path, monkeypatch):
    """Or every later drill would find the AI paused or the team elsewhere."""
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = Stage(tmp_path, {109: {"router": router(109, "dials")}})
    statements: list[str] = []
    monkeypatch.setattr(stage, "sql", statements.append)

    with pytest.raises(drill.DrillError):
        drill.drill_unlisted(stage)
    assert statements == [
        f"update service_config set team_phone = '{drill.UNLISTED_NUMBER}'",
        "update service_config set call_mode = 'paused'",
        "update service_config set call_mode = 'primary'",
        f"update service_config set team_phone = '{drill.TEAM_PHONE}'",
    ]


@pytest.mark.parametrize("failing", ["call_mode = 'paused'", "call_mode = 'primary'"])
def test_the_team_number_is_set_back_when_a_mode_change_fails(
    tmp_path, monkeypatch, failing
):
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = Stage(tmp_path, {109: {"router": router(109, "dials")}})
    statements: list[str] = []

    def sql(statement: str) -> str:
        statements.append(statement)
        if failing in statement:
            raise drill.DrillError("psql failed")
        return ""

    monkeypatch.setattr(stage, "sql", sql)
    with pytest.raises(drill.DrillError):
        drill.drill_unlisted(stage)
    assert (
        statements[-1] == f"update service_config set team_phone = '{drill.TEAM_PHONE}'"
    )


@pytest.mark.parametrize(
    ("codes", "refused"),
    [
        ([403], True),
        ([100, 403], True),
        # Identification by address alone, with the ACL gone or too wide.
        ([401], False),
        ([100, 401], False),
        ([100], False),  # taken, still working on it
        ([100, 180, 486], False),  # rang somewhere first
        ([100, 200], False),
        ([], False),  # no answer at all is no proof
    ],
)
def test_a_strangers_invite_counts_as_refused_only_by_the_acl(codes, refused):
    assert drill.refused(codes) is refused


def test_the_strangers_invite_comes_as_the_access_and_reads_the_answers():
    """`stranger_invite` runs inside the drill's `voice` container; here
    against a socket that answers as Asterisk does after T-10.13."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as pbx:
        pbx.bind(("127.0.0.1", 0))
        # A regression fails the test instead of hanging it.
        pbx.settimeout(5)
        received: list[bytes] = []

        def answer() -> None:
            invite, sender = pbx.recvfrom(65535)
            received.append(invite)
            for line in (b"SIP/2.0 100 Trying", b"SIP/2.0 403 Forbidden"):
                pbx.sendto(line + b"\r\nContent-Length: 0\r\n\r\n", sender)

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        codes = drill.stranger_invite("127.0.0.1", pbx.getsockname()[1], timeout=5)
        worker.join(timeout=5)

    assert codes == [100, 403]
    assert received[0].startswith(b"INVITE ")
    assert b"\r\nFrom: <sip:access@" in received[0]


def test_a_stranger_answered_by_nobody_has_no_codes():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as quiet:
        quiet.bind(("127.0.0.1", 0))
        assert drill.stranger_invite("127.0.0.1", quiet.getsockname()[1], 0.3) == []


def test_the_stranger_drill_hangs_up_whatever_got_through(tmp_path):
    stage = Stage(tmp_path, {})
    clis: list[tuple[str, str]] = []
    stage.compose = lambda *args: "100 200"  # type: ignore[method-assign]
    stage.cli = lambda service, command: clis.append((service, command)) or ""  # type: ignore[method-assign]

    [(_, ok, detail)] = drill.drill_stranger(stage)
    assert not ok and "100 200" in detail
    assert clis == [("asterisk", "channel request hangup all")]


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
    assert stage.no_caller_in_the_logs()[1]
    stage.logs["voice"].append('"WebSocket /media?call=1.0&caller=%2B4972215550101"')
    _, ok, detail = stage.no_caller_in_the_logs()
    assert not ok and detail == "found in voice"


def test_the_caller_of_the_call_that_found_voice_down_counts_too(tmp_path):
    """Asterisk logs the dial string when `voice` cannot be reached; the
    dial string carries no number since round 2 of Codex on PR #297."""
    stage = Stage(tmp_path, {})
    stage.logs["asterisk"].append("WARNING: failed to voice/v(caller=%2B4972215550104)")
    _, ok, detail = stage.no_caller_in_the_logs()
    assert not ok and detail == "found in asterisk"


@pytest.mark.parametrize(
    "number",
    [drill.TEAM_PHONE, drill.LISTED_BUSY_NUMBER, drill.UNLISTED_NUMBER],
)
def test_a_team_number_in_the_output_of_asterisk_fails(tmp_path, number):
    """It can be somebody's own mobile (Codex on PR #297); so can a number
    `voice` names that is not on the list."""
    stage = Stage(tmp_path, {})
    stage.logs["asterisk"].append(f"call 1.1: to the team at {number}")
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
