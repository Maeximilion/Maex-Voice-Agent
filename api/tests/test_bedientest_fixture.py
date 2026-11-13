"""scripts/bedientest_fixture: the start state of the operating test (T-3.5, protocol section 0)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.models import (
    Callback,
    ItemOption,
    MenuItem,
    Order,
    OrderItem,
    OutboxEvent,
    Reservation,
    ServiceConfig,
)
from scripts import bedientest_data as data
from scripts import bedientest_fixture as fx


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


# A fixed late morning, so the tests do not depend on the hour they run at: the
# reservation of `fill` must land on the same local day (see the late-evening test).
MORNING = datetime(2026, 9, 15, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
LATE = datetime(2026, 9, 15, 23, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture
def filled(session):
    return data.fill(session, now=MORNING)


def _order_by_code(session, code):
    return session.scalars(select(Order).where(Order.pickup_code == code)).one()


def _lines(session, order):
    return list(
        session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
    )


def test_fill_builds_the_start_state(session, filled):
    codes = filled["pickup_codes"]
    assert len(set(codes.values())) == 4, "B, C, D and the red card each get a code"
    assert session.scalar(select(func.count()).select_from(Order)) == 4, (
        "A does not exist yet"
    )
    for code in codes.values():
        assert _order_by_code(session, code).status == "confirmed"
    states = {k: _order_by_code(session, v).handover_state for k, v in codes.items()}
    assert states["red"] == "failed"
    assert all(v != "failed" for k, v in states.items() if k != "red")


def test_order_c_has_one_line_with_a_choice_and_a_note(session, filled):
    (line,) = _lines(session, _order_by_code(session, filled["pickup_codes"]["C"]))
    assert line.note == "ohne Zwiebeln"
    group = session.scalars(
        select(ItemOption).where(
            ItemOption.menu_item_id == line.menu_item_id,
            ItemOption.required.is_(True),
        )
    ).all()
    assert group, "the dish of order C has a required choice"


def test_order_d_carries_only_the_old_choice_of_a_group_with_two(session, filled):
    (line,) = _lines(session, _order_by_code(session, filled["pickup_codes"]["D"]))
    options = session.scalars(
        select(ItemOption.option_name).where(
            ItemOption.menu_item_id == line.menu_item_id
        )
    ).all()
    assert sorted(options) == ["Erdnuss", "Kokos"]
    assert [o["option"] for o in line.options] == ["Erdnuss"], (
        "only the old choice is on the line"
    )
    on_cards = {
        _lines(session, _order_by_code(session, c))[0].menu_item_id
        for k, c in filled["pickup_codes"].items()
        if k != "D"
    }
    assert line.menu_item_id not in on_cards, "D's dish is on no other card"


def test_dish_23_is_available_and_on_no_card(session, filled):
    dish = session.scalars(select(MenuItem).where(MenuItem.number == "23")).one()
    assert dish.active
    assert not session.scalars(
        select(OrderItem).where(OrderItem.menu_item_id == dish.id)
    ).all()


def test_service_config_matches_the_reset_rules(session, filled):
    config = session.scalars(select(ServiceConfig)).one()
    assert config.call_mode == "primary"
    assert (
        config.pickup_wait_standard_minutes,
        config.delivery_wait_standard_minutes,
    ) == (25, 50)
    assert config.order_approval_above_cents == data.APPROVAL_ABOVE_CENTS
    assert (
        filled["restore_values"]["order_approval_overdue_minutes"]
        == config.order_approval_overdue_minutes
    )


def test_one_complaint_callback_and_one_reservation_ahead(session, filled):
    (callback,) = session.scalars(select(Callback)).all()
    assert callback.reason == "complaint" and callback.status == "open"
    (reservation,) = session.scalars(select(Reservation)).all()
    assert reservation.status == "confirmed"


def test_no_slip_was_sent_by_the_fixture(session, filled):
    # The dispatcher is not part of the stack: every event is still unsent.
    assert not session.scalars(
        select(OutboxEvent).where(OutboxEvent.sent_at.is_not(None))
    ).all()


def test_fill_twice_is_refused(session, filled):
    with pytest.raises(SystemExit, match="reset"):
        data.fill(session)


def test_arrivals_a_e_and_callback(session, filled):
    a = data.arrive(session, "a")["pickup_code"]
    e = data.arrive(session, "e")["pickup_code"]
    assert a and e and a != e
    assert session.scalar(select(func.count()).select_from(Order)) == 6
    dish_of_e = _lines(session, _order_by_code(session, e))[0].menu_item_id
    assert session.get(MenuItem, dish_of_e).number != "23"
    before = session.scalar(select(func.count()).select_from(Callback))
    data.arrive(session, "callback")
    assert session.scalar(select(func.count()).select_from(Callback)) == before + 1


def test_arrive_without_fill_names_the_missing_stack(session):
    with pytest.raises(SystemExit, match="up"):
        data.arrive(session, "a")


# --- host side: nothing here may name the working stack ----------------------------


def test_compose_names_only_the_throwaway_project():
    cmd = fx.compose("down", "-v")
    assert cmd[:4] == ["docker", "compose", "-p", "bedientest"]
    files = [cmd[i + 1] for i, part in enumerate(cmd) if part == "-f"]
    assert all("override.yml" in f or f.endswith("docker-compose.yml") for f in files)
    assert not any(f.endswith("docker-compose.override.yml") for f in files)


def test_default_port_is_not_the_working_stacks(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORK_DIR", tmp_path / "w")
    fx.write_config("127.0.0.1", fx.DEFAULT_PORT)
    override = (tmp_path / "w" / "override.yml").read_text()
    assert ":18000:8000" in override and ":8000:8000" not in override
    assert list((tmp_path / "w").iterdir()) == [tmp_path / "w" / "override.yml"], (
        "no file holds the stack's credentials"
    )


def test_the_stack_gets_its_settings_from_the_process_not_the_checkout(monkeypatch):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        fx.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, env=kw["env"])
    )
    fx.run(fx.compose("ps"), dry_run=False)
    assert seen["cmd"][seen["cmd"].index("--env-file") + 1] == "/dev/null"
    assert seen["env"]["ENV"] == "dev"
    assert seen["env"]["KITCHEN_BRIDGE_TENANT_ID"] == ""
    assert seen["env"]["DATABASE_URL"].endswith("@db:5432/maex_agent")


def test_dry_run_runs_nothing(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: called.append(a))
    assert fx.main(["up", "--dry-run"]) == 0
    assert fx.main(["arrive", "a", "--dry-run"]) == 0
    assert fx.main(["down", "--dry-run"]) == 0
    assert not called
    out = capsys.readouterr().out
    assert "dispatcher" not in out and "-p bedientest" in out
    # The fresh volume has no login: `up` makes the setup link for the tablet (#570).
    assert "api.jobs.gui_access link --base-url http://127.0.0.1:18000" in out


def test_a_lan_bind_is_refused_unless_asked_for_twice(monkeypatch, capsys):
    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: None)
    with pytest.raises(SystemExit) as refused:
        fx.main(["up", "--dry-run", "--bind", "0.0.0.0"])
    assert refused.value.code == 2 and "--allow-lan" in capsys.readouterr().err
    fx.main(["up", "--dry-run", "--bind", "0.0.0.0", "--allow-lan"])
    assert "ENV=dev" in capsys.readouterr().out
    fx.main(["up", "--dry-run"])
    assert "Note:" not in capsys.readouterr().out


def test_reset_keeps_the_lan_bind_of_the_last_up_only_when_allowed_again(
    tmp_path, monkeypatch, capsys
):
    """#689: a plain `reset` reopened the LAN bind without --allow-lan, and
    without the warning."""
    monkeypatch.setattr(fx, "WORK_DIR", tmp_path)
    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: None)
    fx.main(["up", "--bind", "192.168.1.5", "--allow-lan"])
    capsys.readouterr()
    fx.main(["reset", "--allow-lan"])
    assert "192.168.1.5:18000:8000" in (tmp_path / "override.yml").read_text()
    # The link and the note name the address that is bound, not the arguments'.
    out = capsys.readouterr().out
    assert "http://192.168.1.5:18000" in out and "Note: bound to 192.168.1.5" in out
    fx.main(["reset"])
    assert "127.0.0.1:18000:8000" in (tmp_path / "override.yml").read_text()


def test_reset_keeps_a_loopback_port_of_the_last_up(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORK_DIR", tmp_path)
    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: None)
    fx.main(["up", "--port", "18123"])
    fx.main(["reset"])
    assert "127.0.0.1:18123:8000" in (tmp_path / "override.yml").read_text()


def test_down_forgets_the_bind(tmp_path, monkeypatch):
    """#689: `down` left the LAN bind for the next `reset`."""
    monkeypatch.setattr(fx, "WORK_DIR", tmp_path)
    monkeypatch.setattr(fx.subprocess, "run", lambda *a, **k: None)
    fx.main(["up", "--bind", "192.168.1.5", "--allow-lan"])
    fx.main(["down"])
    assert not (tmp_path / "override.yml").exists()
    fx.main(["reset"])
    assert "127.0.0.1:18000:8000" in (tmp_path / "override.yml").read_text()


def test_the_shell_of_the_caller_cannot_redirect_the_stack(monkeypatch):
    for name, value in {
        "DATABASE_URL": "postgresql://working/db",
        "N8N_WEBHOOK_URL": "https://example.com/hook",
        "COMPOSE_FILE": "other.yml",
        "COMPOSE_PROJECT_NAME": "maex-voice-agent",
        "DOCKER_HOST": "unix:///run/docker.sock",
    }.items():
        monkeypatch.setenv(name, value)
    seen = {}
    monkeypatch.setattr(fx.subprocess, "run", lambda cmd, **kw: seen.update(kw))
    fx.run(fx.compose("ps"), dry_run=False)
    env = seen["env"]
    assert env["DATABASE_URL"] == fx.STACK_ENV["DATABASE_URL"]
    assert not {"N8N_WEBHOOK_URL", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"} & set(env)
    assert env["DOCKER_HOST"] == "unix:///run/docker.sock", (
        "the docker client still finds its daemon"
    )


def test_a_late_evening_start_is_refused_not_misdated(session):
    with pytest.raises(SystemExit, match="too late"):
        data.fill(session, now=LATE)
