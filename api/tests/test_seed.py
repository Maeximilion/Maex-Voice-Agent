"""Seed: Mandant, Live-Schalter, Öffnungszeiten, Kapazität. Idempotent und mandantenfähig."""

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from api.models import Capacity, OpeningHours, ServiceConfig, Tenant
from scripts import seed as seed_module
from scripts.seed import seed

ERWARTETE_OEFFNUNGSZEITEN = 6 * 3 * 2  # sechs Tage, drei Services, zwei Fenster
ERWARTETE_KAPAZITAET = 6 * 2


@pytest.fixture
def session(migrated_db_url):
    engine = create_engine(migrated_db_url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _counts(session: Session, tenant_id) -> dict[str, int]:
    return {
        "tenants": session.scalar(select(func.count()).select_from(Tenant)),
        "opening_hours": session.scalar(
            select(func.count())
            .select_from(OpeningHours)
            .where(OpeningHours.tenant_id == tenant_id)
        ),
        "capacity": session.scalar(
            select(func.count())
            .select_from(Capacity)
            .where(Capacity.tenant_id == tenant_id)
        ),
    }


def test_seed_legt_mandant_konfiguration_zeiten_und_kapazitaet_an(session):
    result = seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    assert result.tenant_created is True
    tenant = session.get(Tenant, result.tenant_id)
    assert tenant.name == "Testbetrieb" and tenant.timezone == "Europe/Berlin"
    config = session.get(ServiceConfig, result.tenant_id)
    assert config.call_mode == "shadow"
    assert _counts(session, result.tenant_id) == {
        "tenants": 1,
        "opening_hours": ERWARTETE_OEFFNUNGSZEITEN,
        "capacity": ERWARTETE_KAPAZITAET,
    }
    montag = session.scalars(
        select(OpeningHours).where(
            OpeningHours.tenant_id == result.tenant_id, OpeningHours.weekday == 0
        )
    ).all()
    assert montag == []


def test_seed_zweimal_ergibt_denselben_zustand(session):
    erster = seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    zweiter = seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    assert zweiter.tenant_id == erster.tenant_id
    assert zweiter.tenant_created is False
    assert _counts(session, erster.tenant_id) == {
        "tenants": 1,
        "opening_hours": ERWARTETE_OEFFNUNGSZEITEN,
        "capacity": ERWARTETE_KAPAZITAET,
    }


def test_seed_ueberschreibt_live_schalter_nicht(session):
    result = seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    config = session.get(ServiceConfig, result.tenant_id)
    config.call_mode = "primary"
    config.delivery_enabled = False
    config.pickup_wait_minutes = 55
    session.commit()

    seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin")
    session.expire_all()
    config = session.get(ServiceConfig, result.tenant_id)
    assert (config.call_mode, config.delivery_enabled, config.pickup_wait_minutes) == (
        "primary",
        False,
        55,
    )


def test_seed_zweiter_mandant_laesst_den_ersten_unberuehrt(session):
    erster = seed(session, tenant_name="Betrieb A", timezone="Europe/Berlin")
    zweiter = seed(session, tenant_name="Betrieb B", timezone="Europe/Vienna")
    assert erster.tenant_id != zweiter.tenant_id
    assert (
        _counts(session, erster.tenant_id)["opening_hours"] == ERWARTETE_OEFFNUNGSZEITEN
    )
    assert _counts(session, zweiter.tenant_id)["tenants"] == 2
    assert session.get(Tenant, zweiter.tenant_id).timezone == "Europe/Vienna"


def test_cli_seedet_und_gibt_json_aus(migrated_db_url, monkeypatch, capsys):
    engine = create_engine(migrated_db_url)
    monkeypatch.setattr(seed_module, "SessionLocal", sessionmaker(bind=engine))
    try:
        assert seed_module.main(["--tenant-name", "CLI-Betrieb"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["tenant_created"] is True
        assert out["opening_hours"] == ERWARTETE_OEFFNUNGSZEITEN
        assert out["capacity"] == ERWARTETE_KAPAZITAET
        with Session(engine) as s:
            assert s.scalar(select(Tenant.name)) == "CLI-Betrieb"
    finally:
        engine.dispose()
