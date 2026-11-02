"""check_delivery (T-6.3, docs/04, D5): the zone of a postal code, every amount
from the database, and a guess nowhere."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import SAY_ON_FAILURE, InvalidInput, OutOfZone, ServiceUnavailable
from api.db import get_db
from api.domain.delivery import (
    SAY_OUT_OF_ZONE,
    SAY_POSTAL_CODE_NOT_UNDERSTOOD,
    check_delivery,
)
from api.main import app
from api.models import Call, DeliveryZone
from api.tests.conftest import p95_ms
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}
POSTAL_CODE = "76530"


@pytest.fixture
def engine(migrated_db_url):
    engine = create_engine(migrated_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def tenant_id(session):
    return uuid.UUID(
        seed(session, tenant_name="Testbetrieb", timezone="Europe/Berlin").tenant_id
    )


def zone(session, tenant_id, postal_codes=(POSTAL_CODE, "76532"), **values):
    row = DeliveryZone(
        **{
            "tenant_id": tenant_id,
            "name": "Zone 1",
            "match_type": "postal_code",
            "postal_codes": list(postal_codes),
            "fee_cents": 250,
            "min_order_cents": 2000,
            "eta_minutes": 50,
            **values,
        }
    )
    session.add(row)
    session.commit()
    return row


def test_a_postal_code_in_a_zone_gets_the_values_of_that_zone(session, tenant_id):
    row = zone(session, tenant_id)
    zone(session, tenant_id, postal_codes=["76534"], name="Zone 2", fee_cents=400)

    found = check_delivery(session, tenant_id, POSTAL_CODE)

    assert found.model_dump() == {
        "deliverable": True,
        "zone_id": row.id,
        "zone_name": "Zone 1",
        "fee_cents": 250,
        "min_order_cents": 2000,
        "eta_minutes": 50,
    }


def test_a_free_delivery_without_a_minimum_is_still_a_zone(session, tenant_id):
    zone(session, tenant_id, fee_cents=0, min_order_cents=0, eta_minutes=0)

    found = check_delivery(session, tenant_id, POSTAL_CODE)

    assert (found.fee_cents, found.min_order_cents) == (0, 0)


def test_any_code_of_the_list_matches(session, tenant_id):
    row = zone(session, tenant_id)

    assert check_delivery(session, tenant_id, "76532").zone_id == row.id


@pytest.mark.parametrize("heard", ["76 530", " 76530 ", "7 6 5 3 0", "76530\n"])
def test_spaces_from_speech_are_no_other_postal_code(session, tenant_id, heard):
    row = zone(session, tenant_id)

    assert check_delivery(session, tenant_id, heard).zone_id == row.id


def test_a_postal_code_outside_every_zone_offers_pickup(session, tenant_id):
    zone(session, tenant_id)

    with pytest.raises(OutOfZone) as refused:
        check_delivery(session, tenant_id, "54321")

    assert refused.value.say == SAY_OUT_OF_ZONE
    assert "abholen" in SAY_OUT_OF_ZONE


def test_a_tenant_without_zones_delivers_nowhere(session, tenant_id):
    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, POSTAL_CODE)


def test_an_inactive_zone_delivers_nowhere(session, tenant_id):
    zone(session, tenant_id, active=False)

    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, POSTAL_CODE)


def test_the_zone_of_another_tenant_does_not_count(session, tenant_id):
    zone(session, tenant_id)
    other = seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id

    with pytest.raises(OutOfZone):
        check_delivery(session, uuid.UUID(other), POSTAL_CODE)


def test_a_polygon_zone_is_not_read_by_its_postal_codes(session, tenant_id):
    """T-6.4 matches it by its shape; the list next to it decides nothing."""
    zone(session, tenant_id, match_type="polygon", polygon={"type": "Polygon"})

    with pytest.raises(OutOfZone):
        check_delivery(session, tenant_id, POSTAL_CODE)


def test_two_zones_for_one_postal_code_are_no_guess_at_the_fee(session, tenant_id):
    """Hard rule 2: the fee would depend on the order the rows come back in, so
    the call goes to the team (hard rule 5)."""
    zone(session, tenant_id, postal_codes=[POSTAL_CODE])
    zone(session, tenant_id, postal_codes=[POSTAL_CODE], name="Zone 2", fee_cents=400)

    with pytest.raises(ServiceUnavailable) as refused:
        check_delivery(session, tenant_id, POSTAL_CODE)

    assert refused.value.say == SAY_ON_FAILURE


def test_an_inactive_twin_is_no_overlap(session, tenant_id):
    row = zone(session, tenant_id)
    zone(session, tenant_id, name="Alt", active=False)

    assert check_delivery(session, tenant_id, POSTAL_CODE).zone_id == row.id


@pytest.mark.parametrize(
    "heard", ["", "   ", "7653", "765300", "abcde", "7653a", "٧٦٥٣٠", "D-76530"]
)
def test_a_postal_code_not_understood_is_asked_again(session, tenant_id, heard):
    """A garbled code must never read as outside the zone: the guest would lose
    the delivery to a hearing error."""
    zone(session, tenant_id)

    with pytest.raises(InvalidInput) as refused:
        check_delivery(session, tenant_id, heard)

    assert refused.value.say == SAY_POSTAL_CODE_NOT_UNDERSTOOD


@pytest.fixture
def client(engine, tenant_id, session):
    call = Call(
        tenant_id=tenant_id,
        external_session_id="ext",
        started_at=datetime.now(UTC),
        delete_after=datetime.now(UTC).date(),
    )
    session.add(call)
    session.commit()

    def override_get_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield (
            TestClient(app),
            {
                "call_id": str(call.id),
                "tenant_id": str(tenant_id),
                "postal_code": POSTAL_CODE,
                "street": "Musterstraße",
                "house_number": "1",
                "city": "Musterstadt",
            },
        )
    finally:
        app.dependency_overrides.clear()


def test_the_tool_answers_in_the_envelope(client, session, tenant_id):
    http, body = client
    row = zone(session, tenant_id)

    response = http.post("/v1/tools/check_delivery", json=body, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "deliverable": True,
            "zone_id": str(row.id),
            "zone_name": "Zone 1",
            "fee_cents": 250,
            "min_order_cents": 2000,
            "eta_minutes": 50,
        },
        "say": None,
    }


def test_the_postal_code_alone_answers_a_question_about_the_area(
    client, session, tenant_id
):
    """docs/05 Auskunft: asked whether we deliver there, a guest names no street."""
    http, body = client
    zone(session, tenant_id)
    question = {key: body[key] for key in ("call_id", "tenant_id", "postal_code")}

    response = http.post("/v1/tools/check_delivery", json=question, headers=AUTH)

    assert response.json()["ok"] is True


def test_the_tool_answers_out_of_zone_with_the_pickup_offer(client):
    http, body = client

    response = http.post("/v1/tools/check_delivery", json=body, headers=AUTH)

    assert response.status_code == 200
    answer = response.json()
    assert answer["ok"] is False
    assert answer["error"]["code"] == "out_of_zone"
    assert answer["say"] == SAY_OUT_OF_ZONE


def test_the_tool_refuses_a_request_without_a_postal_code(client):
    http, body = client
    del body["postal_code"]

    response = http.post("/v1/tools/check_delivery", json=body, headers=AUTH)

    assert response.json()["error"]["code"] == "invalid_input"


def test_the_tool_needs_the_token(client):
    http, body = client

    assert http.post("/v1/tools/check_delivery", json=body).status_code == 401


def test_the_address_stays_out_of_the_call_log(client, session, tenant_id):
    """The log keeps the outcome: the address of a guest is no part of it."""
    http, body = client
    zone(session, tenant_id)

    http.post("/v1/tools/check_delivery", json=body, headers=AUTH)

    call = session.get(Call, uuid.UUID(body["call_id"]))
    session.refresh(call)
    assert [entry["name"] for entry in call.tool_calls] == ["check_delivery"]
    assert "Musterstraße" not in str(call.tool_calls)


@pytest.mark.latency
def test_the_tool_stays_in_its_budget(client, session, tenant_id):
    http, body = client
    zone(session, tenant_id)

    p95_ms(lambda: http.post("/v1/tools/check_delivery", json=body, headers=AUTH))
