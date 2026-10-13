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
    "create schema made_by_the_role",
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
    # The schema counts nothing with a sequence today; the role's rights on one
    # are checked all the same.
    _sql(migrated_db_url, "create sequence backup_probe_numbers")
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
    assert used == sequences and sequences >= 1
    assert _sql(app, "select nextval('backup_probe_numbers')") == [(1,)]
    assert _refused(app, "select setval('backup_probe_numbers', 500)")
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
    assert f"{role} owns " in done.stderr and "reassign owned" in done.stderr


def test_a_role_that_is_a_member_of_another_is_reported(db, role, tmp_path):
    """A member holds what the other role holds: reading files of the server
    here, everything if the other role is the owner."""
    _sql(_admin_url("postgres"), f'create role "{role}"')
    _sql(_admin_url("postgres"), f'grant pg_read_server_files to "{role}"')

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 1, done.stdout
    assert f"{role} is a member of 1 other role" in done.stderr


def test_a_url_that_switches_to_another_role_is_refused(db, role, tmp_path):
    """`options=-c role=...`: the proof would be about the role switched to,
    while the one that logged in keeps its memberships and can switch back
    (Codex, PR #281)."""
    other = f"{role}_other"
    _sql(db, f'create role "{role}"')
    _sql(db, f'create role "{other}"')
    _sql(db, f'grant "{other}" to "{role}"')
    try:
        done = _script(
            "db_role.sh",
            # By hand: a rendered URL writes the blank as "+", which libpq keeps.
            app_url=f"{_as(role, db)}?options=-c%20role%3D{other}",
            owner_url=db,
            tmp_path=tmp_path,
        )
    finally:
        _sql(db, f'drop role "{other}"')

    assert done.returncode == 1, done.stdout
    assert "switches to another role" in done.stderr


def test_a_role_that_owns_a_function_and_nothing_else_is_reported(db, role, tmp_path):
    """Not only tables: the owner of a trigger function can rewrite it and break
    every write that fires it (Codex, PR #281)."""
    _sql(db, f'create role "{role}"')
    _sql(
        db,
        "create function probe_stamp() returns trigger language plpgsql "
        "as $$ begin return new; end $$",
    )
    _sql(db, f'alter function probe_stamp() owner to "{role}"')

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 1, done.stdout
    assert f"{role} owns 1 object(s)" in done.stderr


def test_settings_a_reused_role_carries_are_reset(db, role, tmp_path):
    """`alter role ... set default_transaction_read_only = on` would pass every
    check that only reads and fail every write of the application (Codex, PR #281)."""
    name = make_url(db).database
    _sql(db, f'create role "{role}"')
    _sql(db, f'alter role "{role}" set default_transaction_read_only = on')
    _sql(
        db,
        f'alter role "{role}" in database "{name}" set statement_timeout = 1',
    )
    app = _as(role, db)

    done = _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    _sql(app, "insert into backup_probe values (3, 'written by the role')")
    assert _sql(app, "show statement_timeout") == [("0",)]


def test_a_connection_limit_and_an_expiry_of_a_reused_role_are_lifted(
    db, role, tmp_path
):
    """One connection is enough for the check and not for api and dispatcher
    together (Codex, PR #281)."""
    _sql(db, f"create role \"{role}\" connection limit 1 valid until '2020-01-01'")

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    limits = _sql(
        db,
        f"select rolconnlimit, rolvaliduntil::text from pg_roles where rolname = '{role}'",
    )
    assert limits == [(-1, "infinity")]


def test_a_role_that_reaches_into_another_schema_is_reported(db, role, tmp_path):
    """Rights in a schema besides the application's are not the application's:
    the role would keep reading there (Codex, PR #281)."""
    _sql(db, f'create role "{role}"')
    _sql(db, "create schema private")
    _sql(db, "create table private.customer_secrets (id int primary key)")
    _sql(db, f'grant usage on schema private to "{role}"')
    _sql(db, f'grant select on private.customer_secrets to "{role}"')

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 1, done.stdout
    assert "or use one besides public: 1 schema(s)" in done.stderr


def test_rights_the_role_was_granted_before_are_taken_away(db, role, tmp_path):
    """A role that existed with more ends like a new one (Codex, PR #281):
    granting the wanted rights on top would leave it able to empty a table or
    to make a schema of its own."""
    name = make_url(db).database
    _sql(db, f'create role "{role}"')
    _sql(db, f'grant truncate, references, trigger on backup_probe to "{role}"')
    _sql(db, f'grant create on database "{name}" to "{role}"')
    _sql(db, f'grant create on schema public to "{role}"')
    _sql(
        db,
        f'alter default privileges in schema public grant truncate on tables to "{role}"',
    )
    app = _as(role, db)

    done = _script("db_role.sh", app_url=app, owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 0, done.stderr
    _sql(db, "create table made_later (id int primary key)")
    _sql(app, "insert into made_later values (1)")
    for statement in (*ABOVE_THE_ROLE, "truncate made_later"):
        assert _refused(app, statement), statement


@pytest.mark.parametrize(
    ("grant", "says"),
    [
        (
            "grant truncate on backup_probe to public",
            "above reading and writing rows",
        ),
        ('grant create on database "{name}" to public', "may create a schema"),
        # setval: the role could put a counter anywhere (Codex, PR #281).
        (
            "grant update on sequence backup_probe_numbers to public",
            "above reading and writing rows",
        ),
    ],
)
def test_a_right_that_reaches_the_role_through_everyone_is_reported(
    db, role, tmp_path, grant, says
):
    """The script takes away what the role itself was granted. What PUBLIC holds
    it leaves alone, so the check at the end has to see it."""
    _sql(db, grant.format(name=make_url(db).database))

    done = _script("db_role.sh", app_url=_as(role, db), owner_url=db, tmp_path=tmp_path)

    assert done.returncode == 1, done.stdout
    assert says in done.stderr


def test_the_login_is_checked_the_way_the_application_connects(db, role, tmp_path):
    """The test server speaks no TLS. A DATABASE_URL that demands it logs in the
    owner's way and not its own, so the API could not connect (Codex, PR #281)."""
    app = make_url(_as(role, db)).set(query={"sslmode": "require"})

    done = _script(
        "db_role.sh",
        app_url=app.render_as_string(hide_password=False),
        owner_url=db,
        tmp_path=tmp_path,
    )

    assert done.returncode == 1, done.stdout
    assert "the way DATABASE_URL says" in done.stderr


def test_the_backup_override_does_not_decide_where_the_role_is_made(db, role, tmp_path):
    """BACKUP_DATABASE_URL may name the database under another address for
    backup and restore; the role belongs to the application's address."""
    elsewhere = make_url(db).set(host="another-name-for-the-server")

    done = _script(
        "db_role.sh",
        app_url=_as(role, db),
        owner_url=db,
        tmp_path=tmp_path,
        BACKUP_DATABASE_URL=elsewhere.render_as_string(hide_password=False),
    )

    assert done.returncode == 0, done.stderr
    assert _sql(_as(role, db), "select current_user") == [(role,)]


@pytest.mark.parametrize("script", ["backup.sh", "restore.sh"])
def test_an_owner_url_for_another_database_is_refused(db, role, tmp_path, script):
    """A typo in OWNER_DATABASE_URL must not dump some other database night after
    night, or let --replace swap one whose name matches (Codex, PR #281)."""
    dump = tmp_path / "some.dump"
    dump.write_bytes(b"")
    arguments = {
        "backup.sh": ["--no-encrypt"],
        "restore.sh": [dump.as_posix(), "--replace", "postgres"],
    }
    other = make_url(db).set(database="postgres")

    done = _script(
        script,
        *arguments[script],
        app_url=_as(role, db),
        owner_url=other.render_as_string(hide_password=False),
        tmp_path=tmp_path,
    )

    assert done.returncode == 2, done.stdout + done.stderr
    assert "differ in DATABASE" in done.stderr
    assert not (tmp_path / "backups").exists()


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
            "differ in DATABASE",
        ),
        # The same database name on another server (Codex, PR #281): the role
        # would be made and checked here while the API connects there.
        (
            lambda owner, role: owner.set(username=role, host="elsewhere"),
            "differ in HOST",
        ),
        (lambda owner, role: owner.set(username=role, port=6543), "differ in PORT"),
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
    # None: the script ended before it called Compose.
    called = seen.read_text(encoding="utf-8").splitlines() if seen.exists() else None
    return done, called


APP_URL = "postgresql+psycopg://app:app-secret@db:5432/maex_agent"


def test_migrate_hands_the_owners_url_to_alembic_and_never_as_an_argument(tmp_path):
    owner = "postgresql+psycopg://owner:owner-secret@db:5432/maex_agent"

    done, called = _migrate(tmp_path, DATABASE_URL=APP_URL, OWNER_DATABASE_URL=owner)

    assert done.returncode == 0, done.stderr
    call, url = called

    assert call == (
        "compose run --rm -T -e DATABASE_URL api alembic -c db/alembic.ini upgrade head"
    )
    assert url == owner


def test_migrate_reads_the_owner_from_the_env_file(tmp_path):
    owner = "postgresql+psycopg://owner:owner-secret@db:5432/maex_agent"
    env_file = tmp_path / "server.env"
    env_file.write_text(
        f"DATABASE_URL={APP_URL}\nOWNER_DATABASE_URL={owner}\n", encoding="utf-8"
    )

    done, called = _migrate(tmp_path, "current", BACKUP_ENV_FILE=env_file.as_posix())

    assert done.returncode == 0, done.stderr
    call, url = called
    assert (
        call
        == "compose run --rm -T -e DATABASE_URL api alembic -c db/alembic.ini current"
    )
    assert url == owner


@pytest.mark.parametrize(
    ("owner", "says"),
    [
        ("postgresql+psycopg://owner:owner-secret@db:5432/other", "differ in DATABASE"),
        (
            "postgresql+psycopg://owner:owner-secret@db2:5432/maex_agent",
            "differ in HOST",
        ),
    ],
)
def test_migrate_refuses_an_owner_url_for_another_database(tmp_path, owner, says):
    """The same typo that must not be dumped must not be migrated either."""
    done, called = _migrate(tmp_path, DATABASE_URL=APP_URL, OWNER_DATABASE_URL=owner)

    assert done.returncode == 2 and says in done.stderr
    assert called is None
    assert "owner-secret" not in done.stdout + done.stderr


def test_migrate_without_an_owner_leaves_the_containers_url_alone(tmp_path):
    done, called = _migrate(tmp_path, "downgrade", "-1")

    assert done.returncode == 0, done.stderr
    call, url = called

    assert call == "compose run --rm -T api alembic -c db/alembic.ini downgrade -1"
    assert url == "unset"
