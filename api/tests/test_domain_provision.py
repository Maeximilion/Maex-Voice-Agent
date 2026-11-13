"""Provisioning of a customer's tenant in its stack (#573, D32): one transaction,
rerun without change, one customer per stack, the customer file's checks and
what a tenant still lacks."""

import json
import threading
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from api.config import settings
from api.core.errors import InvalidInput
from api.domain import provision
from api.domain.access.setup import use_link
from api.domain.provision import create_tenant, missing
from api.jobs import provision as job
from api.models import (
    AuditLog,
    BridgeHeartbeat,
    Capacity,
    DeliveryZone,
    GuiAccount,
    MenuItem,
    OpeningHours,
    ServiceConfig,
    Tenant,
)
from api.schemas.provision import CustomerSpec

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
TEAM = "+4972215550000"
EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "customer.example.json"
WEEK = ["tue", "wed", "thu", "fri", "sat", "sun"]


def _spec(**changes) -> CustomerSpec:
    data = {
        "key": "pizzeria-sued",
        "name": "Testbetrieb",
        "holiday_region": "BW",
        "privacy_notice_address": "example.com/datenschutz",
        "hours": [
            {
                "days": WEEK,
                "services": ["dinein", "pickup"],
                "opens": "11:30",
                "closes": "14:00",
            },
            {
                "days": WEEK,
                "services": ["dinein", "pickup"],
                "opens": "17:00",
                "closes": "22:00",
            },
        ],
        "seats": [{"days": WEEK, "start": "17:00", "end": "22:00", "guests": 40}],
        **changes,
    }
    return CustomerSpec.model_validate(data)


@pytest.fixture
def engine(migrated_db_url, monkeypatch):
    monkeypatch.setattr(settings, "team_phone", TEAM)
    monkeypatch.setattr(settings, "transfer_targets", "")
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_create_writes_the_whole_tenant_in_shadow_with_a_usable_setup_link(engine):
    with Session(engine) as session:
        done = create_tenant(session, _spec(), "operator:test", T0)
    assert done.created and done.opening_hours == 24 and done.seats == 6
    with Session(engine) as session:
        tenant = session.get(Tenant, done.tenant_id)
        assert tenant is not None
        assert (tenant.customer_key, tenant.name) == ("pizzeria-sued", "Testbetrieb")
        config = session.get(ServiceConfig, tenant.id)
        assert config is not None
        assert config.call_mode == "shadow" and config.team_phone == TEAM
        assert config.holiday_region == "BW"
        assert config.privacy_notice_address == "example.com/datenschutz"
        sunday_evening = session.scalars(
            select(OpeningHours.service).where(
                OpeningHours.weekday == 6, OpeningHours.opens_at == time(17)
            )
        ).all()
        assert sorted(sunday_evening) == ["dinein", "pickup"]
        assert (
            session.scalar(select(func.count()).where(OpeningHours.weekday == 0)) == 0
        )
        actions = session.scalars(select(AuditLog.action)).all()
        assert "tenant.provisioned" in actions
        assert done.setup_token is not None
        assert use_link(
            session, tenant.id, done.setup_token, "Kuechentablet-2026", "4711", "x", T0
        )


def test_a_rerun_changes_nothing_and_keeps_what_the_team_set(engine):
    with Session(engine) as session:
        first = create_tenant(session, _spec(), "operator:test", T0)
    with Session(engine) as session:
        config = session.get(ServiceConfig, first.tenant_id)
        assert config is not None
        config.call_mode = "primary"
        session.commit()
    rows = _count(engine, OpeningHours), _count(engine, AuditLog)

    with Session(engine) as session:
        again = create_tenant(session, _spec(name="Anders"), "operator:test", T0)
    assert (again.tenant_id, again.created, again.setup_token) == (
        first.tenant_id,
        False,
        None,
    )
    assert (_count(engine, OpeningHours), _count(engine, AuditLog)) == rows
    with Session(engine) as session:
        config = session.get(ServiceConfig, first.tenant_id)
        tenant = session.get(Tenant, first.tenant_id)
        assert config is not None and tenant is not None
        assert config.call_mode == "primary"
        assert tenant.name == "Testbetrieb"


def test_a_second_customer_in_the_same_stack_is_refused(engine):
    with Session(engine) as session:
        create_tenant(session, _spec(), "operator:test", T0)
    with Session(engine) as session, pytest.raises(InvalidInput, match="one customer"):
        create_tenant(session, _spec(key="other-place"), "operator:test", T0)
    assert _count(engine, Tenant) == 1


def test_a_stack_seeded_without_a_key_refuses_too(engine):
    with Session(engine) as session:
        session.add(Tenant(name="Alt"))
        session.commit()
    with Session(engine) as session, pytest.raises(InvalidInput, match="key none"):
        create_tenant(session, _spec(), "operator:test", T0)
    assert _count(engine, OpeningHours) == 0


def test_a_failure_in_the_last_step_leaves_no_tenant(engine, monkeypatch):
    def broken(*args):
        raise RuntimeError("database gone")

    monkeypatch.setattr(provision, "issue_link", broken)
    with Session(engine) as session, pytest.raises(RuntimeError):
        create_tenant(session, _spec(), "operator:test", T0)
    for model in (Tenant, ServiceConfig, OpeningHours, Capacity, AuditLog, GuiAccount):
        assert _count(engine, model) == 0, model


def test_a_dry_run_counts_and_writes_nothing(engine):
    with Session(engine) as session:
        done = create_tenant(session, _spec(), "operator:test", T0, dry_run=True)
    assert (done.tenant_id, done.created, done.opening_hours, done.seats) == (
        None,
        True,
        24,
        6,
    )
    assert done.setup_token is None
    assert _count(engine, Tenant) == 0


def test_the_team_phone_must_be_one_the_dialplan_dials(engine, monkeypatch):
    with Session(engine) as session, pytest.raises(InvalidInput, match="not TEAM"):
        create_tenant(session, _spec(team_phone="+4930123456"), "operator:test", T0)
    # Written the way people write it, it is the dialable number.
    with Session(engine) as session:
        done = create_tenant(
            session, _spec(team_phone="07221 555 0000"), "operator:test", T0
        )
        config = session.get(ServiceConfig, done.tenant_id)
        assert config is not None
        assert config.team_phone == TEAM


def test_an_empty_team_phone_of_the_stack_refuses(engine, monkeypatch):
    monkeypatch.setattr(settings, "team_phone", "")
    with Session(engine) as session, pytest.raises(InvalidInput, match="empty"):
        create_tenant(session, _spec(), "operator:test", T0)


def test_two_runs_at_once_create_one_tenant(engine):
    """The second run waits for the first one's lock, then finds its tenant."""
    holder = Session(engine)
    holder.execute(text("LOCK TABLE tenants IN SHARE ROW EXCLUSIVE MODE"))
    result = {}

    def second() -> None:
        with Session(engine) as session:
            result["done"] = create_tenant(session, _spec(), "operator:test", T0)

    runner = threading.Thread(target=second)
    runner.start()
    runner.join(timeout=1)
    assert runner.is_alive(), "the second run did not wait"
    holder.add(Tenant(name="Testbetrieb", customer_key="pizzeria-sued"))
    holder.commit()
    holder.close()
    runner.join(timeout=10)
    assert result["done"].created is False
    assert _count(engine, Tenant) == 1


@pytest.mark.parametrize(
    "changes, complaint",
    [
        ({"key": "Pizzeria"}, "key"),
        ({"key": "a" * 33}, "key"),
        ({"key": "pizza--sued"}, "key"),
        ({"timezone": "Europe/Karlsruhe"}, "time zone"),
        ({"holiday_region": "XX"}, "one of"),
        ({"hours": []}, "hours"),
        ({"nickname": "x"}, "nickname"),
        (
            {
                "hours": [
                    {
                        "days": ["mon", "mon"],
                        "services": ["pickup"],
                        "opens": "11:00",
                        "closes": "12:00",
                    }
                ]
            },
            "twice",
        ),
        (
            {
                "hours": [
                    {
                        "days": ["mon"],
                        "services": ["pickup"],
                        "opens": "11:00+01:00",
                        "closes": "12:00",
                    }
                ]
            },
            "offset",
        ),
        (
            {
                "hours": [
                    {
                        "days": ["mon"],
                        "services": ["pickup"],
                        "opens": "11:00",
                        "closes": "11:00",
                    }
                ]
            },
            "at once",
        ),
        (
            {
                "hours": [
                    {
                        "days": ["mon"],
                        "services": ["pickup"],
                        "opens": "11:00",
                        "closes": "14:00",
                    },
                    {
                        "days": ["mon"],
                        "services": ["pickup", "dinein"],
                        "opens": "13:30",
                        "closes": "15:00",
                    },
                ]
            },
            "hours of pickup: mon 11:00 overlaps mon 13:30",
        ),
        (
            {
                "hours": [
                    {
                        "days": ["mon"],
                        "services": ["pickup"],
                        "opens": "11:00",
                        "closes": "14:00",
                    }
                ],
                "seats": [
                    {"days": ["sun"], "start": "22:00", "end": "02:00", "guests": 10},
                    {"days": ["mon"], "start": "01:00", "end": "03:00", "guests": 10},
                ],
            },
            "seats: sun 22:00 overlaps mon 01:00",
        ),
    ],
)
def test_the_customer_file_refuses_what_would_be_guessed(changes, complaint):
    with pytest.raises(ValidationError, match=complaint):
        _spec(**changes)


def test_windows_side_by_side_and_past_midnight_are_fine():
    spec = _spec(
        hours=[
            {
                "days": ["sat"],
                "services": ["pickup"],
                "opens": "18:00",
                "closes": "02:00",
            },
            {
                "days": ["sun"],
                "services": ["pickup"],
                "opens": "02:00",
                "closes": "03:00",
            },
            {
                "days": ["sun"],
                "services": ["dinein"],
                "opens": "01:00",
                "closes": "03:00",
            },
        ]
    )
    assert len(spec.hours) == 3


def test_the_example_file_is_a_valid_customer():
    spec = CustomerSpec.model_validate_json(EXAMPLE.read_text(encoding="utf-8"))
    assert spec.key == "pilot" and spec.team_phone is None


def test_missing_names_what_the_new_tenant_lacks_until_it_is_there(engine):
    spec = _spec(
        privacy_notice_address="",
        hours=[
            {
                "days": WEEK,
                "services": ["dinein", "delivery"],
                "opens": "17:00",
                "closes": "22:00",
            }
        ],
        seats=[{"days": WEEK, "start": "17:00", "end": "22:00", "guests": 0}],
    )
    with Session(engine) as session:
        tenant_id = create_tenant(session, spec, "operator:test", T0).tenant_id
        assert tenant_id is not None
        gaps = missing(session, tenant_id)
    assert [gap.split(" (")[0].split(",")[0] for gap in gaps] == [
        "seats for reservations",
        "delivery zones",
        "menu",
        "print bridge",
        "login",
        "privacy notice address",
    ]

    with Session(engine) as session:
        session.execute(text("UPDATE capacity SET max_guests = 20"))
        session.add(
            DeliveryZone(
                tenant_id=tenant_id,
                name="Innen",
                match_type="postal_code",
                postal_codes=["76133"],
                fee_cents=0,
                min_order_cents=0,
                eta_minutes=30,
            )
        )
        session.add(
            MenuItem(
                tenant_id=tenant_id,
                number="1",
                name="Margherita",
                category="Pizza",
                price_cents=900,
            )
        )
        session.add(BridgeHeartbeat(tenant_id=tenant_id, seen_at=T0))
        session.execute(text("UPDATE gui_accounts SET password_hash = 'x'"))
        config = session.get(ServiceConfig, tenant_id)
        assert config is not None
        config.privacy_notice_address = "example.com/d"
        session.commit()
        assert missing(session, tenant_id) == []


def test_missing_of_a_tenant_without_hours(engine):
    with Session(engine) as session:
        tenant = Tenant(name="Leer")
        session.add(tenant)
        session.commit()
        assert missing(session, tenant.id)[0] == "opening hours"


# The command around it.


@pytest.fixture
def cli(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(job, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(job, "utcnow", lambda: T0)
    monkeypatch.delenv("AGENT_DOMAIN", raising=False)
    path = tmp_path / "customer.json"
    path.write_text(_spec().model_dump_json(), encoding="utf-8")
    return path


def test_create_prints_the_report_with_the_link_once(cli, capsys):
    assert (
        job.main(["create", str(cli), "--base-url", "https://agent.example.com"]) == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["created"] and not report["dry_run"]
    assert report["setup_link"].startswith("https://agent.example.com/gui/einrichten#")
    assert "menu (python -m scripts.import_menu)" in report["missing"]

    assert job.main(["create", str(cli)]) == 0
    captured = capsys.readouterr()
    again = json.loads(captured.out)
    assert (again["created"], again["setup_link"]) == (False, None)
    assert again["tenant_id"] == report["tenant_id"]
    assert "nothing changed" in captured.err


def test_create_dry_run_and_check(cli, capsys, engine):
    assert job.main(["check"]) == 2
    assert job.main(["create", str(cli), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["tenant_id"] is None
    assert _count(engine, Tenant) == 0
    assert job.main(["create", str(cli)]) == 0
    capsys.readouterr()
    assert job.main(["check"]) == 1
    assert "missing: menu" in capsys.readouterr().out


def test_create_names_where_the_file_is_wrong_without_its_values(cli, capsys):
    cli.write_text(
        json.dumps(
            {**json.loads(cli.read_text()), "team_phone": 4711, "key": "Geheim"}
        ),
        encoding="utf-8",
    )
    assert job.main(["create", str(cli)]) == 1
    err = capsys.readouterr().err
    assert "error: key:" in err and "error: team_phone:" in err
    assert "Geheim" not in err and "4711" not in err
    assert job.main(["create", str(cli.with_name("none.json"))]) == 2


def test_create_says_why_a_stack_refuses(cli, capsys, engine):
    with Session(engine) as session:
        session.add(Tenant(name="Alt"))
        session.commit()
    assert job.main(["create", str(cli)]) == 1
    assert "refused: this database already holds" in capsys.readouterr().err


@pytest.mark.parametrize("number", ["+49000000000", ""])
def test_missing_names_a_placeholder_team_number(engine, number):
    """#687: the placeholder of .env.example became the team number, and no
    gap named it."""
    with Session(engine) as session:
        tenant_id = create_tenant(session, _spec(), "operator:test", T0).tenant_id
        assert tenant_id is not None
        config = session.get(ServiceConfig, tenant_id)
        assert config is not None
        config.team_phone = number
        session.commit()
        assert any(gap.startswith("team number") for gap in missing(session, tenant_id))
