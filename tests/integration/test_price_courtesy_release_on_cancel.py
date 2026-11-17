"""The courtesy through an order's later life (plan 2026-11-16, Part C, Task 28; spec §6).

The person's old-price entry is claimed by the order that used it. Every road by which that order ends gives it
back: each cancellation reaches `update_order_status(..., CANCELLED)`, and a hard delete releases it before the
order's rows are swept (else the delete would erase the person's entry). A failed delivery never cancels, so the
order keeps its prices and its entry; a payment-method change keeps the courtesy line's net total.

Every test drives the route the real client calls with the body it sends: the bot's order body to
`POST /api/v1/orders/`, the customer's cancel POST, the Orders page's status and payment-method calls, the staff
bot's failed-delivery PUT. Faked: the Celery publishes and the Asl belgisi pre-utilisation (external I/O).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.price_change import PriceCourtesy
from business_app.services.order_deletion_service import OrderDeletionService
from business_app.tasks import notification_tasks, order_tasks
from business_app.tasks.order_tasks import cancel_abandoned_orders, send_payment_expired_notice_task
from business_app.utils.exceptions import TaxCommitteeUnavailableError
from business_app.utils.timezone_utils import get_utc_now
from shared.business_config import CLICK_PAYMENT_WINDOW_HOURS
from shared.enums import DeliveryStatus, EntitySubtype, OrderStatus, UserType
from tests.integration.contract_pricing_factory import make_contract
from tests.integration.price_courtesy_builders import (
    COURTESY_TOTAL,
    courtesy_world,
    entry_of,
    water,
)
from tests.integration.test_price_courtesy_order_create import (
    COURTESY_LINE,
    NEW_PRICE_LINE,
    PRE_UTILISE,
    _money,
    _only_line,
    _order,
    _place,
)
from tests.integration.test_staff_delivery_ownership_guard import _bearer, _driver

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/orders/"
CANCEL = "/api/v1/orders/{}/cancel"
ADMIN_STATUS = "/api/v1/admin/orders/{}/status"
RETRY_WITH_CASH = "/api/v1/orders/{}/retry-with-cash"
DRIVER_STATUS = "/api/v1/staff/delivery/{}/status"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def w(app, db, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


@pytest.fixture(autouse=True)
def publishes(monkeypatch):
    """The Celery publishes a cancellation makes (customer notices), recorded with the tasks' real signatures."""
    seen = []

    def expired(order_id):
        seen.append(("payment_expired", order_id))

    def status_notice(*args, **kwargs):
        seen.append(("status", args, kwargs))

    with patch.object(send_payment_expired_notice_task, "delay", side_effect=expired), patch.object(
        notification_tasks.send_order_notification_task, "delay", side_effect=status_notice
    ):
        yield seen


def _used_by(db, w):
    db.session.expire_all()
    return entry_of(db, w.customer.user).order_id


def _click(client, db, w):
    with patch(PRE_UTILISE, autospec=True, return_value={"utilised_at": None, "fast_path": True}):
        return _order(db, _place(client, w.customer, w.product, payment_method="click"))


def test_a_customer_cancel_gives_the_courtesy_back(client, db, w):
    order = _order(db, _place(client, w.customer, w.product))
    assert _used_by(db, w) == order.id

    response = client.post(CANCEL.format(order.id), json={"expected_via": "cancel"}, headers=w.customer.headers)

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    entry = entry_of(db, w.customer.user)
    assert (db.session.get(Order, order.id).status, entry.order_id, entry.used_at) == (OrderStatus.CANCELLED, None, None)
    again = _order(db, _place(client, w.customer, w.product))
    assert _money(_only_line(again)) == COURTESY_LINE
    assert _used_by(db, w) == again.id


def test_an_admin_cancel_gives_the_courtesy_back(client, db, w, admin_claim_headers):
    order = _order(db, _place(client, w.customer, w.product))

    response = client.put(
        ADMIN_STATUS.format(order.id),
        json={"status": "cancelled", "reason": "Customer asked to cancel"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert _used_by(db, w) is None
    again = _order(db, _place(client, w.customer, w.product))
    assert _money(_only_line(again)) == COURTESY_LINE


def test_a_tax_service_cancel_then_the_cash_rescue_claims_it_again(client, db, w):
    with patch(PRE_UTILISE, autospec=True, side_effect=TaxCommitteeUnavailableError("down")):
        outage = client.post(
            ORDERS,
            json={
                "items": [{"product_id": w.product.id, "quantity": 4}],
                "delivery_address_id": w.customer.address.id,
                "payment_method": "click",
                "source": "telegram",
            },
            headers=w.customer.headers,
        )
    assert outage.status_code == 503, outage.get_json()
    cancelled_id = outage.get_json()["data"]["cancelled_order_id"]
    db.session.expire_all()
    assert db.session.get(Order, cancelled_id).status == OrderStatus.CANCELLED
    assert _used_by(db, w) is None

    rescue = client.post(RETRY_WITH_CASH.format(cancelled_id), headers=w.customer.headers)

    assert rescue.status_code == 201, rescue.get_json()
    rescued = db.session.get(Order, rescue.get_json()["data"]["order"]["id"])
    line = _only_line(rescued)
    assert (line.courtesy_quantity, rescued.total_amount) == (2, COURTESY_TOTAL)
    assert _used_by(db, w) == rescued.id


def test_the_unpaid_order_sweep_gives_the_courtesy_back(client, db, w, monkeypatch):
    order = _click(client, db, w)
    assert _used_by(db, w) == order.id
    later = get_utc_now() + timedelta(hours=CLICK_PAYMENT_WINDOW_HOURS + 1)
    monkeypatch.setattr(order_tasks, "get_utc_now", lambda: later)

    cancel_abandoned_orders()

    db.session.expire_all()
    assert db.session.get(Order, order.id).status == OrderStatus.CANCELLED
    assert _used_by(db, w) is None


def test_cancelling_an_order_that_did_not_use_the_courtesy_leaves_it_used(client, db, w):
    first = _order(db, _place(client, w.customer, w.product))
    # The entry is spent by `first`; a second order placed now takes the plain new price.
    second = _order(db, _place(client, w.customer, w.product))
    assert _money(_only_line(second)) == NEW_PRICE_LINE

    response = client.post(CANCEL.format(second.id), json={"expected_via": "cancel"}, headers=w.customer.headers)

    assert response.status_code == 200, response.get_json()
    assert _used_by(db, w) == first.id


def test_hard_deleting_a_courtesy_order_gives_the_courtesy_back(client, db, w):
    order = _order(db, _place(client, w.customer, w.product))
    number, order_id = order.order_number, order.id
    assert _used_by(db, w) == order_id

    result = OrderDeletionService().delete_order_by_number(number, apply_changes=True)

    assert result["applied"] is True
    db.session.expire_all()
    assert db.session.get(Order, order_id) is None
    entry = entry_of(db, w.customer.user)
    assert entry is not None
    assert (entry.order_id, entry.used_at) == (None, None)
    assert PriceCourtesy.query.filter_by(user_id=w.customer.user.id).count() == 1


def _carried_by(db, order, driver, *, status, order_status):
    """The order on `driver`'s route. Placing it may already have written its Delivery row (one per order), so
    that row is taken over rather than a second one added."""
    order.status = order_status
    delivery = Delivery.query.filter_by(order_id=order.id).first()
    if delivery is None:
        delivery = Delivery(order_id=order.id, scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="09:00-12:00")
        db.session.add(delivery)
    delivery.delivery_person_id = driver.id
    delivery.status = status
    db.session.commit()
    return delivery


def test_a_failed_delivery_keeps_the_courtesy_and_the_prices(app, client, db, w):
    """(Review Focus 4) The order waits for a new date; it is not cancelled and its entry stays used."""
    order = _order(db, _place(client, w.customer, w.product))
    driver = _driver(db, phone="+998901520001", first_name="Oybek")
    delivery = _carried_by(db, order, driver, status=DeliveryStatus.ASSIGNED, order_status=OrderStatus.CONFIRMED)
    delivery_id = delivery.id
    picked_up = client.put(DRIVER_STATUS.format(delivery_id), json={"status": "picked_up"}, headers=_bearer(app, driver))
    assert picked_up.status_code == 200, picked_up.get_data(as_text=True)

    with patch("business_app.tasks.staff_tasks.notify_delivery_failed.delay"), patch(
        "business_app.tasks.notification_tasks.send_delivery_update_task.delay"
    ):
        failed = client.put(
            DRIVER_STATUS.format(delivery_id),
            json={"status": "failed", "metadata": {"fail_reason": "customer_unavailable"}},
            headers=_bearer(app, driver),
        )

    assert failed.status_code == 200, failed.get_data(as_text=True)
    db.session.expire_all()
    kept = db.session.get(Order, order.id)
    assert kept.status != OrderStatus.CANCELLED
    assert _used_by(db, w) == order.id
    assert _money(_only_line(kept)) == COURTESY_LINE


def test_settling_an_unpaid_click_courtesy_order_in_cash_keeps_its_lines(app, client, db, w):
    """(Review Focus 3) The rail change that reaches `reprice_order_for_rail` for a customer order: the driver
    collects cash at the door for an unpaid Click order (`PUT /staff/delivery/<id>/status`, the body the staff bot
    sends). The admin route `POST /admin/orders/<id>/payment-method` offers no cash -> click move
    (`ALLOWED_TRANSITIONS`), so this is the one rail change a courtesy order can make."""
    order = _click(client, db, w)
    driver = _driver(db, phone="+998901520002", first_name="Jasur")
    delivery = _carried_by(db, order, driver, status=DeliveryStatus.ARRIVED, order_status=OrderStatus.OUT_FOR_DELIVERY)

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        response = client.put(
            DRIVER_STATUS.format(delivery.id),
            json={"status": "delivered", "metadata": {"cash_collected": str(COURTESY_TOTAL)}},
            headers=_bearer(app, driver),
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    settled = db.session.get(Order, order.id)
    assert settled.payment_method.value == "cash"
    assert settled.total_amount == COURTESY_TOTAL
    assert _money(_only_line(settled)) == COURTESY_LINE
    assert _used_by(db, w) == order.id


def test_a_rail_change_never_moves_a_courtesy_line_onto_a_contract_price(app, client, db, w):
    """(Final review CROSS-2) A contract never prices a courtesy line. The account became a business after its Click
    order, with a contract in force since before the order (cash 15,000 a bottle); the driver then collects cash at the
    door, and `reprice_order_for_rail` re-prices the order onto cash. The courtesy line keeps the price it was charged
    and its split: a contract price written beside the old courtesy pair made every read of the order answer 500."""
    order = _click(client, db, w)
    user = w.customer.user
    user.user_type, user.entity_subtype, user.company_name = UserType.ENTITY, EntitySubtype.WORKPLACE, "Courtesy Co"
    make_contract(db, user, w.product, cash=Decimal("15000.00"))  # in force since yesterday: before the order
    driver = _driver(db, phone="+998901520003", first_name="Bekzod")
    delivery = _carried_by(db, order, driver, status=DeliveryStatus.ARRIVED, order_status=OrderStatus.OUT_FOR_DELIVERY)

    with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay"), patch(
        "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
    ):
        response = client.put(
            DRIVER_STATUS.format(delivery.id),
            json={"status": "delivered", "metadata": {"cash_collected": str(COURTESY_TOTAL)}},
            headers=_bearer(app, driver),
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    settled = db.session.get(Order, order.id)
    assert settled.payment_method.value == "cash"
    assert _money(_only_line(settled)) == COURTESY_LINE
    assert settled.total_amount == COURTESY_TOTAL
    read = client.get(f"/api/v1/orders/{order.id}", headers=w.customer.headers)
    assert read.status_code == 200, read.get_data(as_text=True)
    (published,) = read.get_json()["data"]["order"]["order_items"]
    assert (published["courtesy_quantity"], published["regular_quantity"], published["total_price"]) == (2, 2, 76000.0)
