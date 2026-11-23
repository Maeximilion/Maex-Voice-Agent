"""scripts/asterisk_image_check.sh: the check of the own Asterisk image (#775).

The real run needs Docker and an image (the CI job asterisk-image); here a fake
`docker` on PATH stands in for it, so what the script decides is tested: the
product's own media Dial is what it dials, every failure must end CHANUNAVAIL
with Asterisk up, and a crash fails the check.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asterisk_image_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")

# A docker that answers like a healthy Asterisk, or one that dies at the first
# failed media connection (DIES set). It copies the configuration it is handed
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
      while [ "$i" -lt "$calls" ]; do echo "NOTICE: check ended CHANUNAVAIL"; i=$((i + 1)); done
    fi
    [ -e "$state/next" ] && echo "NOTICE: check next call runs"
    : ;;
esac
exit 0
"""


def _run(tmp_path, *, dies=False, image="maex-asterisk:ci"):
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
