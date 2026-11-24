"""A first-timer's AquaCoins are worked out without creating his loyalty account.

`LoyaltyService.calculate_points_for_purchase` reads the account a customer with none would get on first use, and
writes nothing (it used to create that account inside its caller's transaction). The figures must not move: on every
path that asks for them, the office's edit preview and the award when an order is delivered and paid, a customer with
no account is quoted and awarded exactly what a customer whose fresh account already exists is. Each test runs for both
and checks the same literal. The program and its 0-point tier are seeded here (200 so'm a coin, x1.5), so a figure
worked out with the wrong program or tier differs.

Driven through the admin routes with the bodies Orders.js sends and an admin role-claim token.
"""

from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig
from business_app.models.order import Order
from business_app.services.loyalty_service import LoyaltyService
from shared.enums import OrderStatus, PaymentMethod
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401  (fixture)
from tests.integration.trip_unit_builders import order_with_lines, small, strict  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

ORDER_STATUS = "/api/v1/admin/orders/{}/status"
EDIT_PREVIEW = "/api/v1/admin/orders/{}/edit-preview"


@pytest.fixture
def program(db):
    program = LoyaltyProgram(name="AquaCoins", is_active=True, is_default=True, uzs_per_point=200)
    db.session.add(program)
    db.session.flush()
    db.session.add(
        LoyaltyTierConfig(
            program_id=program.id, name="Starter", display_order=0, min_points=0, points_multiplier=1.5, is_active=True
        )
    )
    db.session.commit()
    return program


def _accounts(db, user_id):
    db.session.rollback()
    return LoyaltyPoints.query.filter_by(user_id=user_id).all()


@pytest.mark.parametrize("account", ["none", "fresh"])
def test_the_edit_preview_quotes_a_first_timer_what_his_fresh_account_earns(
    client, db, admin_claim_headers, program, small, sample_user, account  # noqa: F811
):
    """2 x 10 L (9 000) becomes 3: 27 000 so'm, 135 coins, x1.5 = 202. The preview creates no account."""
    if account == "fresh":
        LoyaltyService().get_or_create_loyalty_account(sample_user.id)
    order = order_with_lines(db, sample_user, [(small, 2)], status=OrderStatus.CONFIRMED)
    body = {
        "items": [{"orderItemId": order.order_items[0].id, "productId": small.id, "quantity": 3}],
        "reason": "Customer wants one more",
    }

    response = client.post(EDIT_PREVIEW.format(order.id), json=body, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["cascade_summary"]["loyalty"]["new_points_earned"] == 202
    assert len(_accounts(db, sample_user.id)) == (0 if account == "none" else 1)


@pytest.mark.parametrize("account", ["none", "fresh"])
def test_the_award_for_a_first_timer_is_what_his_fresh_account_earns(
    app, client, db, monkeypatch, enqueued, admin_claim_headers, program, small, sample_user, account  # noqa: F811
):
    """A card order of 2 x 10 L (18 000 so'm), paid online, marked Delivered on the Orders page: 90 coins, x1.5 = 135,
    booked on his account (the award creates it for a first-timer)."""
    strict(app, monkeypatch, False)
    if account == "fresh":
        LoyaltyService().get_or_create_loyalty_account(sample_user.id)
    order = order_with_lines(
        db, sample_user, [(small, 2)], status=OrderStatus.OUT_FOR_DELIVERY, payment_method=PaymentMethod.CARD
    )
    order.is_paid = True
    db.session.commit()

    response = client.put(
        ORDER_STATUS.format(order.id), json={"status": "delivered", "notes": ""}, headers=admin_claim_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    (booked,) = _accounts(db, sample_user.id)
    assert (booked.program_id, booked.current_balance) == (program.id, 135)
    assert db.session.get(Order, order.id).loyalty_points_earned == 135
    assert Decimal(db.session.get(Order, order.id).subtotal) == Decimal("18000.00")
