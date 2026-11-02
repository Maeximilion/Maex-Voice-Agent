"""find_customer (T-6.2 part 1, docs/04, legal check L12): who a number is,
without one stored word for the model to say."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.config import settings
from api.core.errors import InvalidInput
from api.db import get_db
from api.domain.customers import SAY_SAME_ADDRESS, find_customer
from api.main import app
from api.models import Address, Call, Customer, RecognitionObjection
from api.tests.conftest import p95_ms
from scripts.seed import seed

AUTH = {"Authorization": f"Bearer {settings.agent_api_token}"}
CALLER = "+4972215551234"
NATIONAL = "07221 5551234"
STARTED = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


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


def customer(
    session, tenant_id, name="Müller", phone=CALLER, blocked=False, order_count=1
):
    """A customer with a confirmed delivery from this number."""
    row = Customer(
        tenant_id=tenant_id,
        phone=phone,
        name=name,
        blocked=blocked,
        order_count=order_count,
    )
    session.add(row)
    session.flush()
    return row


def address(session, tenant_id, row, is_default=True):
    home = Address(
        tenant_id=tenant_id,
        customer_id=row.id,
        street="Rheinstraße",
        house_number="54",
        postal_code="76530",
        city="Ort",
        is_default=is_default,
    )
    session.add(home)
    session.flush()
    return home


def test_a_known_number_finds_the_customer(session, tenant_id):
    row = customer(session, tenant_id)
    home = address(session, tenant_id, row)

    found = find_customer(session, tenant_id, CALLER)

    assert (found.found, found.customer_id, found.address_id) == (True, row.id, home.id)
    assert found.say == SAY_SAME_ADDRESS


def test_the_number_is_normalized_at_entry(session, tenant_id):
    row = customer(session, tenant_id)

    assert find_customer(session, tenant_id, NATIONAL).customer_id == row.id


def test_an_unknown_number_finds_nobody(session, tenant_id):
    customer(session, tenant_id, phone="+4972215559999")

    found = find_customer(session, tenant_id, CALLER)

    assert (found.found, found.customer_id, found.say) == (False, None, None)


def test_nothing_stored_reaches_the_model(session, tenant_id):
    """L12: the agent never says stored data aloud unprompted. What it was
    never given it cannot say, and the name and the street stay at home."""
    row = customer(session, tenant_id, name="Müller")
    address(session, tenant_id, row)

    data = find_customer(session, tenant_id, CALLER).model_dump(mode="json")

    assert set(data) == {"found", "customer_id", "address_id"}
    text = json.dumps(data)
    for stored in ("Müller", "Rheinstraße", "54", "76530", "Ort"):
        assert stored not in text.replace(str(data["customer_id"]), "").replace(
            str(data["address_id"]), ""
        )


def test_the_sentence_asks_and_names_nothing(session, tenant_id):
    assert SAY_SAME_ADDRESS == "An dieselbe Adresse wie beim letzten Mal?"


def test_an_objection_switches_recognition_off(session, tenant_id):
    """L12, Art. 21: for the number, whoever stands behind it."""
    row = customer(session, tenant_id)
    address(session, tenant_id, row)
    session.add(RecognitionObjection(tenant_id=tenant_id, phone=CALLER))
    session.flush()

    found = find_customer(session, tenant_id, NATIONAL)

    assert (found.found, found.customer_id, found.address_id, found.say) == (
        False,
        None,
        None,
        None,
    )


def test_a_blocked_customer_is_not_recognized(session, tenant_id):
    customer(session, tenant_id, blocked=True)

    assert find_customer(session, tenant_id, CALLER).found is False


@pytest.mark.parametrize("orders", [0, 1])
def test_a_blocked_customer_counts_on_a_shared_number(session, tenant_id, orders):
    """#207: taken out before the count, the other one looked like the only
    customer on the number, and the blocked guest would hear that address.
    Also a blocked row no order was confirmed on."""
    customer(session, tenant_id, name="Müller", blocked=True, order_count=orders)
    row = customer(session, tenant_id, name="Schmidt")
    address(session, tenant_id, row)

    found = find_customer(session, tenant_id, CALLER)

    assert (found.found, found.customer_id, found.say) == (False, None, None)


def test_a_name_finds_the_customer_next_to_a_blocked_one(session, tenant_id):
    customer(session, tenant_id, name="Müller", blocked=True)
    row = customer(session, tenant_id, name="Schmidt")

    found = find_customer(session, tenant_id, CALLER, "Schmidt")

    assert found.customer_id == row.id


def test_a_blocked_customer_is_not_found_by_name(session, tenant_id):
    customer(session, tenant_id, name="Müller", blocked=True)
    customer(session, tenant_id, name="Schmidt")

    assert find_customer(session, tenant_id, CALLER, "Müller").found is False


def test_a_shared_number_without_a_name_finds_nobody(session, tenant_id):
    """Never guess which customer is meant (docs/04)."""
    customer(session, tenant_id, name="Müller")
    customer(session, tenant_id, name="Schmidt")

    assert find_customer(session, tenant_id, CALLER).found is False


@pytest.mark.parametrize("said", ["Schmidt", "schmidt"])
def test_the_name_tells_a_shared_number_apart(session, tenant_id, said):
    customer(session, tenant_id, name="Müller")
    row = customer(session, tenant_id, name="Schmidt")

    assert find_customer(session, tenant_id, CALLER, said).customer_id == row.id


def test_a_name_in_the_other_spelling_is_the_same_name(session, tenant_id):
    row = customer(session, tenant_id, name="Müller")

    assert find_customer(session, tenant_id, CALLER, "Mueller").customer_id == row.id


def test_a_name_that_does_not_match_is_a_new_customer(session, tenant_id):
    """L12: the record of someone else on that number stays closed."""
    row = customer(session, tenant_id, name="Müller")
    address(session, tenant_id, row)

    found = find_customer(session, tenant_id, CALLER, "Schmidt")

    assert (found.found, found.customer_id, found.say) == (False, None, None)


def test_a_customer_without_a_default_address_gets_no_question(session, tenant_id):
    row = customer(session, tenant_id)
    address(session, tenant_id, row, is_default=False)

    found = find_customer(session, tenant_id, CALLER)

    assert (found.found, found.address_id, found.say) == (True, None, None)


def test_a_row_no_confirmed_delivery_made_is_not_recognized(session, tenant_id):
    """Security audit 02.11.2026: a draft nobody said yes to, or a booking
    under a number the guest only named, writes a row and counts no order on
    it. It must not give its name to whoever calls from the number later."""
    customer(session, tenant_id, order_count=0)

    assert find_customer(session, tenant_id, CALLER).found is False
    assert find_customer(session, tenant_id, CALLER, "Müller").found is False


def test_a_row_without_an_order_hides_nobody(session, tenant_id):
    customer(session, tenant_id, name="Müller", order_count=0)
    row = customer(session, tenant_id, name="Müller")

    assert find_customer(session, tenant_id, CALLER).customer_id == row.id
    assert find_customer(session, tenant_id, CALLER, "Müller").customer_id == row.id


def test_another_tenant_finds_nobody(session, tenant_id):
    customer(session, tenant_id)
    other = seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id

    assert find_customer(session, uuid.UUID(other), CALLER).found is False


def test_a_number_that_is_none_is_refused(session, tenant_id):
    with pytest.raises(InvalidInput):
        find_customer(session, tenant_id, "unbekannt")


def _call(session, tenant_id, caller_id: str | None = NATIONAL) -> Call:
    call = Call(
        tenant_id=tenant_id,
        external_session_id=uuid.uuid4().hex,
        started_at=STARTED,
        delete_after=STARTED.date(),
        caller_id=caller_id,
    )
    session.add(call)
    session.commit()
    return call


@pytest.fixture
def client(engine, tenant_id, session):
    call = _call(session, tenant_id)

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
            },
        )
    finally:
        app.dependency_overrides.clear()


def test_the_tool_answers_in_the_envelope(client, session, tenant_id):
    http, body = client
    row = customer(session, tenant_id)
    address(session, tenant_id, row)
    session.commit()

    response = http.post("/v1/tools/find_customer", json=body, headers=AUTH)

    assert response.status_code == 200
    answer = response.json()
    assert answer["ok"] is True
    assert answer["data"]["customer_id"] == str(row.id)
    assert answer["say"] == SAY_SAME_ADDRESS


def test_the_tool_takes_the_name(client, session, tenant_id):
    http, body = client
    customer(session, tenant_id, name="Müller")
    session.commit()

    response = http.post(
        "/v1/tools/find_customer",
        json={**body, "guest_name": "Schmidt"},
        headers=AUTH,
    )

    assert response.json()["data"]["found"] is False


def test_the_tool_needs_the_token(client):
    http, body = client

    assert http.post("/v1/tools/find_customer", json=body).status_code == 401


def test_the_tool_goes_by_the_line_of_the_call(client, session, tenant_id):
    """Security audit 02.11.2026: a number in the request is no key to
    someone's record (L12), as in the core (`agent/dispatch.py`)."""
    http, body = client
    other = customer(session, tenant_id, phone="+4972215559999")
    address(session, tenant_id, other)
    session.commit()

    response = http.post(
        "/v1/tools/find_customer",
        json={**body, "caller_id": "+4972215559999"},
        headers=AUTH,
    )

    assert response.json()["ok"] is True
    assert response.json()["data"]["found"] is False


@pytest.mark.parametrize("caller_id", [None, "anonymous"])
def test_a_withheld_line_recognizes_nobody_over_the_tool(
    client, session, tenant_id, caller_id
):
    http, body = client
    row = customer(session, tenant_id)
    address(session, tenant_id, row)
    call = _call(session, tenant_id, caller_id=caller_id)

    response = http.post(
        "/v1/tools/find_customer",
        json={**body, "call_id": str(call.id), "caller_id": CALLER},
        headers=AUTH,
    )

    assert response.json()["data"]["found"] is False


def test_a_call_of_another_tenant_recognizes_nobody(client, session, tenant_id):
    http, body = client
    row = customer(session, tenant_id)
    address(session, tenant_id, row)
    other = seed(session, tenant_name="Anderer", timezone="Europe/Berlin").tenant_id
    call = _call(session, uuid.UUID(other))

    response = http.post(
        "/v1/tools/find_customer",
        json={**body, "call_id": str(call.id)},
        headers=AUTH,
    )

    assert response.json()["data"]["found"] is False


@pytest.mark.latency
def test_the_tool_stays_in_its_budget(client, session, tenant_id):
    http, body = client
    row = customer(session, tenant_id)
    address(session, tenant_id, row)
    session.commit()

    p95_ms(lambda: http.post("/v1/tools/find_customer", json=body, headers=AUTH))
