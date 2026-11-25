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

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asterisk_image_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")

# A docker that answers like a healthy Asterisk, or one that dies at the first
# failed media connection (DIES set), or one that is seen dead from the
# DIES_AT_INSPECT-th liveness check on. Every failed Dial takes FAKE_MS; the
# module NOT_RUNNING is listed as loaded but not running. It copies the configuration it is handed
# to SEEN and counts the originated calls, so `logs` shows what ran.
FAKE_DOCKER = r"""#!/bin/sh
state=$FAKE_STATE
case "$1" in
  run)
    for arg in "$@"; do
      case "$arg" in *:/etc/asterisk:ro) cp -r "${arg%%:/etc/asterisk:ro}" "$state/seen" ;; esac
    done
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
      *"module show like"*) echo "chan_x.so   Some module   0 Running   core" ;;
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
        echo "NOTICE: check ended CHANUNAVAIL after ${FAKE_MS:-4000} ms"; i=$((i + 1))
      done
    fi
    [ -e "$state/next" ] && echo "NOTICE: check next call runs"
    : ;;
esac
exit 0
"""


def _run(tmp_path, *, dies=False, image="maex-asterisk:ci", dial_ms=None, **fake_env):
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
    args = ["sh", str(SCRIPT)] + ([image] if image else [])
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
    assert "FAIL  voice frozen: a Dial ended after 5 ms" in run.stdout


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
    "auth": "chan_pjsip",
    "aor": "chan_pjsip",
    "endpoint": "chan_pjsip",
    "identify": "res_pjsip_endpoint_identifier_ip",
}
# Not named by any line: the bridge of a team leg, alaw to the media
# channel's slin16, and the announcements (`.sln16`).
MEDIA_MODULES = {"bridge_simple", "bridge_native_rtp", "codec_resample", "format_sln"}


def _dialplan_names():
    conf = ROOT / "deploy/asterisk/conf"
    text = "\n".join(
        line
        for name in ("extensions.conf", "features.conf")
        for line in (conf / name).read_text().splitlines()
        if not line.lstrip().startswith(";")
    )
    apps = set(re.findall(r"(?:=>\s*[^,\n]+,|same\s*=>\s*[^,\n]+,|[?(,])([A-Z][A-Za-z]+)\(", text))
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
    assert types <= PJSIP_MODULES.keys(), f"add the module of {sorted(types - PJSIP_MODULES.keys())}"
    needed = {DIALPLAN_MODULES[name] for name in names} | {PJSIP_MODULES[t] for t in types}
    needed = (needed | MEDIA_MODULES) - {None}
    listed = set(re.search(r"for module in (.*?); do", SCRIPT.read_text(), re.S).group(1).split())
    assert needed - listed == set()
