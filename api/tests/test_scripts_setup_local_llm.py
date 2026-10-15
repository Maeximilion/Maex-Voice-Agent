"""`scripts/setup_local_llm.sh` runs the Ollama installer only when its SHA-256 matches (#252).

The download and the installer run are stubbed on PATH: `curl` writes a fake installer
and logs the URL, `env` (the script starts the installer through it) logs its arguments
and exits 97 so nothing after the installer runs. The script itself is Linux only.
"""

import hashlib
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup_local_llm.sh"
FAKE_INSTALLER = "#!/bin/sh\necho fake installer\n"
STOP = 97


@pytest.fixture(scope="module", autouse=True)
def _linux_without_ollama():
    if platform.system() == "Linux" and shutil.which("ollama") is None:
        return
    if os.environ.get("CI"):
        pytest.fail("needs Linux without ollama on PATH")
    pytest.skip("needs Linux without ollama on PATH")


def _stubs(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "log"
    (stubs / "curl").write_text(
        "#!/bin/sh\n"
        f'echo "curl $*" >> {log}\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        f'[ -n "$out" ] && printf %s "$FAKE_INSTALLER" > "$out"\n'
    )
    (stubs / "env").write_text(f'#!/bin/sh\necho "env $*" >> {log}\nexit {STOP}\n')
    (stubs / "zstd").write_text("#!/bin/sh\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "FAKE_INSTALLER": FAKE_INSTALLER,
    }


def _run(
    script: Path, tmp_path: Path, *args: str
) -> tuple[subprocess.CompletedProcess, str]:
    done = subprocess.run(
        ["bash", str(script), "--no-smoke", *args],
        env=_stubs(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = tmp_path / "log"
    return done, log.read_text() if log.exists() else ""


def _with_digest(tmp_path: Path, digest: str) -> Path:
    source = SCRIPT.read_text()
    patched, count = re.subn(
        r'^INSTALLER_SHA256="[0-9a-f]{64}"$',
        f'INSTALLER_SHA256="{digest}"',
        source,
        flags=re.M,
    )
    assert count == 1
    copy = tmp_path / "setup_local_llm.sh"
    copy.write_text(patched)
    return copy


def _version() -> str:
    found = re.search(r'^OLLAMA_VERSION="([0-9.]+)"$', SCRIPT.read_text(), re.M)
    assert found
    return found.group(1)


def test_matching_digest_runs_the_pinned_installer(tmp_path):
    digest = hashlib.sha256(FAKE_INSTALLER.encode()).hexdigest()
    done, log = _run(_with_digest(tmp_path, digest), tmp_path)
    version = _version()
    assert done.returncode == STOP, done.stderr
    assert (
        f"https://github.com/ollama/ollama/releases/download/v{version}/install.sh"
        in log
    )
    assert f"env OLLAMA_VERSION={version} sh " in log


def test_other_installer_is_not_run(tmp_path):
    done, log = _run(SCRIPT, tmp_path)
    assert done.returncode == 1
    assert "does not match the SHA-256" in done.stderr
    assert "curl " in log
    assert "env " not in log


def test_dry_run_downloads_and_runs_nothing(tmp_path):
    done, log = _run(SCRIPT, tmp_path, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "[dry-run] check SHA-256" in done.stdout
    assert "install.sh" not in log
    assert "env " not in log
