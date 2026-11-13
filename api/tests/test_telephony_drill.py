"""api/telephony/drill.py: the kit of the outage drills (T-1.11), without Docker.

The drills themselves run on the workbench (`python3 -m scripts.voice_drill`).
Here: the judgement of a drill from the lines the containers print, the
rendering of the router's configuration, and the refusals before anything is
touched.
"""

import socket
import stat
import threading
from datetime import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from api.telephony import drill
from api.telephony.adapters.fake_speech import DrillEar
from api.telephony.audio import tone
from api.telephony.speech import Heard
from api.tests.conftest import alembic_config


class Stage(drill.Drill):
    """A drill whose containers are lines a test writes: placing call n adds
    what `script[n]` says the router and the AI telephone print."""

    def __init__(self, tmp_path, script: dict[int, dict[str, list[str]]]) -> None:
        super().__init__(tmp_path, dry_run=False)
        self.script = script
        self.logs: dict[str, list[str]] = {"router": [], "asterisk": [], "voice": []}
        self.commands: list[tuple[str, ...]] = []
        # What a command prints, by a piece of it; "" for every other. An
        # exception is raised, as a command that fails does.
        self.prints: dict[str, str | Exception] = {}

    def compose(self, *args: str) -> str:
        return self.docker(*args)

    def docker(self, *args: str) -> str:
        self.commands.append(args)
        joined = " ".join(args)
        out = next((out for key, out in self.prints.items() if key in joined), "")
        if isinstance(out, Exception):
            raise out
        return out

    def cli(self, service: str, command: str) -> str:
        self.commands.append(("cli", service, command))
        if command.startswith("channel originate"):
            n = int(command.split("/")[1].split("@")[0])
            for name, lines in self.script[n].items():
                self.logs[name] += lines
        return "0 active channels"

    def sql(self, statement: str) -> str:
        self.commands.append(("sql", statement))
        return next(
            (
                out
                for key, out in self.prints.items()
                if key in statement and isinstance(out, str)
            ),
            "callback",
        )

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
    [(_, ok, detail), _] = drill.drill_silent(stage)
    assert ok, detail


def _silent_stage(tmp_path, note: str, monkeypatch) -> Stage:
    monkeypatch.setattr(drill, "NOTE_WAIT_SECONDS", 0)
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
    stage.prints["line_note"] = note
    return stage


def test_a_silent_caller_leaves_the_note_that_nothing_of_it_arrived(
    tmp_path, monkeypatch
):
    # Also what answers #651's open question: that Asterisk sends no audio,
    # or only zeros, on a caller leg that sends none.
    [_, (name, ok, detail)] = drill.drill_silent(
        _silent_stage(tmp_path, "no_caller_audio", monkeypatch)
    )
    assert ok, detail
    assert "no_caller_audio" in name


def test_a_silent_caller_whose_call_carries_no_note_fails(tmp_path, monkeypatch):
    # Asterisk sent audible frames, or the session did not mark the call.
    [_, (_, ok, detail)] = drill.drill_silent(
        _silent_stage(tmp_path, "none", monkeypatch)
    )
    assert not ok
    assert "none" in detail


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
    [(_, ok, _), _] = drill.drill_silent(stage)
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


# -- the recording (T-11.3) -------------------------------------------------------

ANSWERED = "call 1.1: voice leg ANSWER ending [transfer] recorded [{}]"


def speaking(n: int, recorded: str, *more: str) -> dict[int, dict[str, list[str]]]:
    """Call n: the AI answers, the team rings, the leg ends."""
    return {
        n: {
            "router": [
                *router(n, "dials", "answered"),
                TEAM_RINGS,
                f"NoOp(DRILL {n} caller leg ended dialstatus=ANSWER)",
            ],
            "asterisk": [ANSWERED.format(recorded), *more],
        }
    }


def test_recording_off_passes_only_without_a_word_and_a_file(tmp_path):
    silent = {
        121: {
            "router": [*router(121, "dials"), TEAM_RINGS],
            "asterisk": [
                "call 1.1: voice leg NOANSWER ending [transfer] recorded [no]"
            ],
        }
    }
    assert all(ok for _, ok, _ in drill.drill_recording_off(Stage(tmp_path, silent)))

    stage = Stage(tmp_path, silent)
    stage.prints["find"] = "1.1.wav 44\n"
    assert not drill.drill_recording_off(stage)[1][1]

    asked = Stage(tmp_path, speaking(121, "no"))
    assert not drill.drill_recording_off(asked)[1][1]


def test_a_recording_on_a_silent_call_fails_the_drill(tmp_path):
    def silence(recorded: str, consent: str) -> Stage:
        stage = Stage(tmp_path, speaking(122, recorded))
        stage.prints["string_agg"] = consent
        stage.prints["recordings r"] = ""
        return stage

    [(_, ok, detail)] = drill.recording_silence(silence("no", "unclear"))
    assert ok, detail
    assert not drill.recording_silence(silence("yes", "unclear"))[0][1]
    assert not drill.recording_silence(silence("no", "yes"))[0][1]


def test_a_hangup_on_the_question_passes_only_with_a_card_and_no_ring(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(drill, "CARD_SECONDS", 0.0)

    def hangup(card: str, *more: str) -> Stage:
        stage = Stage(tmp_path, {127: {"router": [*router(127, "answered"), *more]}})
        stage.prints["callbacks b"] = card
        return stage

    stage = hangup("callback|1")
    [(_, ok, detail)] = drill.recording_hangup(stage)
    assert ok, detail
    assert ("sql", "update service_config set call_mode = 'primary'") in stage.commands
    assert not drill.recording_hangup(hangup(""))[0][1]
    assert not drill.recording_hangup(hangup("abandoned|0"))[0][1]
    assert not drill.recording_hangup(hangup("callback|1", TEAM_RINGS))[0][1]
    dialplan = (drill.CONFIG_DIR / "drill" / "extensions.conf").read_text()
    assert '"127"' in next(line for line in dialplan.splitlines() if "?leaves)" in line)


def test_a_yes_passes_only_with_audio_on_the_file(tmp_path):
    def yes(size: int) -> Stage:
        stage = Stage(tmp_path, speaking(123, "yes"))
        stage.prints["string_agg"] = "yes"
        stage.prints["recordings r"] = "running"
        stage.prints["find"] = f"1.1.wav {size}\n"
        return stage

    [(_, ok, detail)] = drill.recording_yes(yes(8044))
    assert ok, detail
    assert not drill.recording_yes(yes(44))[0][1]


def test_the_stop_key_passes_only_with_the_file_gone_and_the_log_written(tmp_path):
    told = "call 1.1: recording stopped by the team, voice [stopped]"

    def stopped(files: str, consent: str) -> Stage:
        stage = Stage(tmp_path, speaking(124, "yes", told))
        stage.prints["string_agg"] = consent
        stage.prints["recordings r"] = "withdrawn"
        stage.prints["find"] = files
        return stage

    [(_, ok, detail)] = drill.recording_stop_key(stopped("", "yes,no"))
    assert ok, detail
    assert not drill.recording_stop_key(stopped("1.1.wav 4000\n", "yes,no"))[0][1]
    assert not drill.recording_stop_key(stopped("", "yes"))[0][1]


def test_the_team_key_sets_the_router_back(tmp_path):
    stage = Stage(tmp_path, speaking(124, "yes"))
    drill.recording_stop_key(stage)
    assert ("cli", "router", "dialplan set global TEAM_AWAY answer") in stage.commands
    assert ("sql", "update service_config set call_mode = 'primary'") in stage.commands


def test_an_ai_call_passes_only_when_the_file_stops_at_the_handover(tmp_path):
    def ai_call(recorded: str, wav: str) -> Stage:
        stage = Stage(tmp_path, speaking(125, recorded))
        stage.prints["string_agg"] = "yes"
        stage.prints["recordings r"] = "running"
        stage.prints["find"] = "1.1.wav 96044\n"
        stage.prints["wave.open"] = wav
        return stage

    [(_, ok, detail)] = drill.recording_ai_call(ai_call("no", "2 16000 9000"))
    assert ok, detail
    assert not drill.recording_ai_call(ai_call("yes", "2 16000 9000"))[0][1]
    assert not drill.recording_ai_call(ai_call("no", "2 16000 0"))[0][1]


def test_voice_dying_on_the_question_is_started_again(tmp_path):
    stage = Stage(
        tmp_path,
        {
            126: {
                "router": [*router(126, "dials", "answered"), TEAM_RINGS],
                "asterisk": ["call 1.1: voice leg ANSWER ending [] recorded [no]"],
            }
        },
    )
    rings, off = drill.recording_crash(stage)
    assert rings[1] and off[1]
    assert ("up", "-d", "--wait", "voice") in stage.commands


def test_the_recording_is_switched_off_again_whatever_fails(tmp_path, monkeypatch):
    stage = Stage(tmp_path, {})

    def broken(_):
        raise drill.DrillError("a call of the last drill does not end")

    monkeypatch.setattr(drill, "recording_silence", broken)
    with pytest.raises(drill.DrillError):
        drill.drill_recording(stage)
    assert stage._env["DRILL_RECORDING"] == "false"


def test_the_drill_ear_says_yes_to_a_tone_and_nothing_to_silence():
    ear = DrillEar()
    heard: list[Heard] = []
    ear.open(heard.append)
    silence, loud = bytes(320), tone(440, 0.02, 16000)

    ear.feed(silence, 1.0)
    assert heard == []

    ear.feed(loud, 2.0)
    ear.feed(loud, 2.5)
    ear.feed(loud, 3.0)
    assert [(h.text, h.ended_at) for h in heard] == [("Ja", 2.0), ("Ja", 3.0)]


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
        codes = drill.stranger_invite(
            "127.0.0.1", pbx.getsockname()[1], timeout=5, user="+4972215550114"
        )
        worker.join(timeout=5)

    assert codes == [100, 403]
    assert received[0].startswith(b"INVITE ")
    assert b"\r\nFrom: <sip:+4972215550114@" in received[0]


def test_a_stranger_answered_by_nobody_has_no_codes():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as quiet:
        quiet.bind(("127.0.0.1", 0))
        assert drill.stranger_invite("127.0.0.1", quiet.getsockname()[1], 0.3) == []


def test_the_stranger_drill_hangs_up_whatever_got_through(tmp_path):
    stage = Stage(tmp_path, {})
    clis: list[tuple[str, str]] = []
    stage.compose = lambda *args: "100 200"  # type: ignore[method-assign]

    def cli(service: str, command: str) -> str:
        clis.append((service, command))
        return ""

    stage.cli = cli  # type: ignore[method-assign]

    [(_, ok, detail), (_, number_ok, _)] = drill.drill_stranger(stage)
    assert not ok and not number_ok and "100 200" in detail
    assert clis == [("asterisk", "channel request hangup all")] * 2


def test_the_stranger_also_calls_with_a_number_the_logs_must_not_show(tmp_path):
    """#252 finding 12: a refused INVITE must not leave the caller's number in
    Asterisk's output; the second INVITE carries one, and the log check of
    every run looks for it."""
    stage = Stage(tmp_path, {})
    sent: list[str] = []

    def compose(*args: str) -> str:
        sent.append(args[-1])
        return "100 403"

    stage.compose = compose  # type: ignore[method-assign]
    stage.cli = lambda service, command: ""  # type: ignore[method-assign]

    checks = drill.drill_stranger(stage)

    assert [ok for _, ok, _ in checks] == [True, True]
    assert "user='access'" in sent[0]
    assert f"user={drill.STRANGER_NUMBER!r}" in sent[1]
    stage.logs["asterisk"].append(
        f"Request 'INVITE' from '<sip:{drill.STRANGER_NUMBER}@x>' failed"
    )
    _, ok, detail = stage.no_caller_in_the_logs()
    assert not ok and detail == "found in asterisk"


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


def networks_stage(tmp_path, **prints: str) -> Stage:
    """A stack whose walls hold, unless a test says otherwise. A key names a
    piece of the command, the first that matches answers."""
    stage = Stage(tmp_path, {})
    stage.prints = {
        "hostname -i": "172.30.0.2\n",
        "ip route": "172.30.0.0/16 dev eth0 scope link\n",
        "voice 8001": "reached\n",
        **{key.replace("_", " "): value for key, value in prints.items()},
        "5432": "no\n",
    }
    return stage


def verdicts(stage: Stage) -> list[bool]:
    return [ok for _, ok, _ in drill.drill_networks(stage)]


def test_walls_that_hold_pass(tmp_path):
    stage = networks_stage(tmp_path)
    assert verdicts(stage) == [True, True, True]
    tried = {" ".join(c) for c in stage.commands if "sh" in c}
    assert any("nc -z -w 2 db 5432" in c for c in tried)
    assert any("nc -z -w 2 172.30.0.2 5432" in c for c in tried)


def test_an_asterisk_that_lost_voice_fails(tmp_path):
    assert verdicts(networks_stage(tmp_path, voice_8001="no\n"))[0] is False


@pytest.mark.parametrize("host", ["db_5432", "172.30.0.2_5432"])
def test_a_database_asterisk_can_open_fails_by_name_and_by_address(tmp_path, host):
    stage = networks_stage(tmp_path, **{host: "reached\n"})
    assert verdicts(stage)[1] is False


def test_a_database_with_a_way_out_fails(tmp_path):
    routes = "default via 172.30.0.1 dev eth0\n172.30.0.0/16 dev eth0\n"
    assert verdicts(networks_stage(tmp_path, ip_route=routes))[2] is False


def test_every_address_of_the_database_is_tried(tmp_path):
    """Two addresses on one `nc` line would be a bad call, which reads as "no"."""
    stage = networks_stage(tmp_path, **{"hostname -i": "172.30.0.2 fd00::2\n"})
    assert verdicts(stage) == [True, True, True]
    stage = networks_stage(
        tmp_path, **{"hostname -i": "172.30.0.2 fd00::2\n", "fd00::2 5432": "reached\n"}
    )
    assert verdicts(stage)[1] is False


@pytest.mark.parametrize("empty", ["hostname -i", "ip route"])
def test_a_check_that_saw_nothing_is_no_pass(tmp_path, empty):
    """An empty address tries nothing, and an empty list has no default route."""
    stage = networks_stage(tmp_path)
    stage.prints[empty] = ""
    assert False in verdicts(stage)


def network_commands(stage: Stage) -> list[tuple[str, ...]]:
    return [c for c in stage.commands if c[0] == "network"]


def test_voice_is_cut_before_the_call_and_put_back_under_its_name(tmp_path):
    stage = Stage(tmp_path, {115: {"router": [*router(115, "dials"), TEAM_RINGS]}})
    stage.prints["ps -q voice"] = "abc123\n"

    [(_, ok, detail)] = drill.drill_cut_from_asterisk(stage)

    assert ok, detail
    network = f"{stage.project}_telephony"
    assert network_commands(stage) == [
        ("network", "disconnect", network, "abc123"),
        ("network", "connect", "--alias", "voice", network, "abc123"),
    ]
    placed = next(
        i for i, c in enumerate(stage.commands) if "channel originate" in str(c)
    )
    assert stage.commands.index(network_commands(stage)[0]) < placed


def test_a_cut_in_the_middle_of_a_call_waits_for_the_answer_first(tmp_path):
    lines = [*router(116, "answered"), TEAM_RINGS]
    stage = Stage(tmp_path, {116: {"router": lines}})

    [(_, ok, detail)] = drill.drill_cut_from_asterisk_mid_call(stage)

    assert ok and detail.endswith("from the cut"), detail
    assert len(network_commands(stage)) == 2


def test_the_database_drills_cut_the_database_network(tmp_path):
    stage = Stage(tmp_path, {117: {"router": [*router(117, "answered"), TEAM_RINGS]}})
    drill.drill_cut_from_database_mid_call(stage)
    assert network_commands(stage)[0][2] == f"{stage.project}_backend"


def test_a_cut_voice_is_put_back_when_the_team_never_rings(tmp_path, monkeypatch):
    """Or every later drill would find it cut off."""
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = Stage(tmp_path, {118: {"router": router(118, "dials")}})

    with pytest.raises(drill.DrillError):
        drill.drill_cut_from_database(stage)
    assert network_commands(stage)[-1][1] == "connect"


def test_the_caller_of_the_database_cut_presses_keys():
    """Without a turn nothing asks the database in the middle of the call."""
    dialplan = (drill.CONFIG_DIR / "drill" / "extensions.conf").read_text()
    keys = next(line for line in dialplan.splitlines() if "?keys)" in line)
    assert '"117"' in keys


def test_the_database_cut_mid_call_runs_inside_the_opening_hours(tmp_path, monkeypatch):
    """Set back whatever fails, as for the team's drills."""
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = Stage(tmp_path, {117: {"router": router(117, "answered")}})

    with pytest.raises(drill.DrillError):
        drill.drill_cut_from_database_mid_call(stage)
    days = [c[1] for c in stage.commands if c[0] == "sql"]
    assert days == [drill.OPEN_ALL_DAY, drill.SET_THE_DAY_BACK]


def test_the_cut_drills_are_in_the_run_and_the_way_back_comes_last():
    run = [scenario for _, scenario in drill.SCENARIOS]
    assert {
        drill.drill_networks,
        drill.drill_cut_from_asterisk,
        drill.drill_cut_from_asterisk_mid_call,
        drill.drill_cut_from_database_mid_call,
        drill.drill_cut_from_database,
    } <= set(run)
    assert run[-1] is drill.drill_back_after_the_cuts


def test_the_way_back_fails_when_the_team_took_the_call(tmp_path):
    lines = [*router(119, "dials", "answered"), TEAM_RINGS]
    lines += router(119, "caller leg ended dialstatus=ANSWER")
    stage = Stage(tmp_path, {119: {"router": lines}})
    [(_, ok, _), _] = drill.drill_back_after_the_cuts(stage)
    assert not ok


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


# -- the team leg nobody answered (T-10.8) ---------------------------------------

CALLER_111 = drill.NUMBER_PREFIX + "111"


def missed_stage(
    tmp_path,
    n: int,
    *,
    card: str,
    told: str | None,
    row: str,
    dialstatus: str = "ANSWER",
) -> Stage:
    asterisk = [
        f"call 1.1: voice leg {dialstatus} ending [transfer]",
        "call 1.1: team leg BUSY",
        f"call 1.1: nobody at the team, card [{card}]",
    ]
    if told:
        asterisk.append(f"call 1.1: told the caller {told} SUCCESS")
    stage = Stage(
        tmp_path,
        {
            n: {
                "router": [
                    *router(n, "dials", "answered"),
                    TEAM_RINGS,
                    f"NoOp(DRILL {n} caller leg ended dialstatus={dialstatus})",
                ],
                "asterisk": asterisk,
            }
        },
    )
    stage.prints["external_session_id = '1.1'"] = row
    return stage


def test_a_team_nobody_answers_leaves_a_card_and_the_caller_hears_so(tmp_path):
    good = missed_stage(
        tmp_path,
        111,
        card="noted",
        told="missed_noted",
        row=f"callback|1|{CALLER_111}",
    )
    checks = drill.drill_team_missed(good)
    assert all(ok for _, ok, _ in checks), checks

    for card, told, row in (
        ("none", "missed_later", "transferred|0|"),  # no card written
        ("noted", None, f"callback|1|{CALLER_111}"),  # nothing played
        ("noted", "missed_noted", "callback|1|+4972215550999"),  # another number
        ("noted", "missed_noted", f"transferred|1|{CALLER_111}"),  # outcome kept
    ):
        stage = missed_stage(tmp_path, 111, card=card, told=told, row=row)
        assert not all(ok for _, ok, _ in drill.drill_team_missed(stage))


def test_the_team_drills_set_the_router_and_the_hours_back(tmp_path, monkeypatch):
    monkeypatch.setattr(drill, "TEAM_LIMIT_SECONDS", -9.9)
    stage = missed_stage(
        tmp_path, 111, card="noted", told="missed_noted", row="callback|1|x"
    )
    stage.script[111]["router"] = []  # the call never ends: the drill breaks

    with pytest.raises(drill.DrillError):
        drill.drill_team_missed(stage)

    clis = [c for c in stage.commands if "TEAM_AWAY" in " ".join(c)]
    assert "TEAM_AWAY answer" in " ".join(clis[-1])
    sqls = [c[1] for c in stage.commands if c[0] == "sql"]
    assert sqls[0] == drill.OPEN_ALL_DAY
    assert sqls[-1] == drill.SET_THE_DAY_BACK


@pytest.fixture
def day_engine(scratch_db_url):
    command.upgrade(alembic_config(scratch_db_url), "head")
    engine = create_engine(scratch_db_url)
    yield engine
    engine.dispose()


def _a_tenant(conn):
    return conn.execute(
        text(
            "INSERT INTO tenants (name, timezone) VALUES ('One', 'Europe/Berlin') "
            "RETURNING id"
        )
    ).scalar_one()


def _a_day(conn, tenant, note, *, status="set"):
    return conn.execute(
        text(
            "INSERT INTO special_days (tenant_id, date, closed, note, status) VALUES "
            "(:t, (now() at time zone 'Europe/Berlin')::date, true, :n, :s) "
            "RETURNING id"
        ),
        {"t": tenant, "n": note, "s": status},
    ).scalar_one()


def _days(conn):
    return conn.execute(
        text(
            "SELECT note, closed, opens_at, status FROM special_days "
            "ORDER BY status, note"
        )
    ).all()


def test_a_day_with_a_row_of_its_own_stands_aside_and_comes_back(day_engine):
    """#207: one counting row per date (migration 017), so the drill's row
    aborted every team drill on a holiday the team had answered."""
    with day_engine.begin() as conn:
        tenant = _a_tenant(conn)
        day = _a_day(conn, tenant, "Betriebsferien")

        conn.exec_driver_sql(drill.OPEN_ALL_DAY)
        assert _days(conn) == [("drill", False, time(0), "set")]

        conn.exec_driver_sql(drill.SET_THE_DAY_BACK)
        assert _days(conn) == [("Betriebsferien", True, None, "set")]
        assert conn.execute(text("SELECT id FROM special_days")).scalar_one() == day


def test_a_killed_drill_leaves_nothing_in_the_way(day_engine):
    """Its row stayed, and so did the day it had set aside."""
    with day_engine.begin() as conn:
        tenant = _a_tenant(conn)
        _a_day(conn, tenant, "Betriebsferien")
        conn.exec_driver_sql(drill.OPEN_ALL_DAY)

        conn.exec_driver_sql(drill.OPEN_ALL_DAY)
        assert _days(conn) == [("drill", False, time(0), "set")]

        conn.exec_driver_sql(drill.SET_THE_DAY_BACK)
        assert _days(conn) == [("Betriebsferien", True, None, "set")]


def test_a_day_without_a_row_gets_none_back(day_engine):
    with day_engine.begin() as conn:
        _a_tenant(conn)
        conn.exec_driver_sql(drill.OPEN_ALL_DAY)
        conn.exec_driver_sql(drill.SET_THE_DAY_BACK)

        assert _days(conn) == []


def test_a_suggestion_of_the_day_is_left_alone(day_engine):
    """It counts for nothing and has a key of its own (migration 015)."""
    with day_engine.begin() as conn:
        tenant = _a_tenant(conn)
        _a_day(conn, tenant, "Allerheiligen", status="suggested")

        conn.exec_driver_sql(drill.OPEN_ALL_DAY)
        conn.exec_driver_sql(drill.SET_THE_DAY_BACK)

        assert _days(conn) == [("Allerheiligen", True, None, "suggested")]


def test_a_call_the_ai_never_answered_gets_a_card_and_no_sentence(tmp_path):
    def paused(told: str | None) -> Stage:
        return missed_stage(
            tmp_path,
            112,
            card="noted",
            told=told,
            row=f"callback|1|{drill.NUMBER_PREFIX}112",
            dialstatus="NOANSWER",
        )

    assert all(ok for _, ok, _ in drill.drill_team_missed_paused(paused(None)))
    assert not all(
        ok for _, ok, _ in drill.drill_team_missed_paused(paused("missed_noted"))
    )


def test_with_voice_down_the_caller_still_hears_to_call_again(tmp_path):
    stage = missed_stage(
        tmp_path, 113, card="", told="missed_later", row="transferred|0|"
    )

    checks = drill.drill_team_missed_voice_down(stage)

    assert all(ok for _, ok, _ in checks), checks
    assert ("stop", "voice") in stage.commands
    assert stage.commands.index(("stop", "voice")) < stage.commands.index(
        ("up", "-d", "--wait", "voice")
    )
    promised = missed_stage(
        tmp_path, 113, card="", told="missed_noted", row="transferred|0|"
    )
    assert not all(ok for _, ok, _ in drill.drill_team_missed_voice_down(promised))
    # A card written although `voice` gave no answer: not what the point says.
    written = missed_stage(
        tmp_path, 113, card="", told="missed_later", row=f"callback|1|{CALLER_111}"
    )
    assert not all(ok for _, ok, _ in drill.drill_team_missed_voice_down(written))


def test_the_announcements_asterisk_plays_are_the_sentences_of_the_code(tmp_path):
    stage = Stage(tmp_path, {})
    stage.prints["ANNOUNCEMENTS"] = '{"missed_noted": "Ja.", "missed_later": "Nein."}'
    # The fake mouth's audio is the sentence, then silence.
    stage.prints["missed_noted.sln16"] = "Ja.\x00"
    stage.prints["missed_later.sln16"] = "Nein.\x00"

    [(_, ok, detail)] = drill.drill_announcements(stage)
    assert ok, detail

    for stale in (
        "Vielleicht",  # another sentence
        "Nein. Und",  # an older, longer sentence that begins the same way
        drill.DrillError("head: No such file or directory"),  # none at all
    ):
        stage.prints["missed_later.sln16"] = stale
        [(_, ok, detail)] = drill.drill_announcements(stage)
        assert not ok and "missed_later" in detail


def _blocked_stage(tmp_path, dropped: str = "12\n") -> Stage:
    stage = Stage(
        tmp_path,
        {
            128: {
                "router": [
                    *router(128, "dials", "answered"),
                    "NoOp(DRILL 128 caller leg ended dialstatus=ANSWER)",
                ],
                "asterisk": ["call 1.1: voice leg ANSWER ending [hangup]"],
            }
        },
    )
    stage.prints["line_note"] = "no_caller_audio"
    stage.prints["ps -q asterisk"] = "abc123\n"
    stage.prints["iptables -vxn"] = dropped
    return stage


def _rule_commands(stage: Stage) -> list[tuple[str, ...]]:
    return [c for c in stage.commands if c[:2] == ("run", "--rm")]


def test_a_caller_whose_rtp_is_blocked_is_noted_and_the_rule_comes_off(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(drill, "NOTE_WAIT_SECONDS", 0)
    stage = _blocked_stage(tmp_path)

    checks = drill.drill_caller_rtp_blocked(stage)

    assert all(ok for _, ok, _ in checks), checks
    assert len(checks) == 3
    on, off = _rule_commands(stage)
    for run in (on, off):
        assert "NET_ADMIN" in run and "container:abc123" in run
    assert "iptables -I INPUT -p udp --dport 10000:10039 -j DROP" in on[-1]
    assert "iptables -D INPUT -p udp --dport 10000:10039 -j DROP" in off[-1]
    placed = next(
        i for i, c in enumerate(stage.commands) if "channel originate" in str(c)
    )
    assert stage.commands.index(on) < placed < stage.commands.index(off)


def test_a_blocked_leg_that_sent_no_packet_measures_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(drill, "NOTE_WAIT_SECONDS", 0)
    *_, (_, ok, detail) = drill.drill_caller_rtp_blocked(_blocked_stage(tmp_path, ""))
    assert not ok and detail.startswith("0 packets")


def test_a_blocked_leg_without_the_note_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(drill, "NOTE_WAIT_SECONDS", 0)
    stage = _blocked_stage(tmp_path)
    stage.prints["line_note"] = "none"
    checks = drill.drill_caller_rtp_blocked(stage)
    assert [ok for _, ok, _ in checks] == [True, False, True]


def test_a_docker_that_refuses_the_rule_says_so_and_places_no_call(tmp_path):
    stage = _blocked_stage(tmp_path)
    stage.prints["iptables -I"] = drill.DrillError("operation not permitted")

    [(_, ok, detail)] = drill.drill_caller_rtp_blocked(stage)

    assert not ok and "operation not permitted" in detail
    assert not any("channel originate" in str(c) for c in stage.commands)


def test_the_rule_comes_off_when_the_call_breaks(tmp_path, monkeypatch):
    def broken(*_args):
        raise drill.DrillError("a call of the last drill does not end")

    monkeypatch.setattr(drill, "silent_call", broken)
    stage = _blocked_stage(tmp_path)

    with pytest.raises(drill.DrillError):
        drill.drill_caller_rtp_blocked(stage)
    assert "iptables -D" in _rule_commands(stage)[-1][-1]


def test_the_blocked_ports_are_the_media_range_and_its_caller_speaks():
    rtp = (drill.CONFIG_DIR / "conf" / "rtp.conf").read_text()
    start, end = drill.RTP_PORTS.split(":")
    assert f"rtpstart={start}" in rtp and f"rtpend={end}" in rtp
    dialplan = (drill.CONFIG_DIR / "drill" / "extensions.conf").read_text()
    speaks = next(line for line in dialplan.splitlines() if "?speaks)" in line)
    assert '"128"' in speaks
    assert drill.drill_caller_rtp_blocked in [s for _, s in drill.SCENARIOS]


def test_the_number_of_the_blocked_caller_must_not_show_in_the_logs(tmp_path):
    stage = Stage(tmp_path, {})
    stage.logs["asterisk"].append(f"Dial({drill.NUMBER_PREFIX}128)")
    assert not stage.no_caller_in_the_logs()[1]
