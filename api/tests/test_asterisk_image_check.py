"""scripts/asterisk_image_check.sh: the check of the own Asterisk image (#775).

The real run needs Docker and an image (the CI job asterisk-image); here a fake
`docker` on PATH stands in for it, so what the script decides is tested: the
product's own media Dial is what it dials, every failure must end CHANUNAVAIL
with Asterisk up, and a crash fails the check.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asterisk_image_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")

# A docker that answers like a healthy Asterisk, or one that dies at the first
# failed media connection (DIES set), or one that is seen dead from the
# DIES_AT_INSPECT-th liveness check on. Every failed Dial takes FAKE_MS; the
# module NOT_RUNNING is listed as loaded but not running, and for
# ONLY_LONGER only a module whose name starts with it runs. It keeps how the
# Asterisk container was created in `create`, copies the configuration it is
# handed to `seen` and counts the originated calls, so `logs` shows what ran.
FAKE_DOCKER = r"""#!/bin/sh
state=$FAKE_STATE
case "$1" in
  create)
    echo "$*" > "$state/create"
    echo id ;;
  cp)
    cp -r "${2%/.}" "$state/seen" ;;
  run)
    echo id ;;
  inspect)
    calls=$(cat "$state/calls" 2>/dev/null || echo 0)
    case "$*" in
      *ExitCode*) echo "exit 139" ;;
      *)
        seen=$(( $(cat "$state/inspects" 2>/dev/null || echo 0) + 1 ))
        echo "$seen" > "$state/inspects"
        if [ -n "${DIES:-}" ] && [ "$calls" -ge 1 ]; then echo false
        elif [ -n "${DIES_AT_INSPECT:-}" ] && [ "$seen" -ge "$DIES_AT_INSPECT" ]; then echo false
        else echo true; fi ;;
    esac ;;
  exec)
    shift 2
    case "$*" in
      *"core show version"*) echo "Asterisk 22.12.0 built by fake" ;;
      *"module show like ${NOT_RUNNING:-none}"*)
        echo "${NOT_RUNNING}.so   Some module   0 Not Running   core" ;;
      *"module show like"*)
        name=${*##* }
        if [ "$name" = "${ONLY_LONGER:-none}" ]; then
          echo "${name}_gsm.so   Some module   0 Running   core"
        else
          echo "$name.so   Some module   0 Running   core"
        fi ;;
      *"originate Local/fail"*)
        calls=$(cat "$state/calls" 2>/dev/null || echo 0)
        echo $((calls + 1)) > "$state/calls" ;;
      *"originate Local/next"*) touch "$state/next" ;;
    esac ;;
  logs)
    calls=$(cat "$state/calls" 2>/dev/null || echo 0)
    if [ -z "${DIES:-}" ]; then
      i=0
      while [ "$i" -lt "$calls" ]; do
        echo "NOTICE: check ended CHANUNAVAIL after ${FAKE_MS-4000} ms"; i=$((i + 1))
      done
    fi
    [ -e "$state/next" ] && echo "NOTICE: check next call runs"
    : ;;
esac
exit 0
"""


def _run(
    tmp_path,
    *,
    dies=False,
    image="maex-asterisk:ci",
    dial_ms=None,
    script=SCRIPT,
    **fake_env,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER, newline="\n")
    fake.chmod(0o755)
    # A sleep that returns at once keeps the waits of the script short.
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    sleep.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_STATE": str(state),
    }
    if dies:
        env["DIES"] = "1"
    if dial_ms is not None:
        env["FAKE_MS"] = str(dial_ms)
    # DIES_AT_INSPECT, NOT_RUNNING: see FAKE_DOCKER.
    env.update({name.upper(): str(value) for name, value in fake_env.items()})
    args = ["sh", str(script)] + ([image] if image else [])
    run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    return run, state


def test_a_healthy_asterisk_passes(tmp_path):
    run, _ = _run(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "FAIL" not in run.stdout
    for voice in ("refused", "unresolved", "frozen"):
        assert f"PASS  voice {voice}: 3 failed media connections" in run.stdout
    assert "PASS  the call after the failures runs" in run.stdout


def test_it_dials_the_products_own_media_line(tmp_path):
    _, state = _run(tmp_path)
    product = (ROOT / "deploy/asterisk/conf/extensions.conf").read_text()
    dial = next(
        line.split("n,", 1)[1]
        for line in product.splitlines()
        if "Dial(WebSocket/" in line and not line.lstrip().startswith(";")
    )
    assert dial in (state / "seen" / "extensions.conf").read_text()
    seen = (state / "seen" / "websocket_client.conf").read_text()
    assert "uri=ws://voice:8001/media" in seen
    assert "${VOICE_MEDIA_TOKEN}" not in seen


def test_an_asterisk_that_dies_fails_the_check(tmp_path):
    run, _ = _run(tmp_path, dies=True)
    assert run.returncode == 1
    assert "FAIL  voice refused: Asterisk died (exit 139)" in run.stdout


def test_without_an_image_it_says_how_to_use_it(tmp_path):
    run, _ = _run(tmp_path, image="")
    assert run.returncode == 2
    assert "usage:" in run.stderr


def test_a_frozen_voice_that_ends_the_dial_at_once_fails_the_check(tmp_path):
    """A `voice` that takes the connection and answers nothing has to hold the
    Dial for the connection timeout of websocket_client.conf (Asterisk issue
    #1979). A Dial that ends sooner never met a frozen `voice`: the check's
    stand-in closed the connection, and a release that waits for ever on a
    silent `voice` would pass (deep review of #972)."""
    run, _ = _run(tmp_path, dial_ms=5)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  voice frozen: Dials took 5 5 5 ms" in run.stdout


def test_frozen_dials_whose_time_cannot_be_read_fail_the_check(tmp_path):
    """No readable duration is no proof of a frozen `voice` (sweep of the deep
    review of #972): the check failed only on a number it could read."""
    run, _ = _run(tmp_path, dial_ms="")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  voice frozen: Dials took" in run.stdout


def test_a_frozen_dial_that_outlasts_the_timeout_fails_the_check(tmp_path):
    """A release that ignores `connection_timeout` and holds the caller for
    most of a minute before the team rings must not pass (sweep of the deep
    review of #972)."""
    run, _ = _run(tmp_path, dial_ms=19000)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  voice frozen: Dials took 19000 19000 19000 ms" in run.stdout


def test_a_module_is_matched_by_its_whole_name(tmp_path):
    """`module show like format_wav` also lists format_wav_gsm; that one running
    says nothing about format_wav (sweep of the deep review of #972)."""
    run, _ = _run(tmp_path, only_longer="format_wav")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  format_wav does not run" in run.stdout


def test_the_frozen_voice_holds_every_connection():
    """busybox nc with its stdin at /dev/null closes a connection at once (seen
    in the CI log of #972: three "frozen" Dials in 0.29 s); with `-e sleep` it
    holds each one, `-k` takes the next."""
    script = SCRIPT.read_text()
    assert "nc -lk -p 8001 -e sleep" in script


def test_a_patch_that_does_not_apply_fails_the_image_build(tmp_path):
    """The Dockerfile applies Alpine's musl patches in a loop; a loop's status
    is its last command's, so a patch that stopped applying after a version
    bump would build an image that deadlocks at boot (deep review of #972)."""
    if shutil.which("patch") is None:
        pytest.skip("needs patch")
    dockerfile = (ROOT / "deploy/asterisk/image/Dockerfile").read_text()
    loop = re.search(r"for p in /patches/\*\.patch; do .*?; done", dockerfile)
    assert loop, "Dockerfile has no patch loop"
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "10-broken.patch").write_text(
        "--- a/missing.c\n+++ b/missing.c\n@@ -1 +1 @@\n-old\n+new\n", newline="\n"
    )
    (patches / "20-fine.patch").write_text(
        "--- /dev/null\n+++ b/fine.c\n@@ -0,0 +1 @@\n+fine\n", newline="\n"
    )
    src = tmp_path / "src"
    src.mkdir()
    command = loop.group(0).replace("/patches/", f"{patches.as_posix()}/")
    run = subprocess.run(
        ["sh", "-c", command],
        cwd=src,
        stdin=subprocess.DEVNULL,
        # No terminal: GNU patch would ask there for the file it cannot find.
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode != 0, run.stdout + run.stderr


def test_an_asterisk_seen_dead_only_at_the_last_check_fails_it(tmp_path):
    """Asterisk issue #2121 crashed Asterisk when a call's thread ended, after
    its last log line. A death that only the last liveness check sees must fail
    the check, not skip the next call and exit 0 (deep review of #972)."""
    _, state = _run(tmp_path / "healthy")
    checks = int((state / "inspects").read_text())
    run, _ = _run(tmp_path / "late", dies_at_inspect=checks)
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  Asterisk died after the failures" in run.stdout


def test_a_module_that_is_not_running_fails_the_check(tmp_path):
    """`module show` lists a module that loaded and declined as "Not Running"
    (seen on 22.12.0), which also holds " Running " (deep review of #972)."""
    run, _ = _run(tmp_path, not_running="chan_websocket")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "FAIL  chan_websocket does not run" in run.stdout


# The module of every application and function the product's dialplan and
# features.conf use, and of every PJSIP object type pjsip.conf sets up. With
# `autoload=yes` a missing one stops neither the boot nor the healthcheck: the
# call hits it and ends, the team never rung (rule 5). None: in the core.
DIALPLAN_MODULES = {
    "Dial": "app_dial",
    "ExecIf": "app_exec",
    "TryExec": "app_exec",
    "Gosub": "app_stack",
    "Return": "app_stack",
    "Log": "app_verbose",
    "MixMonitor": "app_mixmonitor",
    "StopMixMonitor": "app_mixmonitor",
    "Playback": "app_playback",
    "TrySystem": "app_system",
    "Goto": None,
    "GotoIf": None,
    "Hangup": None,
    "Set": None,
    "CALLERID": "func_callerid",
    "CURL": "func_curl",
    "CURLOPT": "func_curl",
    "ENV": "func_env",
    "STAT": "func_env",
    "FIELDNUM": "func_strings",
    "FILTER": "func_strings",
    "IF": "func_logic",
}
PJSIP_MODULES = {
    "global": "chan_pjsip",
    "acl": "res_pjsip_acl",
    "transport": "chan_pjsip",
    "registration": "res_pjsip_outbound_registration",
    # As `outbound_auth`: the registration and the team legs the access challenges.
    "auth": "res_pjsip_outbound_authenticator_digest",
    "aor": "chan_pjsip",
    "endpoint": "chan_pjsip",
    "identify": "res_pjsip_endpoint_identifier_ip",
}
# Not named by any line: the bridge of a team leg, alaw to the media
# channel's slin16, and the announcements (`.sln16`).
MEDIA_MODULES = {
    "bridge_simple",
    "bridge_native_rtp",
    "codec_resample",
    "format_sln",
    # Every PJSIP call: its session and the SDP of its audio.
    "res_pjsip_session",
    "res_pjsip_sdp_rtp",
}


def _dialplan_names():
    conf = ROOT / "deploy/asterisk/conf"
    text = "\n".join(
        line
        for name in ("extensions.conf", "features.conf")
        for line in (conf / name).read_text().splitlines()
        if not line.lstrip().startswith(";")
    )
    apps = set(
        re.findall(
            r"(?:=>\s*[^,\n]+,|same\s*=>\s*[^,\n]+,|[?(,])([A-Z][A-Za-z]+)\(", text
        )
    )
    funcs = set(re.findall(r"(?:\$\{|Set\()([A-Z_]{2,})\(", text))
    return (apps | funcs) - {"WebSocket", "PJSIP"}


def test_the_check_runs_every_module_the_product_uses():
    """A slimmer recipe that drops one of them keeps CI green unless the check
    names it (deep review of #972)."""
    names = _dialplan_names()
    unknown = names - DIALPLAN_MODULES.keys()
    assert not unknown, f"add the module of {sorted(unknown)} here and to the check"
    pjsip = (ROOT / "deploy/asterisk/conf/pjsip.conf").read_text()
    types = set(re.findall(r"^type=(\w+)", pjsip, re.M))
    assert types <= PJSIP_MODULES.keys(), (
        f"add the module of {sorted(types - PJSIP_MODULES.keys())}"
    )
    needed = {DIALPLAN_MODULES[name] for name in names} | {
        PJSIP_MODULES[t] for t in types
    }
    needed = (needed | MEDIA_MODULES) - {None}
    loop = re.search(r"for module in (.*?); do", SCRIPT.read_text(), re.S)
    assert loop, "the check has no module loop"
    assert needed - set(loop.group(1).split()) == set()


def test_asterisk_starts_through_the_images_entrypoint_as_its_user(tmp_path):
    """The product starts Asterisk through the image's entrypoint, which hands
    the runtime folders to the asterisk user, and drops to that user
    (`-U asterisk`, docker-compose.yml). Started as root past the entrypoint,
    the check would stay green on a recipe that breaks either (deep review of
    #972)."""
    _, state = _run(tmp_path)
    created = (state / "create").read_text()
    assert "--entrypoint" not in created


def _copy_of_the_check(tmp_path, extensions):
    """The check in a tree of its own, with the given extensions.conf."""
    for part in ("scripts", "deploy/asterisk/conf", "deploy/asterisk/image"):
        (tmp_path / part).mkdir(parents=True)
    for name in ("scripts/asterisk_image_check.sh", "deploy/asterisk/image/Dockerfile"):
        (tmp_path / name).write_text((ROOT / name).read_text(), newline="\n")
    conf = ROOT / "deploy/asterisk/conf/websocket_client.conf"
    (tmp_path / "deploy/asterisk/conf/websocket_client.conf").write_text(
        conf.read_text()
    )
    (tmp_path / "deploy/asterisk/conf/extensions.conf").write_text(extensions)
    return tmp_path / "scripts/asterisk_image_check.sh"


def test_a_dialplan_without_the_media_dial_is_named(tmp_path):
    """`set -e` ended the check at the empty grep, exit 1 without a word, before
    its own message (deep review of #972)."""
    script = _copy_of_the_check(tmp_path, "[ai]\nexten => s,1,Hangup()\n")
    run = subprocess.run(
        ["sh", str(script), "img"], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 2, run.stdout + run.stderr
    assert "no media Dial in extensions.conf" in run.stderr


def test_a_commented_media_dial_is_not_the_one_dialled(tmp_path):
    """The check dials the product's line, never one in a comment above it."""
    script = _copy_of_the_check(
        tmp_path,
        "; was: Dial(WebSocket/old/c(slin16),5)\n"
        "[ai]\nexten => s,1,Dial(WebSocket/voice/c(slin16),10)\n",
    )
    _, state = _run(tmp_path / "run", script=script)
    seen = (state / "seen" / "extensions.conf").read_text()
    assert "Dial(WebSocket/voice/c(slin16),10)" in seen
    assert "WebSocket/old" not in seen


def test_every_question_to_asterisk_is_bounded():
    """A deadlocked loader or a held lock (#2121) would hang `asterisk -rx`
    until the job's 30 minutes, without the check's message."""
    cli = re.search(r"^cli\(\) \{(.*)\}$", SCRIPT.read_text(), re.M)
    assert cli and "timeout " in cli.group(1)


def test_ci_on_main_judges_every_commit_of_a_push():
    """A stack merge pushes several merge commits at once; the Asterisk gate on
    `main` reads the whole push, not only its last commit. Its base can lie
    outside the checkout's depth: fetched first, and a diff that still fails
    is held out of the pipeline's status whatever the shell's pipefail (Codex
    on #1026), so `changes` never fails and the empty list builds."""
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    job = re.search(r"\n  changes:\n(.*?)(?=\n  [a-z-]+:\n)", ci, re.S)
    assert job, "ci.yml has no job changes"
    on_main = job.group(1).split("else", 1)[1]
    assert "github.event.before" in on_main
    assert re.search(
        r"git fetch [^\n]*--depth=1 origin \"\$base\"[^\n]*\|\| true", on_main
    )
    assert re.search(r"\{ git diff [^\n]*\"\$base\" HEAD \|\| true; \}", on_main)


def _compose_asterisk():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return compose["services"]["asterisk"]


def test_asterisk_starts_with_the_products_command_and_rights(tmp_path):
    """Same flags (`-p` starts the canary and asks for a real-time priority it
    may not get) and the same capabilities as the service in
    docker-compose.yml: a flag or a right the product's start needs and the
    image refuses would otherwise pass (deep review of #972)."""
    service = _compose_asterisk()
    _, state = _run(tmp_path)
    args = (state / "create").read_text().split()
    command = service["command"]
    assert args[-len(command) :] == command
    # Its rights and nothing more: every option before the image is one of
    # the service's, or the container's own name and network.
    options = args[1 : -len(command) - 1]
    given = set(zip(options[::2], options[1::2], strict=True))
    wanted = {
        (flag, value)
        for flag, key in (
            ("--cap-drop", "cap_drop"),
            ("--cap-add", "cap_add"),
            ("--security-opt", "security_opt"),
        )
        for value in service[key]
    }
    assert given - {p for p in given if p[0] in ("--name", "--network")} == wanted


def test_dependabot_keeps_the_asterisk_base_apart():
    """An Alpine step in the Asterisk image (patches, package names, the busybox
    tools render.sh reads) moves by hand with the drills, and never blocks
    the API's base bump in a shared PR; the image name of our own build is on
    no registry (deep review of #972)."""
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    updates = config["updates"]
    own = [u for u in updates if "/deploy/asterisk/image" in u.get("directories", [])]
    assert len(own) == 1 and own[0]["directories"] == ["/deploy/asterisk/image"]
    ignored = {
        (rule["dependency-name"], kind)
        for rule in own[0].get("ignore", [])
        for kind in rule.get("update-types", [])
    }
    assert ("alpine", "version-update:semver-major") in ignored
    assert ("alpine", "version-update:semver-minor") in ignored
    compose = [
        u
        for u in updates
        if u["package-ecosystem"] == "docker-compose" and "/" in u["directories"]
    ]
    names = {rule["dependency-name"] for rule in compose[0].get("ignore", [])}
    assert "maex-voice-agent/*" in names
