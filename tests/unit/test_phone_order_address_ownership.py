"""Plan E Task 7 / owner ruling A5 — a phone order's delivery address must
belong to the client it is created for.

Five of the six order-creation paths already re-check this; StaffService's did
not, so a direct API call could attach ANOTHER user's address to an order. That
is the exact state that would make a place-scoped collection degrade silently
(plan Q5), so closing it makes the state impossible by construction.
"""

import pytest

from business_app.models.user import UserAddress
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ValidationError

# 🔴 THERE IS NO `operator_user` FIXTURE. Verified 2026-08-04: tests/conftest.py
# has sample_user (:293), second_sample_user (:331), user_address (:312),
# place (:359), sample_product (:458) — and nothing named operator_user. Every
# existing phone-order test builds its operator itself. Reuse the shared helper
# the COD-cap file already uses rather than adding a fixture:
from tests.unit._scope_money_helpers import make_user


@pytest.mark.unit
def test_phone_order_rejects_an_address_owned_by_someone_else(
    app, db, sample_user, second_sample_user, sample_product
):
    """THE REGRESSION. `second_sample_user`'s address must not attach to
    `sample_user`'s order."""
    operator = make_user(db)
    other_address = UserAddress(
        user_id=second_sample_user.id,
        full_address="9 Elsewhere St, Tashkent",
        street_address="9 Elsewhere St",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    db.session.add(other_address)
    db.session.commit()

    with pytest.raises(ValidationError) as exc:
        StaffService.create_phone_order(
            operator_id=operator.id,
            client_id=sample_user.id,
            order_data={
                "items": [{"product_id": sample_product.id, "quantity": 1}],
                "delivery_address_id": other_address.id,
                "payment_method": "cash",
            },
        )
    assert exc.value.error_code == "STAFF_INVALID_DELIVERY_ADDRESS"


@pytest.mark.unit
def test_phone_order_accepts_the_clients_own_address(
    app, db, sample_user, user_address, sample_product
):
    """The guard must not break the operator's normal flow.

    `user_address` (tests/conftest.py:312) is owned by `sample_user` (:317) and
    is ungrouped, so no place arm fires — exactly the majority case.
    """
    operator = make_user(db)
    order = StaffService.create_phone_order(
        operator_id=operator.id,
        client_id=sample_user.id,
        order_data={
            "items": [{"product_id": sample_product.id, "quantity": 1}],
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
        },
    )
    assert order.delivery_address_id == user_address.id
