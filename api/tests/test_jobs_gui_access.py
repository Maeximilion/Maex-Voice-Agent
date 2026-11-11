"""The operator's command for the restaurant's login (#570): setup link, devices,
sign-out of a lost tablet."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from api.domain.access.hashing import token_hash
from api.domain.access.sessions import resolve, sign_in
from api.domain.access.setup import use_link
from api.jobs import gui_access
from api.models import AuditLog, GuiSetupLink, Tenant

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
PASSWORD = "Kuechentablet-2026"


@pytest.fixture
def engine(migrated_db_url, monkeypatch):
    engine = create_engine(migrated_db_url)
    monkeypatch.setattr(gui_access, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(gui_access, "utcnow", lambda: T0)
    monkeypatch.delenv("AGENT_DOMAIN", raising=False)
    yield engine
    engine.dispose()


@pytest.fixture
def tenant_id(engine):
    with Session(engine) as session:
        tenant = Tenant(name="Testbetrieb")
        session.add(tenant)
        session.commit()
        return tenant.id


def _link(capsys, *args: str) -> str:
    assert gui_access.main(["link", *args]) == 0
    return capsys.readouterr().out.strip()


def test_link_prints_the_setup_link_once_and_stores_only_its_hash(
    engine, tenant_id, capsys
):
    url = _link(capsys, "--base-url", "https://agent.example.com/")
    prefix = "https://agent.example.com/gui/einrichten#"
    assert url.startswith(prefix)
    code = url.removeprefix(prefix)
    with Session(engine) as session:
        stored = session.scalars(select(GuiSetupLink.token_hash)).one()
        assert stored == token_hash(code) and code not in stored
        assert "gui.setup_link_issued" in session.scalars(select(AuditLog.action)).all()
        assert use_link(session, tenant_id, code, PASSWORD, "4711", "x", T0)


def test_link_takes_the_domain_of_the_stack_or_prints_the_path(
    engine, tenant_id, capsys, monkeypatch
):
    assert _link(capsys).startswith("/gui/einrichten#")
    monkeypatch.setenv("AGENT_DOMAIN", "lokal.example.com")
    assert _link(capsys).startswith("https://lokal.example.com/gui/einrichten#")


def test_sign_out_ends_one_device_by_its_id_or_all(engine, tenant_id, capsys):
    code = _link(capsys).split("#")[1]
    with Session(engine) as session:
        first = use_link(session, tenant_id, code, PASSWORD, "4711", "Tablet", T0).token
        second = sign_in(session, tenant_id, PASSWORD, "192.0.2.1", "Handy", T0).token
        second_access = resolve(session, second, tenant_id, T0)
    assert second_access is not None
    one = second_access.session_id

    assert gui_access.main(["devices"]) == 0
    listed = capsys.readouterr().out
    assert str(one) in listed and "Handy" in listed and "Tablet" in listed

    assert gui_access.main(["sign-out", str(one)[:7]]) == 1  # too short to be sure
    assert gui_access.main(["sign-out", str(one)[:8]]) == 0
    with Session(engine) as session:
        assert resolve(session, second, tenant_id, T0) is None
        assert resolve(session, first, tenant_id, T0) is not None
    assert gui_access.main(["sign-out", "--all"]) == 0
    with Session(engine) as session:
        assert resolve(session, first, tenant_id, T0) is None
    assert gui_access.main(["sign-out", "--all"]) == 1  # nobody left


def test_without_a_tenant_nothing_happens(engine, capsys):
    assert gui_access.main(["link"]) == 2
    assert "No tenant" in capsys.readouterr().err


def test_pin_shows_and_sets_the_team_pin(engine, tenant_id, capsys):
    """Maxi, 09.11.2026: the dev sees and changes the team PIN."""
    assert gui_access.main(["pin"]) == 1
    assert "No team PIN set." in capsys.readouterr().out
    code = _link(capsys).split("#")[1]
    with Session(engine) as session:
        use_link(session, tenant_id, code, PASSWORD, "4711", "Tablet", T0)
    assert gui_access.main(["pin"]) == 0
    assert capsys.readouterr().out.strip() == "4711"
    assert gui_access.main(["pin", "--set", "12"]) == 1
    assert "4 bis 6 Ziffern" in capsys.readouterr().err
    assert gui_access.main(["pin", "--set", "908172"]) == 0
    capsys.readouterr()
    assert gui_access.main(["pin"]) == 0
    assert capsys.readouterr().out.strip() == "908172"


def test_pin_admin_shows_and_sets_the_owners_pin(engine, tenant_id, capsys):
    """Maxi, 09.11.2026 (D34 point 8): the dev sees and changes the admin PIN too."""
    code = _link(capsys).split("#")[1]
    with Session(engine) as session:
        use_link(session, tenant_id, code, PASSWORD, "4711", "Tablet", T0)
    assert gui_access.main(["pin", "--admin"]) == 1
    assert "No admin PIN set." in capsys.readouterr().out
    # Never the team PIN: the staff know it.
    assert gui_access.main(["pin", "--admin", "--set", "4711"]) == 1
    assert "Team-PIN" in capsys.readouterr().err
    assert gui_access.main(["pin", "--admin", "--set", "080815"]) == 0
    capsys.readouterr()
    assert gui_access.main(["pin", "--admin"]) == 0
    assert capsys.readouterr().out.strip() == "080815"
    # The team PIN stays as it was.
    assert gui_access.main(["pin"]) == 0
    assert capsys.readouterr().out.strip() == "4711"
