"""The application does not run as a Postgres superuser on the server (#252).

Two halves that need no shell: the API refuses to start as a superuser outside
dev and test, and the Compose files keep the owner's credentials out of the
containers that run the application. The role itself is set up by
`scripts/db_role.sh` (test_scripts_db_role.py).
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from api.config import settings
from api.db import refuse_a_superuser_role
from api.main import app
from api.tests.test_compose_ports import BASE, PROD, _load, _services
from api.tests.test_deploy_exposure import OWN_CODE

OWNER_ONLY = ("POSTGRES_PASSWORD", "OWNER_DATABASE_URL", "BACKUP_DATABASE_URL")


@pytest.fixture
def superuser():
    """The connection the suite itself uses; it creates databases, so it is one."""
    engine = create_engine(settings.database_url, poolclass=NullPool)
    with engine.connect() as conn:
        if not conn.scalar(text("select current_setting('is_superuser')::bool")):
            pytest.skip("DATABASE_URL of this run is no superuser")
    yield engine
    engine.dispose()


@pytest.fixture
def plain_role(superuser):
    """A role that can log in and nothing more, as scripts/db_role.sh leaves it."""
    name = f"plain_{uuid.uuid4().hex[:10]}"
    with superuser.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"create role \"{name}\" login password 'plain-secret'"))
    url = make_url(settings.database_url).set(username=name, password="plain-secret")
    engine = create_engine(url, poolclass=NullPool)
    yield engine
    engine.dispose()
    with superuser.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'drop role "{name}"'))


def test_a_superuser_is_refused_on_the_server(superuser):
    with pytest.raises(RuntimeError, match="superuser") as refused:
        refuse_a_superuser_role("prod", superuser)

    # The message ends up in a log and names the way out.
    assert "scripts/db_role.sh" in str(refused.value)


@pytest.mark.parametrize("env", ["produktion", "staging", ""])
def test_every_env_but_dev_and_test_counts_as_the_server(superuser, env):
    with pytest.raises(RuntimeError):
        refuse_a_superuser_role(env, superuser)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_the_workbench_keeps_its_single_user(superuser, env):
    refuse_a_superuser_role(env, superuser)


def test_a_role_of_its_own_starts(plain_role):
    refuse_a_superuser_role("prod", plain_role)


def test_a_database_that_does_not_answer_does_not_stop_the_start(caplog):
    nowhere = make_url(settings.database_url).set(port=1)
    engine = create_engine(
        nowhere, poolclass=NullPool, connect_args={"connect_timeout": 2}
    )

    with caplog.at_level(logging.WARNING, logger="api.db"):
        refuse_a_superuser_role("prod", engine)

    assert "could not be checked" in caplog.text


def test_the_api_runs_the_check_when_it_starts(monkeypatch, superuser):
    monkeypatch.setattr(settings, "env", "prod")

    with pytest.raises(RuntimeError, match="superuser"), TestClient(app):
        pass


def test_the_api_starts_as_before_on_the_workbench():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_the_owners_credentials_stay_out_of_the_applications_containers():
    """`env_file: .env` hands a container every line of .env. The owner's
    password in the environment of the application would make its own role
    pointless: whoever reads the environment has the superuser."""
    base = _services(_load(BASE))
    production = _services(_load(PROD))
    for service in OWN_CODE:
        environment = base[service].get("environment") or {}
        override = production.get(service, {}).get("environment") or {}
        for name in OWNER_ONLY:
            assert environment.get(name) == "", f"{service} is handed {name}"
            assert name not in override, f"production hands {service} {name} again"
