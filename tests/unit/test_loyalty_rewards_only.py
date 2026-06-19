"""Unit F — loyalty is rewards-only (owner decision 2026-06-14).

Loyalty points are NEVER converted to a UZS order discount. Order creation,
cart pricing, and Order.calculate_total already enforce this; the order-edit
total projection must match (it previously applied a stale 1 point = 100 UZS
discount, disagreeing with the persisted total).
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.services.order_edit_service import OrderEditService


@pytest.mark.unit
def test_order_edit_projection_is_rewards_only(app):
    with app.app_context():
        svc = OrderEditService()
        # Order carries a (legacy) non-zero loyalty_points_used; it must NOT
        # produce any UZS discount in the projection.
        order = SimpleNamespace(
            discount_amount=Decimal("0"),
            delivery_fee=Decimal("500"),
            loyalty_points_used=5,
        )
        changes = [SimpleNamespace(direction="add", unit_price=Decimal("1000"), new_quantity=3)]

        totals = svc._project_totals_after(order, changes)

        assert totals["loyalty_discount"] == 0.0
        assert totals["loyalty_points_refunded"] == 0
        # subtotal 3000 - discount 0 + delivery 500, with NO points discount
        assert totals["total_amount"] == 3500.0
