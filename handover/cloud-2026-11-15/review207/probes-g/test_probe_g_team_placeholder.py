"""Probe (review #207, area G): the remedy `missing()` names for a placeholder
team number ("TEAM_PHONE in .env") does not clear it. After the operator sets
TEAM_PHONE and reruns provisioning, the tenant's stored team number stays the
placeholder, which the dialplan no longer dials."""

from sqlalchemy.orm import Session

from api.config import TEAM_PHONE_PLACEHOLDER, settings
from api.domain.calls.routing import call_routing
from api.domain.provision import create_tenant, missing
from api.domain.status.staff_changes import dialable_numbers
from api.tests.test_domain_provision import T0, _spec, engine  # noqa: F401

REAL = "+4972215559999"


def test_following_the_hint_clears_the_placeholder(engine, monkeypatch):
    # Customer file without team_phone, .env still the example's placeholder.
    monkeypatch.setattr(settings, "team_phone", TEAM_PHONE_PLACEHOLDER)
    with Session(engine) as session:
        tenant_id = create_tenant(session, _spec(), "operator:test", T0).tenant_id
        assert any(g.startswith("team number") for g in missing(session, tenant_id))
    # The operator follows the hint: TEAM_PHONE in .env, restart, rerun.
    monkeypatch.setattr(settings, "team_phone", REAL)
    with Session(engine) as session:
        again = create_tenant(session, _spec(), "operator:test", T0)
        assert not again.created
        routed = call_routing(session, tenant_id).team_phone
        gaps = missing(session, tenant_id)
    assert routed in dialable_numbers(), (routed, dialable_numbers())
    assert not any(g.startswith("team number") for g in gaps), gaps
