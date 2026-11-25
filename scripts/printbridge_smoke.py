#!/usr/bin/env python3
"""Run the built print bridge without a real printer or our server (T-9.6).

    python scripts/printbridge_smoke.py dist/maex-printbridge.exe --tls-url https://api.github.com

A build can succeed and still miss what the program imports at run time. These checks
reach the parts a frozen file can lose: the Windows printer path, the slip's code page
and, with --tls-url, https with certificate checks against any public address.
"""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

CUT = b"\x1dV\x42\x00"
NO_SERVER = "https://127.0.0.1:9"
NO_PRINTER = "tcp:127.0.0.1:9"
SETTINGS = {
    "MAEX_KITCHEN_TOKEN": "smoke-token",
    "MAEX_TENANT_ID": "00000000-0000-0000-0000-000000000000",
}


def _run(program: Sequence[str], *args: str, **settings: str) -> tuple[int, str]:
    done = subprocess.run(
        [*program, *args],
        env={**os.environ, **SETTINGS, **settings},
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def _test_slip(program: Sequence[str]) -> tuple[int, str, bytes]:
    """`--test` against a listener. It answers no status query: each waits out its 2 s."""
    received = bytearray()
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(30)

        def receive() -> None:
            conn, _ = server.accept()
            with conn:
                while chunk := conn.recv(4096):
                    received.extend(chunk)

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        printer = f"tcp:127.0.0.1:{server.getsockname()[1]}"
        code, output = _run(program, "--test", MAEX_PRINTER=printer)
        thread.join(5)
    return code, output, bytes(received)


def checks(program: Sequence[str], tls_url: str | None = None) -> list[str]:
    """What went wrong, one entry per failed check. Empty when the program is sound."""
    failures: list[str] = []

    def expect(name: str, ok: bool, output: str) -> None:
        if not ok:
            failures.append(f"{name}:\n{output}")

    code, output = _run(program, "--help")
    expect("--help", code == 0 and "--once" in output, output)

    with tempfile.TemporaryDirectory() as folder:
        once = {
            "MAEX_PRINTER": NO_PRINTER,
            "MAEX_STATE_FILE": str(Path(folder) / "state.json"),
        }
        code, output = _run(program, "--once", MAEX_SERVER_URL=NO_SERVER, **once)
        expect(
            "--once without a server",
            code == 1 and "Durchlauf abgebrochen" in output,
            output,
        )
        if tls_url:
            # An HTTP status means the handshake and the certificate check passed.
            code, output = _run(program, "--once", MAEX_SERVER_URL=tls_url, **once)
            expect("https", code == 1 and "HTTP Error" in output, output)

    # win32print is imported by name at run time, so a build can lose it unnoticed. An
    # unknown queue reaches the module; without it the answer is "pywin32 fehlt".
    code, output = _run(program, "--test", MAEX_PRINTER="windows:Maex Smoke No Queue")
    expect(
        "Windows queue path",
        code != 0 and "nicht gefunden" in output and "pywin32 fehlt" not in output,
        output,
    )

    code, output, slip = _test_slip(program)
    expect(
        "--test slip",
        code == 0 and "Umlaute äöü ß".encode("cp858") in slip and slip.endswith(CUT),
        f"{output}\nreceived {len(slip)} bytes",
    )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", help="path of the built program")
    parser.add_argument("--tls-url", help="public https address, any status will do")
    args = parser.parse_args(argv)
    failures = checks([args.program], args.tls_url)
    for failure in failures:
        print(f"FAILED {failure}", file=sys.stderr)
    print(f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
