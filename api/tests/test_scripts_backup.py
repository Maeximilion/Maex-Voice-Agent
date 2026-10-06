"""`scripts/backup.sh` and `scripts/restore.sh` against real scratch databases (T-9.3).

A backup that was never restored is a hope (docs/13 §4), so every test here dumps a
real database and reads it back. The scripts are bash and need gpg plus a Postgres
client (on PATH, or Docker for the client image); where those are missing the tests
are skipped locally and fail in CI.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from api.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PASSPHRASE = "correct horse battery staple"


def _bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # System32\bash.exe is the WSL launcher; the scripts need Git Bash with gpg.
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        for parts in (
            ("Git", "bin", "bash.exe"),
            ("Programs", "Git", "bin", "bash.exe"),
        ):
            candidate = Path(base or "", *parts)
            if base and candidate.exists():
                return str(candidate)
    return None


BASH = _bash()


def _missing_tool() -> str | None:
    if BASH is None:
        return "bash"
    probe = (
        "command -v gpg >/dev/null || { echo gpg; exit 0; }; "
        "command -v pg_dump >/dev/null || docker info >/dev/null 2>&1 "
        "|| echo 'a Postgres client or a running Docker daemon'"
    )
    found = subprocess.run(
        [BASH, "-c", probe], capture_output=True, text=True, timeout=60
    )
    return found.stdout.strip() or None


@pytest.fixture(scope="module", autouse=True)
def _tools():
    missing = _missing_tool()
    if missing is None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"backup tests need {missing}")
    pytest.skip(f"backup tests need {missing}")


def _admin_url(database: str) -> str:
    url = make_url(settings.database_url).set(database=database)
    return url.render_as_string(hide_password=False)


def _sql(url: str, statement: str) -> list:
    engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(statement))
            return list(result) if result.returns_rows else []
    finally:
        engine.dispose()


def _databases_like(prefix: str) -> list[str]:
    rows = _sql(
        _admin_url("postgres"),
        f"select datname from pg_database where datname like '{prefix}%' order by 1",
    )
    return [row[0] for row in rows]


@pytest.fixture
def db(migrated_db_url):
    """A migrated database with one row to recognise, plus cleanup of what restore keeps."""
    _sql(migrated_db_url, "create table backup_probe (id int primary key, note text)")
    _sql(migrated_db_url, "insert into backup_probe values (1, 'before')")
    name = make_url(migrated_db_url).database
    yield migrated_db_url
    for leftover in _databases_like(f"{name}_"):
        _sql(_admin_url("postgres"), f'drop database "{leftover}" with (force)')


@pytest.fixture
def passphrase_file(tmp_path):
    path = tmp_path / "passphrase"
    path.write_text(PASSPHRASE + "\n", encoding="utf-8")
    return path


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


def _run(script, *args, db_url, backup_dir, passphrase_file=None, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
    env.update(
        DATABASE_URL=db_url,
        BACKUP_DIR=backup_dir.as_posix(),
        # The worktree's own .env must not leak into a test run.
        BACKUP_ENV_FILE=(backup_dir.parent / "absent.env").as_posix(),
        PGCONNECT_TIMEOUT="5",
    )
    if passphrase_file is not None:
        env["BACKUP_PASSPHRASE_FILE"] = passphrase_file.as_posix()
    env.update(extra)
    return subprocess.run(
        [BASH, f"scripts/{script}", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _backup(db, backup_dir, passphrase_file) -> Path:
    done = _run(
        "backup.sh", db_url=db, backup_dir=backup_dir, passphrase_file=passphrase_file
    )
    assert done.returncode == 0, done.stderr
    (dump,) = backup_dir.iterdir()
    return dump


def _note(url: str) -> str:
    return _sql(url, "select note from backup_probe where id = 1")[0][0]


def test_backup_writes_one_encrypted_dump(db, backup_dir, passphrase_file):
    done = _run(
        "backup.sh", db_url=db, backup_dir=backup_dir, passphrase_file=passphrase_file
    )

    assert done.returncode == 0, done.stderr
    name = make_url(db).database
    (dump,) = backup_dir.iterdir()
    assert dump.name.startswith(f"maex_{name}_") and dump.name.endswith(".dump.gpg")
    # A plain custom-format dump starts with PGDMP; the encrypted one must not.
    assert not dump.read_bytes().startswith(b"PGDMP")
    if os.name != "nt":
        # NTFS has no owner-only mode; there the folder's ACL decides.
        assert dump.stat().st_mode & 0o077 == 0
    assert name in done.stdout
    # The target is named without the URL and without the password part of it
    # (the bare password proves nothing where it equals the user name, as in CI).
    said = done.stdout + done.stderr
    assert db not in said and f":{make_url(db).password}@" not in said


def test_restore_check_reads_the_dump_and_leaves_the_database_alone(
    db, backup_dir, passphrase_file
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    revision = _sql(db, "select version_num from alembic_version")[0][0]

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--check",
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr
    assert f"revision {revision}" in done.stdout
    assert _note(db) == "after"
    name = make_url(db).database
    assert _databases_like(f"{name}_") == []


def test_restore_replace_brings_the_state_back_and_keeps_the_old_database(
    db, backup_dir, passphrase_file
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    name = make_url(db).database

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr
    assert _note(db) == "before"
    # search_menu needs the trigram extension; a restore without it is not a restore.
    assert _sql(db, "select 1 from pg_extension where extname = 'pg_trgm'")
    (previous,) = _databases_like(f"{name}_")
    assert previous.startswith(f"{name}_before_restore_")
    assert previous in done.stdout
    assert _note(_admin_url(previous)) == "after"

    # The kept copy holds customer data and nothing expires it: every later run names it.
    later = _run(
        "restore.sh",
        dump.as_posix(),
        "--check",
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert later.returncode == 0, later.stderr
    assert "kept from earlier restores" in later.stdout and previous in later.stdout


# Stands in for psql, pg_dump and pg_restore: runs the real client, but the answer to
# a rename is lost after the server carried it out, as when the connection drops.
LOSSY_CLIENT = """#!/usr/bin/env bash
tool="$(basename "$0")"
here="$(cd "$(dirname "$0")" && pwd)"
PATH=":$PATH:"
PATH="${PATH//:$here:/:}"
PATH="${PATH#:}"
export PATH="${PATH%:}"
source "$LIB"
BACKUP_PG_CLIENT=auto
pg_resolve_client
pg "$tool" "$@"
status=$?
if [ "$tool" = psql ] && [[ "$*" == *"rename to"* ]]; then exit 2; fi
exit $status
"""


def test_restore_replace_asks_the_server_when_the_answer_to_the_swap_is_lost(
    db, backup_dir, passphrase_file, tmp_path
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    name = make_url(db).database
    clients = tmp_path / "clients"
    clients.mkdir()
    for tool in ("psql", "pg_dump", "pg_restore"):
        (clients / tool).write_text(LOSSY_CLIENT, encoding="utf-8", newline="\n")
        (clients / tool).chmod(0o755)

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
        PATH=f"{clients}{os.pathsep}{os.environ['PATH']}",
        LIB=(REPO_ROOT / "scripts" / "lib_pg.sh").as_posix(),
        BACKUP_PG_CLIENT="local",
    )

    # "Nothing changed" would be a false assurance: the restored state is live.
    assert done.returncode == 0, done.stderr
    assert "the server has renamed the databases" in done.stderr
    assert "nothing changed" not in done.stderr
    assert _note(db) == "before"
    (previous,) = _databases_like(f"{name}_")
    assert _note(_admin_url(previous)) == "after"


def test_restore_replace_recreates_a_database_that_is_gone(
    db, backup_dir, passphrase_file
):
    dump = _backup(db, backup_dir, passphrase_file)
    name = make_url(db).database
    _sql(_admin_url("postgres"), f'drop database "{name}" with (force)')

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr
    assert _note(db) == "before"
    assert _databases_like(f"{name}_") == []


def test_backup_refuses_to_write_plain_text_unless_told(db, backup_dir):
    refused = _run("backup.sh", db_url=db, backup_dir=backup_dir)

    assert refused.returncode == 2
    assert "BACKUP_PASSPHRASE_FILE" in refused.stderr
    assert not backup_dir.exists() or list(backup_dir.iterdir()) == []

    plain = _run("backup.sh", "--no-encrypt", db_url=db, backup_dir=backup_dir)

    assert plain.returncode == 0, plain.stderr
    (dump,) = backup_dir.iterdir()
    assert dump.name.endswith(".dump")
    assert dump.read_bytes().startswith(b"PGDMP")


def test_restore_with_the_wrong_passphrase_changes_nothing(
    db, backup_dir, passphrase_file, tmp_path
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    wrong = tmp_path / "wrong"
    wrong.write_text("not the passphrase\n", encoding="utf-8")
    name = make_url(db).database

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=wrong,
    )

    assert done.returncode == 1
    assert "nothing changed" in done.stderr
    assert _note(db) == "after"
    assert _databases_like(f"{name}_") == []


def test_restore_replace_wants_the_name_of_the_target_database(
    db, backup_dir, passphrase_file
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    common = {
        "db_url": db,
        "backup_dir": backup_dir,
        "passphrase_file": passphrase_file,
    }

    other = _run("restore.sh", dump.as_posix(), "--replace", "maex_agent", **common)
    no_mode = _run("restore.sh", dump.as_posix(), **common)

    assert other.returncode == 2
    assert make_url(db).database in other.stderr
    assert no_mode.returncode == 2
    assert _note(db) == "after"


def test_restore_replace_refuses_while_a_session_is_connected(
    db, backup_dir, passphrase_file
):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    name = make_url(db).database
    engine = create_engine(db, poolclass=NullPool)

    with engine.connect():
        done = _run(
            "restore.sh",
            dump.as_posix(),
            "--replace",
            name,
            db_url=db,
            backup_dir=backup_dir,
            passphrase_file=passphrase_file,
        )
    engine.dispose()

    assert done.returncode == 1
    assert "session" in done.stderr and "nothing changed" in done.stderr
    assert _note(db) == "after"
    assert _databases_like(f"{name}_") == []


def test_backup_of_an_unreachable_database_keeps_no_file(
    db, backup_dir, passphrase_file
):
    backup_dir.mkdir()
    name = make_url(db).database
    old = backup_dir / f"maex_{name}_20200101T000000Z.dump.gpg"
    old.write_bytes(b"old")
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))
    nowhere = make_url(db).set(port=1).render_as_string(hide_password=False)

    done = _run(
        "backup.sh",
        db_url=nowhere,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 1
    assert "cannot connect" in done.stderr
    # A failed run must not thin out the backups that exist.
    assert [path.name for path in backup_dir.iterdir()] == [old.name]


def test_backup_of_a_database_without_tables_fails(
    scratch_db_url, backup_dir, passphrase_file
):
    done = _run(
        "backup.sh",
        db_url=scratch_db_url,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 1
    assert "no table data" in done.stderr
    assert list(backup_dir.iterdir()) == []


def test_retention_removes_only_old_scheduled_backups_of_this_database(
    db, backup_dir, passphrase_file
):
    backup_dir.mkdir()
    name = make_url(db).database
    old_scheduled = f"maex_{name}_20200101T000000Z.dump.gpg"
    kept = [
        f"maex_{name}_20200101T000000Z_before_import.dump.gpg",
        "maex_otherdb_20200101T000000Z.dump.gpg",
        "notes.txt",
    ]
    long_ago = time.time() - 30 * 86400
    for filename in [old_scheduled, *kept]:
        (backup_dir / filename).write_bytes(b"old")
        os.utime(backup_dir / filename, (long_ago, long_ago))

    done = _run(
        "backup.sh",
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
        BACKUP_KEEP_DAYS="14",
    )

    assert done.returncode == 0, done.stderr
    names = {path.name for path in backup_dir.iterdir()}
    assert old_scheduled not in names
    assert set(kept) <= names
    assert len(names) == len(kept) + 1
    assert old_scheduled in done.stdout


def test_two_backups_in_the_same_second_never_share_a_file(
    db, backup_dir, passphrase_file
):
    def start():
        env = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
        env.update(
            DATABASE_URL=db,
            BACKUP_DIR=backup_dir.as_posix(),
            BACKUP_ENV_FILE=(backup_dir.parent / "absent.env").as_posix(),
            BACKUP_PASSPHRASE_FILE=passphrase_file.as_posix(),
        )
        return subprocess.Popen(
            [BASH, "scripts/backup.sh"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    # Both names carry the second they start in: start right after one begins.
    time.sleep(1.05 - time.time() % 1)
    runs = [start(), start()]
    results = [(run.communicate(timeout=300), run.returncode) for run in runs]

    written = sorted(backup_dir.iterdir())
    good = [code for _, code in results if code == 0]
    assert good, [err for (_, err), _ in results]
    # One file per run that reported success, and nothing half-written left over.
    assert len(written) == len(good), [path.name for path in written]
    for (_, err), code in results:
        assert code in (0, 1)
        assert code == 0 or "exists already" in err
    for dump in written:
        check = _run(
            "restore.sh",
            dump.as_posix(),
            "--check",
            db_url=db,
            backup_dir=backup_dir,
            passphrase_file=passphrase_file,
        )
        assert check.returncode == 0, check.stderr


def test_label_is_part_of_the_file_name(db, backup_dir, passphrase_file):
    common = {
        "db_url": db,
        "backup_dir": backup_dir,
        "passphrase_file": passphrase_file,
    }

    done = _run("backup.sh", "--label", "before_menu_import", **common)
    bad = _run("backup.sh", "--label", "../etc", **common)

    assert done.returncode == 0, done.stderr
    (dump,) = backup_dir.iterdir()
    assert dump.name.endswith("Z_before_menu_import.dump.gpg")
    assert bad.returncode == 2


TARGET_PROBE = (
    'pg_target_from_url "$1"; '
    'printf \'%s\\n\' "$PGHOST" "$PGPORT" "${PGUSER-}" "${PGPASSWORD-}" "$PGDATABASE"'
)


def _lib(snippet: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "BACKUP_"))}
    return subprocess.run(
        [BASH, "-c", f"source scripts/lib_pg.sh; {snippet}", "probe", *args],
        cwd=REPO_ROOT,
        env={**clean, "BACKUP_ENV_FILE": "/nonexistent", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _target(url: str, **env: str) -> subprocess.CompletedProcess:
    return _lib(TARGET_PROBE, url, **env)


def test_url_without_a_password_keeps_the_exported_one():
    done = _target("postgresql://maex@db/maex_agent", PGPASSWORD="from the caller")

    assert done.stdout.split("\n")[3] == "from the caller"


def test_connection_parameters_and_an_ipv6_host_are_taken_from_the_url():
    done = _lib(
        'pg_target_from_url "$1"; '
        'echo "$PGHOST $PGPORT $PGSSLMODE $PGCONNECT_TIMEOUT $PGSSLROOTCERT"',
        "postgresql+psycopg://maex:pw@[::1]:6543/maex_agent"
        "?connect_timeout=3&sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fdb%20ca.pem",
    )

    assert done.stdout.strip() == "::1 6543 verify-full 3 /etc/ssl/db ca.pem"


def test_a_parameter_that_cannot_be_passed_on_stops_the_script():
    # Dropping it silently would let the backup connect differently from the application.
    done = _target("postgresql://maex@db/maex_agent?sslpassword=hunter2")

    assert done.returncode == 2
    assert "sslpassword" in done.stderr and "hunter2" not in done.stderr


def test_certificate_files_are_refused_for_the_client_container():
    done = _lib(
        'pg_target_from_url "$1"; pg_resolve_client',
        "postgresql://maex@db/maex_agent?sslmode=verify-full&sslrootcert=/etc/ssl/ca.pem",
        BACKUP_PG_CLIENT="docker",
    )

    assert done.returncode == 2
    assert "sslrootcert" in done.stderr


def test_the_compose_network_always_takes_the_client_from_the_image():
    # A client on the host cannot resolve `db`, whatever is installed there.
    done = _lib(
        'PGHOST=db; pg_resolve_client; echo "$PG_CLIENT ${DOCKER_ARGS[*]}"',
        BACKUP_DOCKER_NETWORK="maex_default",
    )

    assert done.stdout.strip() == "docker --network maex_default"


def test_major_version_is_read_from_a_version_line():
    done = _lib(
        "echo 'pg_dump (PostgreSQL) 16.4 (Ubuntu 16.4-1.pgdg24.04+1)' | pg_major; "
        "echo 'pg_dump (PostgreSQL) 9.6.24' | pg_major"
    )

    assert done.stdout.split() == ["16", "9"]


def test_target_is_read_from_the_sqlalchemy_url():
    encoded = _target(
        "postgresql+psycopg://ma%40x:p%40ss%3Aw%2Frd%25@db.internal:6543/maex_agent"
        "?sslmode=require"
    )
    plain = _target("postgresql://maex@db/maex_agent")

    assert encoded.stdout.split("\n")[:5] == [
        "db.internal",
        "6543",
        "ma@x",
        "p@ss:w/rd%",
        "maex_agent",
    ]
    assert plain.stdout.split("\n")[:5] == ["db", "5432", "maex", "", "maex_agent"]


def test_a_url_that_is_not_postgres_is_rejected_without_echoing_it():
    done = _target("mysql://maex:secret@db/maex_agent")

    assert done.returncode == 2
    assert "secret" not in done.stdout + done.stderr


def test_cron_file_runs_the_backup_daily_at_three():
    lines = [
        line
        for line in (REPO_ROOT / "deploy" / "backup.cron").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    (job,) = lines
    assert job.startswith("0 3 * * * ")
    assert "scripts/backup.sh" in job
    # The shell opens the log before the script runs; without the folder no backup.
    assert job.index("mkdir -p backups") < job.index(">> backups/")


def test_passphrase_file_with_windows_line_ending_gives_the_same_key(
    db, backup_dir, passphrase_file, tmp_path
):
    crlf = tmp_path / "crlf"
    crlf.write_bytes(PASSPHRASE.encode() + b"\r\n")
    dump = _backup(db, backup_dir, crlf)

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--check",
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr


def test_a_dump_with_bytes_after_its_end_is_still_read_to_the_end(db, backup_dir):
    """pg_restore stops at the end marker; the writer of the pipe must not die of it.

    Before the fix `cat` (or gpg) was killed by SIGPIPE whenever pg_restore finished
    first, and `pipefail` turned that into "cannot read the dump", on about one run in
    ten, for a perfectly good dump. Bytes after the end make the race certain.
    """
    plain = _run("backup.sh", "--no-encrypt", db_url=db, backup_dir=backup_dir)
    assert plain.returncode == 0, plain.stderr
    (dump,) = backup_dir.iterdir()
    with dump.open("ab") as handle:
        handle.write(b"\0" * (1 << 20))

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--check",
        db_url=db,
        backup_dir=backup_dir,
    )

    assert done.returncode == 0, done.stderr


def test_the_dump_check_does_not_end_the_script_when_bash_runs_the_last_pipe_in_place(
    db, backup_dir
):
    """With `lastpipe` the last element of a pipeline runs in the script's own shell."""
    plain = _run("backup.sh", "--no-encrypt", db_url=db, backup_dir=backup_dir)
    assert plain.returncode == 0, plain.stderr
    (dump,) = backup_dir.iterdir()

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--check",
        db_url=db,
        backup_dir=backup_dir,
        BASHOPTS="lastpipe",
    )

    assert done.returncode == 0, done.stderr
    assert "restore check passed" in done.stdout


def test_restore_of_a_cut_off_dump_changes_nothing(db, backup_dir, passphrase_file):
    dump = _backup(db, backup_dir, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    whole = dump.read_bytes()
    dump.write_bytes(whole[: len(whole) - 200])
    name = make_url(db).database

    done = _run(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        db_url=db,
        backup_dir=backup_dir,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 1
    assert "nothing changed" in done.stderr
    assert _note(db) == "after"
    assert _databases_like(f"{name}_") == []
