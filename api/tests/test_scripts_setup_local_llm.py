"""`scripts/setup_local_llm.sh` runs the pinned Ollama installer on the pinned archive (#252).

The installer and the release archive are checked against their SHA-256 before the
installer runs, and the installer's one network download of that archive is replaced
by the checked file. The downloads and the installer run are stubbed on PATH: `curl`
writes a fake installer or a fake archive by URL and logs the URL, `env` (the script
starts the installer through it) logs its arguments, keeps a copy of the installer it
was handed and exits 97 so nothing after the installer runs. The script itself is
Linux only.
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
DOWNLOAD_LINE = (
    'download_and_extract "https://ollama.com/download" "$OLLAMA_INSTALL_DIR" '
    '"ollama-linux-${ARCH}"'
)
FAKE_INSTALLER = f"#!/bin/sh\necho fake installer\n{DOWNLOAD_LINE}\n"
FAKE_ARCHIVE = "fake archive"
STOP = 97


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def _linux_without_ollama():
    if platform.system() == "Linux" and shutil.which("ollama") is None:
        return
    if os.environ.get("CI"):
        pytest.fail("needs Linux without ollama on PATH")
    pytest.skip("needs Linux without ollama on PATH")


def _stubs(tmp_path: Path, installer: str) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "log"
    (stubs / "curl").write_text(
        "#!/bin/sh\n"
        f'echo "curl $*" >> {log}\n'
        'for a; do case "$a" in http*) url="$a" ;; esac; done\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
        'case "$url" in *install.sh) body="$FAKE_INSTALLER" ;; *) body="$FAKE_ARCHIVE" ;; esac\n'
        '[ -n "$out" ] && printf %s "$body" > "$out"\n'
    )
    (stubs / "env").write_text(
        "#!/bin/sh\n"
        f'echo "env $*" >> {log}\n'
        'for a; do last="$a"; done\n'
        f'cp "$last" {tmp_path / "ran.sh"}\n'
        f"exit {STOP}\n"
    )
    (stubs / "zstd").write_text("#!/bin/sh\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "FAKE_INSTALLER": installer,
        "FAKE_ARCHIVE": FAKE_ARCHIVE,
    }


def _run(
    script: Path, tmp_path: Path, *args: str, installer: str = FAKE_INSTALLER
) -> tuple[subprocess.CompletedProcess, str]:
    done = subprocess.run(
        ["bash", str(script), "--no-smoke", *args],
        env=_stubs(tmp_path, installer),
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = tmp_path / "log"
    return done, log.read_text() if log.exists() else ""


def _with_digests(tmp_path: Path, installer: str, archive: str) -> Path:
    source = SCRIPT.read_text()
    source, count = re.subn(
        r'^INSTALLER_SHA256="[0-9a-f]{64}"$',
        f'INSTALLER_SHA256="{installer}"',
        source,
        flags=re.M,
    )
    assert count == 1
    source, count = re.subn(
        r'^(ARCHIVE_SHA256_(?:AMD64|ARM64))="[0-9a-f]{64}"$',
        rf'\1="{archive}"',
        source,
        flags=re.M,
    )
    assert count == 2
    copy = tmp_path / "setup_local_llm.sh"
    copy.write_text(source)
    return copy


def _version() -> str:
    found = re.search(r'^OLLAMA_VERSION="([0-9.]+)"$', SCRIPT.read_text(), re.M)
    assert found
    return found.group(1)


def _release() -> str:
    return f"https://github.com/ollama/ollama/releases/download/v{_version()}"


def test_matching_digests_run_the_pinned_installer_on_the_checked_archive(tmp_path):
    script = _with_digests(tmp_path, _sha(FAKE_INSTALLER), _sha(FAKE_ARCHIVE))
    done, log = _run(script, tmp_path)

    assert done.returncode == STOP, done.stderr
    assert f"{_release()}/install.sh" in log
    assert re.search(
        rf"{re.escape(_release())}/ollama-linux-(amd64|arm64)\.tar\.zst", log
    )
    assert f"env OLLAMA_VERSION={_version()} sh " in log
    # The installer that ran extracts the checked file; it fetches no archive.
    ran = (tmp_path / "ran.sh").read_text()
    assert DOWNLOAD_LINE not in ran
    assert re.search(r'zstd -dc "/\S+/ollama-linux-(amd64|arm64)\.tar\.zst"', ran)


def test_other_installer_is_not_run(tmp_path):
    done, log = _run(SCRIPT, tmp_path)
    assert done.returncode == 1
    assert "installer does not match the SHA-256" in done.stderr
    assert "curl " in log
    assert "env " not in log


def test_an_archive_with_another_digest_is_not_installed(tmp_path):
    """The real archive digest of the script against a fake archive."""
    pinned = re.search(
        r'^ARCHIVE_SHA256_AMD64="([0-9a-f]{64})"$', SCRIPT.read_text(), re.M
    )
    assert pinned
    script = _with_digests(tmp_path, _sha(FAKE_INSTALLER), pinned.group(1))
    done, log = _run(script, tmp_path)

    assert done.returncode == 1
    assert "archive does not match the SHA-256" in done.stderr
    assert "env " not in log


def test_an_installer_without_the_download_line_is_not_run(tmp_path):
    """A digest that matches an installer whose download line moved must not
    end in an installer that still fetches the archive unchecked."""
    other = "#!/bin/sh\necho installer of another layout\n"
    script = _with_digests(tmp_path, _sha(other), _sha(FAKE_ARCHIVE))
    done, log = _run(script, tmp_path, installer=other)

    assert done.returncode == 1
    assert "download line" in done.stderr
    assert "env " not in log


def test_dry_run_downloads_and_runs_nothing(tmp_path):
    done, log = _run(SCRIPT, tmp_path, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert "[dry-run] check SHA-256" in done.stdout
    assert "install.sh" not in log
    assert "tar.zst" not in log
    assert "env " not in log
