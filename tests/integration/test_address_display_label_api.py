"""One address payload and one display rule, published by the API (address-label contract, 2026-11-17).

Every endpoint that lists a customer's saved addresses answers `UserAddress.to_dict()`, and that payload carries
`display_label`: the address as the customer names it (its title), else its street line. The bots and the web print
it instead of re-deriving it. Driven through the routes the clients call, with the bodies they send:

- `GET /api/v1/auth/addresses`, the customer bot's and the website's list;
- `GET` / `POST /api/v1/staff/operator/users/<id>/addresses`, the operator's list and add (the staff bot reads
  `title`; the endpoint used to rename it to `label`, so every operator screen lost the name);
- `GET /api/v1/delivery/<id>/live-tracking`, which read the never-existing `address_line1` and answered 500;
- `POST /api/v1/admin/tryouts/<id>/convert`, which handed the delivery notes over under a key the address update
  ignores.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.tryout import ProductTryout
from business_app.models.user import UserAddress
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

pytestmark = [pytest.mark.integration, pytest.mark.api]

CUSTOMER_ADDRESSES = "/api/v1/auth/addresses"
OPERATOR_ADDRESSES = "/api/v1/staff/operator/users/{}/addresses"
LIVE_TRACKING = "/api/v1/delivery/{}/live-tracking"
ADMIN_TRYOUTS = "/api/v1/admin/tryouts"

UY_STREET = "Chilonzor 9-kvartal, 14-uy, Toshkent"
UNTITLED_STREET = "Amir Temur ko'chasi 108, Toshkent"
IN_ZONE_PIN = (41.3111, 69.2797)


def _saved(db, user, title, full_address, **columns):
    address = UserAddress(user_id=user.id, title=title, full_address=full_address, city="Toshkent", **columns)
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def two_addresses(db, sample_user):
    """One address the customer named ("Uy") and one saved without a title (the admin form allows it)."""
    return _saved(db, sample_user, "Uy", UY_STREET), _saved(db, sample_user, None, UNTITLED_STREET)


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_the_customer_list_names_each_address_by_its_title_else_its_street_line(
    client, auth_headers, two_addresses
):
    titled, untitled = two_addresses

    response = client.get(CUSTOMER_ADDRESSES, headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    rows = _by_id(response.get_json()["data"]["addresses"])
    assert (rows[titled.id]["title"], rows[titled.id]["display_label"]) == ("Uy", "Uy")
    assert (rows[untitled.id]["title"], rows[untitled.id]["display_label"]) == (None, UNTITLED_STREET)


def test_the_operator_list_sends_the_title_and_display_label_and_no_label(
    client, operator_auth_headers, sample_user, two_addresses
):
    titled, untitled = two_addresses

    response = client.get(OPERATOR_ADDRESSES.format(sample_user.id), headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]
    assert body["total"] == 2
    rows = _by_id(body["items"])
    assert (rows[titled.id]["title"], rows[titled.id]["display_label"]) == ("Uy", "Uy")
    assert (rows[untitled.id]["title"], rows[untitled.id]["display_label"]) == (None, UNTITLED_STREET)
    assert rows[titled.id]["full_address"] == UY_STREET
    assert all("label" not in row for row in body["items"]), "`label` is the old staff-only rename of `title`"


def test_an_address_the_operator_saves_comes_back_under_the_name_they_typed(
    client, operator_auth_headers, sample_user
):
    # The body the staff bot sends from its add-address flow (manage_address.confirm_address).
    body = {
        "title": "Ofis",
        "full_address": "Amir Temur ko'chasi 108, 3-qavat",
        "district": "Yunusobod",
        "delivery_notes": "Qo'ng'iroq ishlamaydi",
        "latitude": IN_ZONE_PIN[0],
        "longitude": IN_ZONE_PIN[1],
    }

    created = client.post(OPERATOR_ADDRESSES.format(sample_user.id), json=body, headers=operator_auth_headers)

    assert created.status_code == 201, created.get_data(as_text=True)
    saved = created.get_json()["data"]
    assert (saved["title"], saved["display_label"], saved["full_address"]) == (
        "Ofis", "Ofis", "Amir Temur ko'chasi 108, 3-qavat",
    )
    assert saved["delivery_instructions"] == "Qo'ng'iroq ishlamaydi"
    assert "label" not in saved

    listed = client.get(OPERATOR_ADDRESSES.format(sample_user.id), headers=operator_auth_headers)
    [row] = listed.get_json()["data"]["items"]
    assert (row["id"], row["title"], row["display_label"]) == (saved["id"], "Ofis", "Ofis")


def test_live_tracking_names_the_delivery_street_line(client, db, auth_headers, sample_user):
    address = _saved(db, sample_user, "Uy", UY_STREET, latitude=IN_ZONE_PIN[0], longitude=IN_ZONE_PIN[1])
    order = Order(
        user_id=sample_user.id,
        order_number="ORD-LIVE-0001",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("45000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("45000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=get_utc_now() + timedelta(hours=2),
        scheduled_time_slot="09:00-11:00",
    )
    db.session.add(delivery)
    db.session.commit()

    response = client.get(LIVE_TRACKING.format(delivery.id), headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["tracking"]["delivery_address"] == {
        "lat": IN_ZONE_PIN[0],
        "lng": IN_ZONE_PIN[1],
        "address": UY_STREET,
    }


def test_converting_a_tryout_onto_an_existing_address_keeps_its_delivery_notes(
    client, db, admin_claim_headers, sample_user, sample_product
):
    """The admin converts a try-out whose contact is an existing customer, at an address that customer already
    saved: the conversion updates that row, and the try-out's door notes must land on it."""
    existing = _saved(db, sample_user, "Uy", UY_STREET)
    # The body the admin Try-outs form sends (admin_ui/src/pages/Tryouts.js normalizeTryoutFormValues).
    payload = {
        "trial_contact": {
            "first_name": "Test",
            "last_name": "User",
            "phone": sample_user.phone,
            "preferred_language": "uz",
        },
        "address": {
            "label": "Try-out",
            "full_address": UY_STREET,
            "city": "Tashkent",
            "latitude": None,
            "longitude": None,
            "delivery_notes": "Domofon 14, ikkinchi eshik",
            "is_default": True,
        },
        "items": [{"product_id": sample_product.id, "quantity": 1}],
        "complete_handoff": False,
    }
    created = client.post(ADMIN_TRYOUTS, json=payload, headers=admin_claim_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    tryout_id = created.get_json()["data"]["tryout"]["id"]

    converted = client.post(f"{ADMIN_TRYOUTS}/{tryout_id}/convert", json={}, headers=admin_claim_headers)

    assert converted.status_code == 200, converted.get_data(as_text=True)
    assert converted.get_json()["data"]["conversion"]["action"] == "linked_existing_user"
    assert db.session.get(ProductTryout, tryout_id).converted_user_id == sample_user.id
    db.session.expire_all()
    [address] = UserAddress.query.filter_by(user_id=sample_user.id).all()
    assert address.id == existing.id
    assert address.delivery_instructions == "Domofon 14, ikkinchi eshik"


@pytest.mark.parametrize(
    "notes_field",
    [
        pytest.param({}, id="notes-left-empty"),  # the form drops an untouched field (undefined) from the JSON body
        pytest.param({"delivery_notes": ""}, id="notes-typed-then-cleared"),
    ],
)
def test_a_tryout_without_notes_keeps_the_door_notes_the_customer_already_saved(
    client, db, admin_claim_headers, sample_user, sample_product, notes_field
):
    """A try-out that carries no notes has nothing to hand over: converting it onto the customer's existing address
    must leave that address's own door notes (the entry code the driver needs) as they were."""
    existing = _saved(db, sample_user, "Uy", UY_STREET, delivery_instructions="Kod 1234, 3-qavat")
    payload = {
        "trial_contact": {
            "first_name": "Test",
            "last_name": "User",
            "phone": sample_user.phone,
            "preferred_language": "uz",
        },
        "address": {
            "label": "Try-out",
            "full_address": UY_STREET,
            "city": "Tashkent",
            "latitude": None,
            "longitude": None,
            "is_default": True,
            **notes_field,
        },
        "items": [{"product_id": sample_product.id, "quantity": 1}],
        "complete_handoff": False,
    }
    created = client.post(ADMIN_TRYOUTS, json=payload, headers=admin_claim_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    tryout_id = created.get_json()["data"]["tryout"]["id"]

    converted = client.post(f"{ADMIN_TRYOUTS}/{tryout_id}/convert", json={}, headers=admin_claim_headers)

    assert converted.status_code == 200, converted.get_data(as_text=True)
    assert converted.get_json()["data"]["conversion"]["action"] == "linked_existing_user"
    db.session.expire_all()
    [address] = UserAddress.query.filter_by(user_id=sample_user.id).all()
    assert address.id == existing.id
    assert address.delivery_instructions == "Kod 1234, 3-qavat"
