"""An audit trail records only money that is true (price-change transition, Task Y3).

The stock check inside order creation used to audit the catalogue subtotal (80,000) before the courtesy lowered the
basket to the charged 76,000. The only money figures in an order's audit rows are now the charged ones, on the
order's own created event. The test drives the bot's `POST /api/v1/orders/` and reads the rows it wrote.
"""

import pytest

from business_app.models.audit import AuditLog, AuditEventType
from business_app.models.order import Order
from tests.integration.price_courtesy_builders import COURTESY_TOTAL, courtesy_world, place_by_bot, water

pytestmark = pytest.mark.integration

CATALOGUE_SUBTOTAL = 80000.0  # 4 bottles at the old price: what nobody was charged


def test_a_courtesy_orders_audit_rows_carry_only_the_charged_money(app, db, sample_product, admin_user):
    w = courtesy_world(app, db, water(db, sample_product), admin_user)

    response = place_by_bot(app, w)

    order = db.session.get(Order, response.get_json()["data"]["order"]["id"])
    assert order.total_amount == COURTESY_TOTAL
    rows = AuditLog.query.filter_by(event_type=AuditEventType.ORDER_CREATED).all()
    stock_rows = [r for r in rows if r.action == "order_inventory_validated"]
    assert len(stock_rows) == 1
    assert set(stock_rows[0].additional_data) == {"items_count", "total_quantity"}
    assert stock_rows[0].additional_data["total_quantity"] == 4
    for row in rows:
        assert CATALOGUE_SUBTOTAL not in (row.additional_data or {}).values()
    created = [r for r in rows if r.action == "inventory_reserved_for_order" and r.resource_id == str(order.id)]
    assert len(created) == 1
    assert created[0].additional_data["subtotal"] == float(COURTESY_TOTAL)
    assert created[0].additional_data["total_amount"] == float(COURTESY_TOTAL)
