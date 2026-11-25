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
# failed media connection (DIES set). Every failed Dial takes FAKE_MS. It copies the configuration it is handed
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
      *) if [ -n "${DIES:-}" ] && [ "$calls" -ge 1 ]; then echo false; else echo true; fi ;;
    esac ;;
  exec)
    shift 2
    case "$*" in
      *"core show version"*) echo "Asterisk 22.12.0 built by fake" ;;
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


def _run(tmp_path, *, dies=False, image="maex-asterisk:ci", dial_ms=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER, newline="\n")
    fake.chmod(0o755)
    # A sleep that returns at once keeps the waits of the script short.
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    sleep.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_STATE": str(state),
    }
    if dies:
        env["DIES"] = "1"
    if dial_ms is not None:
        env["FAKE_MS"] = str(dial_ms)
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
