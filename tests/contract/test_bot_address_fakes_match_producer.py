"""The bots' fake address rows against what the address endpoints really send (address-label contract, 2026-11-17).

The customer-bot harness and the operator journeys answer the bots' address reads from fake rows. Those rows used to
be hand-written from each screen's own guesses (`address_line1`, `city`; `title` where the staff endpoint sent
`label`), so a screen reading a key the backend never sends stayed green. They are now built by
`tests/integration/address_row_builders.address_row` through the producer's own serializer, and both fake backends
refuse any row without exactly `ADDRESS_KEYS` (tests/telegram_bot/test_fake_backend_address_rows.py,
tests/staff_bot/test_operator_world_address_rows.py). This test holds those keys to the live endpoints, for an
address with a title and for one without: every row the operator endpoints answer has exactly `ADDRESS_KEYS`; the
customer's own list (`GET /api/v1/auth/addresses`) answers `CUSTOMER_LIST_KEYS`, which is `ADDRESS_KEYS` plus
`pin_move_refusal`, and the customer-bot fake serves that list through `customer_list_row`. The builder refuses the
old vocabulary outright.

The duplicate-address 409 is held the same way: both bots' fakes serve `possible_duplicate_body`, and the real
customer create and operator create must answer its envelope (`error_code`, `details.move_address_id`, each
candidate's keys and its whole-metre `distance_m`).
"""

import pytest

from business_app.models.user import UserAddress
from tests.integration.address_guard_geometry import at
from tests.integration.address_row_builders import (
    ADDRESS_KEYS,
    CUSTOMER_LIST_KEYS,
    address_row,
    customer_list_row,
    possible_duplicate_body,
)

pytestmark = pytest.mark.contract

CUSTOMER_ADDRESSES = "/api/v1/auth/addresses"  # telegram_bot/api_client.get_user_addresses
OPERATOR_ADDRESSES = "/api/v1/staff/operator/users/{}/addresses"  # staff_bot/api_client get/add


@pytest.fixture
def saved(db, sample_user):
    rows = [
        UserAddress(user_id=sample_user.id, title="Uy", full_address="Chilonzor 9-kvartal, 14-uy", city="Toshkent"),
        UserAddress(user_id=sample_user.id, title=None, full_address="Amir Temur ko'chasi 108", city="Toshkent"),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def _fake_keys():
    """The keys the fake backends enforce on every row they hold."""
    assert set(address_row(1, title="Uy", full_address="Chilonzor 9-kvartal, 14-uy")) == ADDRESS_KEYS
    return set(ADDRESS_KEYS)


def test_the_customer_bot_fake_rows_have_exactly_the_keys_of_the_customer_address_list(
    client, auth_headers, saved
):
    response = client.get(CUSTOMER_ADDRESSES, headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["addresses"]
    assert len(rows) == 2
    # The customer's list adds the pin rule that would refuse a move of each row (`pin_move_refusal`).
    assert set(customer_list_row(address_row(1, full_address=""))) == CUSTOMER_LIST_KEYS
    assert [set(row) for row in rows] == [CUSTOMER_LIST_KEYS, CUSTOMER_LIST_KEYS]


def test_the_operator_fake_rows_have_exactly_the_keys_of_the_operator_address_list_and_add(
    client, operator_auth_headers, sample_user, saved
):
    endpoint = OPERATOR_ADDRESSES.format(sample_user.id)
    listed = client.get(endpoint, headers=operator_auth_headers)
    added = client.post(
        endpoint,
        json={"title": "Ofis", "full_address": "Amir Temur ko'chasi 108, 3-qavat", "district": None,
              "delivery_notes": None, "latitude": 41.3111, "longitude": 69.2797},
        headers=operator_auth_headers,
    )

    assert listed.status_code == 200, listed.get_data(as_text=True)
    assert added.status_code == 201, added.get_data(as_text=True)
    rows = listed.get_json()["data"]["items"]
    assert len(rows) == 2
    assert [set(row) for row in rows] == [_fake_keys(), _fake_keys()]
    assert set(added.get_json()["data"]) == _fake_keys()


def test_the_published_address_definition_documents_exactly_the_address_payload(app):
    """The Swagger `Address` definition (`$ref`d by `Order.delivery_address`) still published the removed raw-SQL
    table's `address_line1`/`address_line2`; consumers written from it read keys no endpoint sends."""
    from business_app.utils.swagger_config import get_swagger_template

    assert set(get_swagger_template()["definitions"]["Address"]["properties"]) == _fake_keys()


@pytest.mark.parametrize("legacy_key", ["address_line1", "address_line2", "label", "delivery_notes"])
def test_a_fake_row_cannot_hold_a_key_no_address_endpoint_sends(app, legacy_key):
    """The vocabulary of the raw-SQL `addresses` table removed in 2025-08 (and the staff-only `label`) is what the
    broken readers were written against; the builder refuses it, so a fake can never be written in it again."""
    with pytest.raises(TypeError):
        address_row(1, title="Uy", full_address="Chilonzor 9", **{legacy_key: "x"})


def _envelope(body: dict) -> dict:
    """What the bots read off a duplicate 409: the top-level `error_code`, the keys of `details`, the type of
    `move_address_id`, and each candidate's keys with the type of its `distance_m`."""
    details = body.get("details") or {}
    return {
        "error_code": body.get("error_code"),
        "details": set(details),
        "move_address_id": type(details.get("move_address_id")),
        "candidates": [(set(row), type(row.get("distance_m"))) for row in details.get("candidates") or []],
    }


def _fake_409(*, move_address_id):
    """The bots' fake for the same answer: one saved address 14 m away."""
    return possible_duplicate_body(
        [address_row(1, title="Uy", full_address="1 Test St, Tashkent") | {"distance_m": 14}],
        move_address_id=move_address_id,
    )


def test_the_bots_409_fake_has_the_envelope_of_the_customer_create(client, auth_headers, user_address):
    """The customer bot's pin create (telegram_bot `_build_address_payload` plus `point_source`) 14 m from a saved
    pinned address: the 409 offers Move, so `move_address_id` is that address's id."""
    near = at(14)
    response = client.post(
        CUSTOMER_ADDRESSES,
        json={"title": "Uy", "full_address": "15, Chilonzor dahasi", "city": "Toshkent",
              "latitude": near[0], "longitude": near[1], "point_source": "map_pin"},
        headers=auth_headers,
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    body = response.get_json()
    assert _envelope(body) == _envelope(_fake_409(move_address_id=63))
    assert body["details"]["move_address_id"] == user_address.id


def test_the_bots_409_fake_has_the_envelope_of_the_operator_create(
    client, operator_auth_headers, sample_user, user_address
):
    """The operator journeys' add-address body (staff_bot `confirm_address`) 14 m from the customer's saved address:
    an operator is never offered Move."""
    near = at(14)
    response = client.post(
        OPERATOR_ADDRESSES.format(sample_user.id),
        json={"title": "Uy 2", "full_address": "Chilonzor 9-kvartal, 14-uy, 2-kirish", "district": None,
              "delivery_notes": None, "latitude": near[0], "longitude": near[1]},
        headers=operator_auth_headers,
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    assert _envelope(response.get_json()) == _envelope(_fake_409(move_address_id=None))


@pytest.mark.parametrize("renamed", [{"distance": 14}, {}], ids=["distance-renamed", "distance-missing"])
def test_the_409_fake_refuses_a_candidate_the_backend_never_sends(app, renamed):
    with pytest.raises(AssertionError):
        possible_duplicate_body([address_row(1, title="Uy", full_address="Chilonzor 9") | renamed])
