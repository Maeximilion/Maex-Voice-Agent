"""`scripts/db_role.sh`, the restore with a role of its own, `scripts/migrate.sh` (#252).

The application's role is only worth something if it holds exactly what the
application needs: every test here sets it up on a real database and then
connects as that role. Bash and a Postgres client are needed as for the backup
tests; where they are missing the tests are skipped locally and fail in CI.
"""

import os
import subprocess
import uuid

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from api.tests.conftest import migrate
from api.tests.test_scripts_backup import (
    BASH,
    PASSPHRASE,
    REPO_ROOT,
    _admin_url,
    _databases_like,
    _missing_tool,
    _run,
    _sql,
)

INSUFFICIENT_PRIVILEGE = "42501"
# What a role that only reads and writes rows must be refused.
ABOVE_THE_ROLE = (
    "create table made_by_the_role (id int)",
    "drop table backup_probe",
    "alter table backup_probe add column more text",
    "truncate backup_probe",
    "create database made_by_the_role",
    "create role made_by_the_role",
    "copy backup_probe to program 'true'",
)
EVERY_TABLE_READ_AND_WRITTEN = (
    "select count(*) filter (where has_table_privilege(c.oid, 'select') "
    "and has_table_privilege(c.oid, 'insert') and has_table_privilege(c.oid, 'update') "
    "and has_table_privilege(c.oid, 'delete')), count(*) from pg_class c "
    "where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'"
)
EVERY_SEQUENCE_USED = (
    "select count(*) filter (where has_sequence_privilege(c.oid, 'usage')), count(*) "
    "from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'S'"
)


@pytest.fixture(scope="module", autouse=True)
def _tools():
    missing = _missing_tool()
    if missing is None:
        return
    if os.environ.get("CI"):
        pytest.fail(f"role tests need {missing}")
    pytest.skip(f"role tests need {missing}")


@pytest.fixture
def role():
    """A role name of this test alone; roles belong to the server, not to a database.

    Requested before the database fixtures, so it is dropped after them: a role
    with rights in a database that still exists cannot be dropped.
    """
    name = f"app_{uuid.uuid4().hex[:10]}"
    yield name
    _sql(_admin_url("postgres"), f'drop role if exists "{name}"')


@pytest.fixture
def db(role, migrated_db_url):
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


def _as(role: str, owner_url: str, password: str = "app-secret") -> str:
    """The application's URL: the owner's with the role's user and password."""
    url = make_url(owner_url).set(username=role, password=password)
    return url.render_as_string(hide_password=False)


def _script(script, *args, app_url, owner_url, tmp_path, **extra):
    return _run(
        script,
        *args,
        db_url=app_url,
        backup_dir=tmp_path / "backups",
        OWNER_DATABASE_URL=owner_url,
        **extra,
    )


def _refused(url: str, statement: str) -> bool:
    try:
        _sql(url, statement)
    except ProgrammingError as exc:
        return getattr(exc.orig, "sqlstate", None) == INSUFFICIENT_PRIVILEGE
    return False


def _role_exists(role: str) -> bool:
    count = _sql(
        _admin_url("postgres"),
        f"select count(*) from pg_roles where rolname = '{role}'",
    )
    return count[0][0] == 1


def test_the_role_reads_and_writes_rows_and_nothing_else(db, role, tmp_path):
    app = _as(role, db)

    done = _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    usable, tables = _sql(app, EVERY_TABLE_READ_AND_WRITTEN)[0]
    assert usable == tables and tables > 10
    assert f"reads and writes {tables} tables" in done.stdout
    used, sequences = _sql(app, EVERY_SEQUENCE_USED)[0]
    assert used == sequences
    _sql(app, "insert into backup_probe values (2, 'by the role')")
    _sql(app, "update backup_probe set note = 'changed' where id = 2")
    _sql(app, "delete from backup_probe where id = 2")
    # The menu search sets these two for its transaction (domain/menu/search.py).
    _sql(app, "select set_config('pg_trgm.similarity_threshold', '0.5', true)")
    assert _sql(app, "select similarity('pho bo', 'pho bo')")[0][0] == 1
    flags = "rolsuper or rolcreatedb or rolcreaterole or rolreplication or rolbypassrls"
    assert _sql(app, f"select {flags} from pg_roles where rolname = current_user") == [
        (False,)
    ]
    for statement in ABOVE_THE_ROLE:
        assert _refused(app, statement), statement
    # Neither the URL nor a password is repeated in what the script prints.
    said = done.stdout + done.stderr
    assert "app-secret" not in said and db not in said


def test_the_role_comes_first_on_a_new_server_and_gets_the_tables_made_later(
    role, scratch_db_url, tmp_path
):
    """Fresh server: the role is set up on the empty database, then Alembic runs
    as the owner. What the owner creates later is the role's to read and write."""
    app = _as(role, scratch_db_url)

    done = _script(
        "db_role.sh", app_url=app, owner_url=scratch_db_url, tmp_path=tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert "reads and writes 0 tables" in done.stdout

    migrate(scratch_db_url)

    usable, tables = _sql(app, EVERY_TABLE_READ_AND_WRITTEN)[0]
    assert usable == tables and tables > 10
    used, sequences = _sql(app, EVERY_SEQUENCE_USED)[0]
    assert used == sequences
    assert _refused(app, "create table made_by_the_role (id int)")
    assert _refused(app, "drop table alembic_version")


def test_a_second_run_sets_the_new_password(db, role, tmp_path):
    first = _as(role, db, "first-secret")
    # Everything a password may hold that SQL, psql or a URL treat specially.
    second = _as(role, db, 'it\'s\\n:"new"$1%;--@/?')
    assert (
        _script("db_role.sh", app_url=first, owner_url=db, tmp_path=tmp_path).returncode
        == 0
    )

    done = _script("db_role.sh", app_url=second, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    assert _sql(second, "select current_user") == [(role,)]
    with pytest.raises(OperationalError):
        _sql(first, "select 1")
    assert "first-secret" not in done.stdout + done.stderr


def test_a_role_that_was_given_more_is_cut_back(db, role, tmp_path):
    _sql(_admin_url("postgres"), f'create role "{role}" createdb createrole')

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    assert _refused(_as(role, db), "create database made_by_the_role")
    assert _refused(_as(role, db), "create role made_by_the_role")


def test_a_superuser_of_that_name_is_left_as_it_is(db, role, tmp_path):
    """A typo in DATABASE_URL must not take an administrator's rights away or
    change its password."""
    _sql(
        _admin_url("postgres"),
        f"create role \"{role}\" superuser login password 'the-admins-own'",
    )

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 2, done.stdout
    assert "is a superuser" in done.stderr
    kept = _as(role, db, "the-admins-own")
    assert _sql(kept, "select current_setting('is_superuser')") == [("on",)]


def test_a_role_that_owns_a_table_is_reported(db, role, tmp_path):
    """An owner alters and drops its table whatever is granted. It happens when
    a migration ran as the application's user."""
    app = _as(role, db)
    assert (
        _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path).returncode
        == 0
    )
    _sql(db, f'alter table backup_probe owner to "{role}"')

    done = _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 1, done.stdout
    assert f"{role} owns 1 of" in done.stderr and "reassign owned" in done.stderr


@pytest.mark.parametrize(
    ("application", "says"),
    [
        (lambda owner, role: owner, "the owner itself"),
        (lambda owner, role: owner.set(username=f'{role}"X'), "lower-case letters"),
        # set() keeps a password it is handed None for.
        (
            lambda owner, role: owner.set(username=role)._replace(password=None),
            "no password",
        ),
        (
            lambda owner, role: owner.set(username=role, database="another"),
            "different databases",
        ),
    ],
)
def test_a_configuration_without_a_usable_role_is_refused(
    db, role, tmp_path, application, says
):
    app = application(make_url(db), role).render_as_string(hide_password=False)

    done = _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 2, done.stdout + done.stderr
    assert says in done.stderr
    roles = _sql(
        _admin_url("postgres"),
        f"select count(*) from pg_roles where rolname like '{role}%'",
    )
    assert roles == [(0,)]


def test_an_owner_that_cannot_be_reached_changes_nothing(db, role, tmp_path):
    nowhere = make_url(db).set(port=1, password="owner-secret")
    owner = nowhere.render_as_string(hide_password=False)

    done = _script(
        "db_role.sh", app_url=_as(role, owner), owner_url=owner, tmp_path=tmp_path
    )

    assert done.returncode == 1
    assert "cannot connect" in done.stderr
    assert "owner-secret" not in done.stdout + done.stderr
    assert not _role_exists(role)


def _dump(db, app, tmp_path, passphrase_file):
    done = _script(
        "backup.sh",
        app_url=app,
        owner_url=db,
        tmp_path=tmp_path,
        passphrase_file=passphrase_file,
    )
    assert done.returncode == 0, done.stderr
    # Dumped by the owner, although DATABASE_URL names the role.
    assert f"backup of {make_url(db).username}@" in done.stdout
    (dump,) = (tmp_path / "backups").iterdir()
    return dump


def test_a_restored_database_is_the_roles_to_read_and_write_again(
    db, role, tmp_path, passphrase_file
):
    app = _as(role, db)
    assert (
        _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path).returncode
        == 0
    )
    dump = _dump(db, app, tmp_path, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")

    done = _script(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        make_url(db).database,
        app_url=app,
        owner_url=db,
        tmp_path=tmp_path,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr
    assert f"rights of the application's role {role} set" in done.stdout
    assert _sql(app, "select note from backup_probe where id = 1") == [("before",)]
    usable, tables = _sql(app, EVERY_TABLE_READ_AND_WRITTEN)[0]
    assert usable == tables and tables > 10
    # The next migration on the restored database: its tables are the role's too.
    _sql(db, "create table made_later (id int primary key)")
    _sql(app, "insert into made_later values (1)")
    for statement in ABOVE_THE_ROLE:
        assert _refused(app, statement), statement


def test_a_restore_rehearsal_sets_the_rights_and_leaves_the_database_alone(
    db, role, tmp_path, passphrase_file
):
    app = _as(role, db)
    assert (
        _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path).returncode
        == 0
    )
    dump = _dump(db, app, tmp_path, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")

    done = _script(
        "restore.sh",
        dump.as_posix(),
        "--check",
        app_url=app,
        owner_url=db,
        tmp_path=tmp_path,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 0, done.stderr
    assert f"rights of the application's role {role} set" in done.stdout
    assert _sql(app, "select note from backup_probe where id = 1") == [("after",)]
    assert _databases_like(f"{make_url(db).database}_") == []


def test_a_restore_for_a_role_the_server_does_not_have_changes_nothing(
    db, role, tmp_path, passphrase_file
):
    """A dump restored on a new server before db_role.sh ran: the application
    could not read the result, so the swap does not happen."""
    app = _as(role, db)
    dump = _dump(db, app, tmp_path, passphrase_file)
    _sql(db, "update backup_probe set note = 'after'")
    name = make_url(db).database

    done = _script(
        "restore.sh",
        dump.as_posix(),
        "--replace",
        name,
        app_url=app,
        owner_url=db,
        tmp_path=tmp_path,
        passphrase_file=passphrase_file,
    )

    assert done.returncode == 1, done.stdout + done.stderr
    assert "scripts/db_role.sh" in done.stderr and "nothing changed" in done.stderr
    assert _sql(db, "select note from backup_probe where id = 1") == [("after",)]
    assert _databases_like(f"{name}_") == []


def _migrate(tmp_path, *args, **env):
    """scripts/migrate.sh with a `docker` that only writes down how it was called."""
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    seen = tmp_path / "seen"
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" "${DATABASE_URL-unset}" >"$SEEN"\n',
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DATABASE_URL", "OWNER_DATABASE_URL"}
    }
    environment.update(
        PATH=f"{stub.parent.as_posix()}{os.pathsep}{os.environ['PATH']}",
        SEEN=seen.as_posix(),
        # The worktree's own .env must not leak into a test run.
        BACKUP_ENV_FILE=(tmp_path / "absent.env").as_posix(),
    )
    environment.update(env)
    done = subprocess.run(
        [BASH, "scripts/migrate.sh", *args],
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return seen.read_text(encoding="utf-8").splitlines()


def test_migrate_hands_the_owners_url_to_alembic_and_never_as_an_argument(tmp_path):
    owner = "postgresql+psycopg://owner:owner-secret@db:5432/maex_agent"

    call, url = _migrate(tmp_path, OWNER_DATABASE_URL=owner)

    assert call == (
        "compose exec -T -e DATABASE_URL api alembic -c db/alembic.ini upgrade head"
    )
    assert url == owner


def test_migrate_reads_the_owner_from_the_env_file(tmp_path):
    owner = "postgresql+psycopg://owner:owner-secret@db:5432/maex_agent"
    env_file = tmp_path / "server.env"
    env_file.write_text(
        f"DATABASE_URL=postgresql+psycopg://app:app-secret@db:5432/maex_agent\n"
        f"OWNER_DATABASE_URL={owner}\n",
        encoding="utf-8",
    )

    call, url = _migrate(tmp_path, "current", BACKUP_ENV_FILE=env_file.as_posix())

    assert (
        call == "compose exec -T -e DATABASE_URL api alembic -c db/alembic.ini current"
    )
    assert url == owner


def test_migrate_without_an_owner_leaves_the_containers_url_alone(tmp_path):
    call, url = _migrate(tmp_path, "downgrade", "-1")

    assert call == "compose exec -T api alembic -c db/alembic.ini downgrade -1"
    assert url == "unset"
