"""One guard per subscription action, published as `allowed_actions` (plan 2026-10-26 Task 1; spec
2026-10-26 §5 B1, B8, B9, §8.1-8.2, §12).

The website, the bot and admin each used to re-derive "may I pause, skip, cancel or edit this?" from the
status, and the bot offered Edit and Cancel on a cancelled subscription. Now ONE function per action in
`SubscriptionService` decides: it publishes `allowed_actions` and it guards every customer and admin write.
The core test below is the published answer against the real write, for every status x action x actor.

Also here: the stale-skip 409 (Review Focus 1), the schedule guards on PUT, a schedule change never bringing
back a skipped date, the payment-method guard (the menu IS the write guard), and the admin bulk actions going
through the same rules.

Driven through the endpoints the clients call, with the bodies they send: the storefront's cookie JWT and
`X-CSRF-TOKEN` exactly as base.js `apiRequest` sends them, the admin UI's role-claim bearer token. Only
external I/O is mocked: the Celery publish, the route notifications and the Click link.
"""

import json
import re
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from dateutil.relativedelta import relativedelta
from flask_jwt_extended import create_access_token, get_csrf_token

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.translation import Translation
from business_app.services import customer_order_actions, subscription_schedule
from business_app.services.subscription_service import SubscriptionService
from business_app.tasks.subscription_tasks import create_subscription_delivery_task, process_daily_subscription_billing
from business_app.utils import payment_projection
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, PaymentStatus, SubscriptionFrequency, SubscriptionStatus
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.sales_pay_builders import freeze_local_now
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user

pytestmark = pytest.mark.integration

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
BASE = "/api/v1/subscriptions"
ADMIN_BASE = "/api/v1/admin/subscriptions"
THURSDAY, SATURDAY = 3, 5
WEEK = timedelta(days=7)

CUSTOMER_ACTIONS = ("pay", "skip", "pause", "resume", "cancel", "edit", "remove_item")
ADMIN_ACTIONS = ("pause", "resume", "cancel", "edit", "remove_item", "create_next_order")
# The admin drawer's item editors (admin_ui Subscriptions.js) follow the published `edit` answer, so their routes
# are "edit" too: each must refuse exactly when `edit` is refused, in its words (re-review G11, concern 12).
ADMIN_ITEM_ROUTES = {"add_item": "edit", "update_item": "edit"}

# The seeded copy every refusal on these paths answers with.
GUARD_KEY_PREFIXES = (
    "api.subscriptions.action.",
    "api.subscriptions.actions.",
    "api.subscriptions.schedule.",
    "api.subscriptions.time.",
    "api.subscriptions.page.",
    "web.display_labels.",
    "api.subscriptions.payment.",
    "api.subscriptions.address.",
    "api.subscriptions.skip_reason.",
    "api.subscriptions.error.",
    "api.common.",
)
NOT_PAUSED = "api.subscriptions.action.resume.not_paused"
PAUSE_NOT_ACTIVE = "api.subscriptions.action.pause.not_active"
SKIP_NOT_ACTIVE = "api.subscriptions.action.skip.not_active"
ALREADY_CANCELLED = "api.subscriptions.action.cancel.already_cancelled"
CLOSED = "api.subscriptions.action.edit.closed"
LAST_ITEM = "api.subscriptions.action.remove_item.last_item"
NOTHING_TO_PAY = "api.subscriptions.action.pay.nothing_to_pay"
ORDER_NEEDS_ACTIVE = "api.subscriptions.action.create_next_order.not_active"
# A write whose screen is stale (spec 2026-10-27 §16 M2): "This changed. Refresh to see the latest."
STATE_CHANGED = "api.common.state_changed"
DAY_OF_WEEK_REQUIRED = "api.subscriptions.schedule.day_of_week_required"
DAY_OF_MONTH_REQUIRED = "api.subscriptions.schedule.day_of_month_required"
SLOT_NOT_ON_DAY = "api.subscriptions.schedule.slot_not_offered_on_day"
SLOT_NOT_EVERY_DAY = "api.subscriptions.schedule.slot_not_every_day"
SLOT_CHOICE_REQUIRED = "api.subscriptions.schedule.slot_choice_required"
DAY_NOT_DELIVERY_DAY = "api.subscriptions.schedule.day_not_delivery_day"
METHOD_NOT_OFFERED = "api.subscriptions.payment.method_not_offered"
# The customer's address move to an address whose menu drops the method asks for one (spec 2026-10-27 §7, B8 re-pick).
METHOD_REQUIRED = "api.subscriptions.address.payment_method_required"
INVALID_ADDRESS = "api.subscriptions.error.invalid_delivery_address"
INACTIVE_SLOT = "api.subscriptions.error.invalid_or_inactive_time_slot"

# The rules themselves did not change (spec §5 B1); this pins them. None = allowed.
EXPECTED_REFUSAL = {
    ("active", "pay"): None,
    ("active", "skip"): None,
    ("active", "pause"): None,
    ("active", "resume"): NOT_PAUSED,
    ("active", "cancel"): None,
    ("active", "edit"): None,
    ("active", "remove_item"): None,
    ("active", "create_next_order"): None,
    ("paused", "pay"): NOTHING_TO_PAY,
    ("paused", "skip"): SKIP_NOT_ACTIVE,
    ("paused", "pause"): PAUSE_NOT_ACTIVE,
    ("paused", "resume"): None,
    ("paused", "cancel"): None,
    ("paused", "edit"): None,
    ("paused", "remove_item"): None,
    ("paused", "create_next_order"): ORDER_NEEDS_ACTIVE,
    ("cancelled", "pay"): NOTHING_TO_PAY,
    ("cancelled", "skip"): SKIP_NOT_ACTIVE,
    ("cancelled", "pause"): PAUSE_NOT_ACTIVE,
    ("cancelled", "resume"): NOT_PAUSED,
    ("cancelled", "cancel"): ALREADY_CANCELLED,
    ("cancelled", "edit"): CLOSED,
    ("cancelled", "remove_item"): CLOSED,
    ("cancelled", "create_next_order"): ORDER_NEEDS_ACTIVE,
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _external_io():
    """The Celery publish, the route notifications and the Click link are external I/O."""
    with patch.object(create_subscription_delivery_task, "delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ), patch(
        "business_app.services.payment_service.PaymentService.create_payment_link",
        lambda self, payment_id, **_: {"payment_url": f"https://my.click.uz/services/pay?id={payment_id}"},
    ):
        yield


@pytest.fixture(autouse=True)
def copy(db):
    """Every refusal key these paths answer with, seeded exactly as the canonical seeder ships it, so a
    refusal is real text and never the bare key."""
    keys = [key for key in BACKEND_TRANSLATIONS if key.startswith(GUARD_KEY_PREFIXES)]
    for key in keys:
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    return keys


class _Clock:
    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch

    def at(self, day: date, hour: int = 10):
        """The business clock, and the instant Pay reads (the published answer's and the write guard's)."""
        frozen = freeze_local_now(self._monkeypatch, datetime.combine(day, time(hour, 0), tzinfo=TASHKENT))
        instant = frozen.astimezone(UTC)
        self._monkeypatch.setattr(customer_order_actions, "get_utc_now", lambda: instant)
        self._monkeypatch.setattr(payment_projection, "get_utc_now", lambda: instant)
        return frozen


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    return _Clock(monkeypatch)


@pytest.fixture
def monday(clock) -> date:
    """The first Monday on or after the real local date, 10:00 Tashkent (`start_date` still defaults to the
    wall clock, so it must not fall after the frozen one)."""
    today = datetime.now(TASHKENT).date()
    day = today + timedelta(days=(7 - today.weekday()) % 7)
    clock.at(day)
    return day


@pytest.fixture
def every_day_slot(db):
    slot = DeliveryTimeSlot(name="Morning", start_time="10:00", end_time="12:00", is_active=True,
                            available_days=[0, 1, 2, 3, 4, 5, 6])
    db.session.add(slot)
    db.session.commit()
    return slot


@pytest.fixture
def other_every_day_slot(db):
    slot = DeliveryTimeSlot(name="Afternoon", start_time="14:00", end_time="17:00", is_active=True,
                            available_days=[0, 1, 2, 3, 4, 5, 6])
    db.session.add(slot)
    db.session.commit()
    return slot


@pytest.fixture
def weekday_slot(db):
    """Offered Monday to Friday only."""
    slot = DeliveryTimeSlot(name="Office hours", start_time="09:00", end_time="18:00", is_active=True,
                            available_days=[0, 1, 2, 3, 4])
    db.session.add(slot)
    db.session.commit()
    return slot


@pytest.fixture
def cups(db, sample_category):
    product = Product(
        name="Spring water 5L", description="Five litres", category_id=sample_category.id, size="5L",
        volume=5.0, volume_unit="L", base_price=Decimal("5000.00"), stock_quantity=100,
        min_stock_level=1, max_stock_level=500, is_active=True, created_at=datetime.now(UTC),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def jug(db, sample_category):
    """A third product, in no subscription yet: what the admin drawer's Add item adds."""
    product = Product(
        name="Water jug 10L", description="Ten litres", category_id=sample_category.id, size="10L",
        volume=10.0, volume_unit="L", base_price=Decimal("9000.00"), stock_quantity=100,
        min_stock_level=1, max_stock_level=500, is_active=True, created_at=datetime.now(UTC),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def admin_headers(app, admin_user):
    """The admin UI's bearer token; `validate_admin_action` reads the role claim (CLAUDE.md)."""
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class _Web:
    """base.js `apiRequest` as the storefront sends it: the httpOnly `access_token_cookie`, the readable
    `csrf_access_token` copied into `X-CSRF-TOKEN`, `X-Platform: web`, and no Authorization header."""

    def __init__(self, app, client, user):
        with app.app_context():
            token = create_access_token(identity=str(user.id))
        self._csrf = get_csrf_token(token)
        client.set_cookie("access_token_cookie", token)
        client.set_cookie("csrf_access_token", self._csrf)
        self._client = client

    def _headers(self):
        return {"Content-Type": "application/json", "X-Platform": "web", "X-CSRF-TOKEN": self._csrf}

    def get(self, path):
        return self._client.get(path, headers={"X-Platform": "web"})

    def post(self, path, body=None):
        return self._client.post(path, headers=self._headers(), data=json.dumps({} if body is None else body))

    def put(self, path, body):
        return self._client.put(path, headers=self._headers(), data=json.dumps(body))

    def delete(self, path):
        return self._client.delete(path, headers=self._headers())


@pytest.fixture
def web(app, client, sample_user):
    return _Web(app, client, sample_user)


# --------------------------------------------------------------------------- builders and readers


def _create(web, address_id, items, **fields) -> int:
    body = {
        "name": "Home water",
        "frequency": "weekly",
        "delivery_day_of_week": THURSDAY,
        "delivery_address_id": address_id,
        "payment_method": "click",
        "items": items,
        "auto_renew": True,
    }
    body.update(fields)
    resp = web.post(f"{BASE}/", body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["subscription"]["id"]


def _row(db, subscription_id) -> Subscription:
    db.session.expire_all()
    return db.session.get(Subscription, subscription_id)


def _orders(db, subscription_id):
    db.session.expire_all()
    return Order.query.filter_by(subscription_id=subscription_id).order_by(Order.delivery_date, Order.id).all()


def _logs(db, subscription_id, action):
    db.session.expire_all()
    return SubscriptionLog.query.filter_by(subscription_id=subscription_id, action=action).all()


def _order_ref(order):
    return {"id": order.id, "order_number": order.order_number, "delivery_date": order.delivery_date.isoformat()}


def _ids(refs):
    """Published or returned order refs without their display labels (tests/integration/
    test_subscription_page_data.py pins those)."""
    return [{key: ref[key] for key in ("id", "order_number", "delivery_date", "reason_code") if key in ref} for ref in refs]


def _published(web, subscription_id):
    resp = web.get(f"{BASE}/{subscription_id}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["subscription"]


def _state(reader, subscription_id, headers=None) -> str:
    """The `expected_state` the customer's screen is drawn from (spec 2026-10-27 §16 M2): `allowed_actions.
    expected_state` as `GET /subscriptions/<id>` publishes it, read fresh and never recomputed here. `reader` is the
    website (`_Web`), or the test client with the bot's (or a test's) bearer `headers`."""
    path = f"{BASE}/{subscription_id}"
    resp = reader.get(path) if headers is None else reader.get(path, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["subscription"]["allowed_actions"]["expected_state"]


def _admin_published(client, admin_headers, subscription_id):
    resp = client.get(f"{ADMIN_BASE}/{subscription_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["subscription"]


def _refusal_text(resp) -> str:
    """The customer routes answer `message`; the admin routes answer `errors[0]` under "Validation failed"."""
    body = resp.get_json()
    if body.get("message") == "Validation failed" and body.get("errors"):
        return body["errors"][0]
    return body["message"]


def _translated(key):
    return set(BACKEND_TRANSLATIONS[key].values())


def _mark_paid(db, order_id):
    """What the Click webhook leaves behind (the gateway itself is external I/O)."""
    order = db.session.get(Order, order_id)
    order.is_paid = True
    order.payment.status = PaymentStatus.COMPLETED
    db.session.commit()


def _picked_up(db, order_id):
    """What the driver's Picked up tap leaves: the order is out for delivery."""
    order = db.session.get(Order, order_id)
    order.status = OrderStatus.OUT_FOR_DELIVERY
    db.session.commit()


@pytest.fixture
def build(web, db, clock, monday, user_address, sample_product, cups, every_day_slot):
    """A weekly-Thursday Click subscription with two items, created on Monday, whose Thursday order the
    Wednesday run has created; then left in `status`, its order `paid` or with the driver."""

    def make(status="active", *, order="unpaid", items=None):
        sub_id = _create(
            web,
            user_address.id,
            items or [{"product_id": sample_product.id, "quantity": 2}, {"product_id": cups.id, "quantity": 1}],
            delivery_time_slot_id=every_day_slot.id,
        )
        clock.at(monday + timedelta(days=2))
        process_daily_subscription_billing()
        [created] = _orders(db, sub_id)
        if order == "paid":
            _mark_paid(db, created.id)
        elif order == "with_driver":
            _picked_up(db, created.id)
        if status == "paused":
            assert web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(web, sub_id)}).status_code == 200
        elif status == "cancelled":
            assert web.post(f"{BASE}/{sub_id}/cancel", {"expected_state": _state(web, sub_id)}).status_code == 200
        return sub_id

    return make


# --------------------------------------------------------------------------- the core: published ⇔ guard


def _latest_order_id(db, sub_id):
    orders = _orders(db, sub_id)
    return orders[-1].id if orders else None


def _second_item_id(db, sub_id):
    db.session.expire_all()
    items = SubscriptionItem.query.filter_by(subscription_id=sub_id).order_by(SubscriptionItem.id).all()
    return items[-1].id


def _customer_call(web, db, sub_id, action, published, payload):
    if action == "pay":
        order_id = published.get("order_id") or _latest_order_id(db, sub_id)
        # The amount the screen showed; a refused Pay publishes none, so the order's total.
        amount = (
            published["expected_amount"] if published["allowed"] else str(db.session.get(Order, order_id).total_amount)
        )
        return web.post(f"/api/v1/orders/{order_id}/retry-payment", {"channel": "web", "expected_amount": amount})
    if action == "skip":
        return web.post(
            f"{BASE}/{sub_id}/skip-next-delivery",
            {"expected_date": published.get("date") or payload["next_delivery_date"]},
        )
    if action == "resume":
        return web.post(f"{BASE}/{sub_id}/resume", {})
    if action in ("pause", "cancel"):
        # The state the screen was drawn from (spec 2026-10-27 §16 M2): the GET just read.
        return web.post(f"{BASE}/{sub_id}/{action}", {"expected_state": payload["allowed_actions"]["expected_state"]})
    if action == "edit":
        return web.put(f"{BASE}/{sub_id}", {"name": "Renamed water"})
    if action == "remove_item":
        return web.delete(f"{BASE}/{sub_id}/items/{_second_item_id(db, sub_id)}")
    raise AssertionError(action)


def _admin_call(client, admin_headers, db, sub_id, action, new_product_id=None):
    if action == "pause":
        return client.post(f"{ADMIN_BASE}/{sub_id}/pause", headers=admin_headers, json={"pause_reason": "x"})
    if action == "resume":
        return client.post(f"{ADMIN_BASE}/{sub_id}/resume", headers=admin_headers, json={})
    if action == "cancel":
        return client.post(f"{ADMIN_BASE}/{sub_id}/cancel", headers=admin_headers, json={"cancellation_reason": "x"})
    if action == "edit":
        return client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"name": "Renamed by admin"})
    if action == "remove_item":
        return client.delete(f"{ADMIN_BASE}/{sub_id}/items/{_second_item_id(db, sub_id)}", headers=admin_headers)
    if action == "create_next_order":
        return client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})
    # The drawer's Add item and its quantity Save send exactly these bodies (Subscriptions.js).
    if action == "add_item":
        return client.post(
            f"{ADMIN_BASE}/{sub_id}/items", headers=admin_headers, json={"product_id": new_product_id, "quantity": 1}
        )
    if action == "update_item":
        return client.put(
            f"{ADMIN_BASE}/{sub_id}/items/{_second_item_id(db, sub_id)}", headers=admin_headers, json={"quantity": 3}
        )
    raise AssertionError(action)


_CASES = (
    [("customer", action) for action in CUSTOMER_ACTIONS]
    + [("admin", action) for action in ADMIN_ACTIONS]
    + [("admin", action) for action in ADMIN_ITEM_ROUTES]
)


@pytest.mark.parametrize("status", ["active", "paused", "cancelled"])
@pytest.mark.parametrize("actor, action", _CASES, ids=[f"{actor}-{action}" for actor, action in _CASES])
def test_the_published_answer_is_the_write_guard(
    client, db, web, admin_headers, build, jug, status, actor, action
):
    """GET the subscription as that actor's client does, then perform the action: published allowed ⇔ the
    write accepts it, and a refusal answers 4xx with the very block_reason the GET published. The admin item
    routes are judged by the answer that gates them in the drawer (`edit`)."""
    sub_id = build(status)
    rule = ADMIN_ITEM_ROUTES.get(action, action)
    if actor == "customer":
        payload = _published(web, sub_id)
        published = payload["allowed_actions"][action]
        resp = _customer_call(web, db, sub_id, action, published, payload)
    else:
        published = _admin_published(client, admin_headers, sub_id)["allowed_actions"][rule]
        resp = _admin_call(client, admin_headers, db, sub_id, action, new_product_id=jug.id)

    expected = EXPECTED_REFUSAL[(status, rule)]
    assert published["allowed"] is (expected is None), published
    if published["allowed"]:
        assert published["block_reason"] is None
        assert resp.status_code in (200, 201), resp.get_json()
    else:
        assert 400 <= resp.status_code < 500, resp.get_json()
        assert published["block_reason"] in _translated(expected)
        if action != "pay":
            # Pay's write is the ORDER's retry route; it refuses in its own (order) words.
            assert _refusal_text(resp) == published["block_reason"]


def test_each_actor_is_published_its_own_actions(client, db, web, admin_headers, build):
    sub_id = build()

    # The customer's answer also carries the state its writes send back (spec 2026-10-27 §16 M2).
    assert set(_published(web, sub_id)["allowed_actions"]) == set(CUSTOMER_ACTIONS) | {"expected_state"}
    assert set(_admin_published(client, admin_headers, sub_id)["allowed_actions"]) == set(ADMIN_ACTIONS)
    listed = client.get(f"{ADMIN_BASE}?per_page=100", headers=admin_headers).get_json()["data"]["items"]
    [row] = [row for row in listed if row["id"] == sub_id]
    assert set(row["allowed_actions"]) == set(ADMIN_ACTIONS)
    [mine] = [row for row in web.get(f"{BASE}/").get_json()["data"]["items"] if row["id"] == sub_id]
    assert mine["allowed_actions"] == _published(web, sub_id)["allowed_actions"]


def test_the_last_item_cannot_be_removed_and_says_why(db, web, client, admin_headers, build, sample_product):
    sub_id = build(items=[{"product_id": sample_product.id, "quantity": 2}])
    published = _published(web, sub_id)["allowed_actions"]["remove_item"]
    [item] = SubscriptionItem.query.filter_by(subscription_id=sub_id).all()

    customer = web.delete(f"{BASE}/{sub_id}/items/{item.id}")
    admin = client.delete(f"{ADMIN_BASE}/{sub_id}/items/{item.id}", headers=admin_headers)

    assert published["allowed"] is False
    assert published["block_reason"] in _translated(LAST_ITEM)
    assert (customer.status_code, _refusal_text(customer)) == (400, published["block_reason"])
    assert (admin.status_code, _refusal_text(admin)) == (400, published["block_reason"])
    assert SubscriptionItem.query.filter_by(subscription_id=sub_id).count() == 1


def test_every_customer_edit_path_of_a_cancelled_subscription_gives_the_published_reason(
    db, web, build, sample_product, cups
):
    """PUT, add item, update item and change payment method are all "edit": one rule, one reason."""
    sub_id = build("cancelled")
    reason = _published(web, sub_id)["allowed_actions"]["edit"]["block_reason"]
    item_id = _second_item_id(db, sub_id)
    before = _row(db, sub_id).updated_at

    responses = [
        web.put(f"{BASE}/{sub_id}", {"name": "Renamed water"}),
        web.post(f"{BASE}/{sub_id}/items", {"product_id": cups.id, "quantity": 1}),
        web.put(f"{BASE}/{sub_id}/items/{item_id}", {"quantity": 3}),
        web.post(
            f"{BASE}/{sub_id}/change-payment-method",
            {"payment_method": "cash", "expected_state": _state(web, sub_id)},
        ),
    ]

    assert reason in _translated(CLOSED)
    assert [(r.status_code, _refusal_text(r)) for r in responses] == [(400, reason)] * 4
    assert _row(db, sub_id).updated_at == before


def test_the_admin_edit_any_status_override_still_edits_a_cancelled_subscription(
    db, client, admin_headers, build
):
    sub_id = build("cancelled")
    published = _admin_published(client, admin_headers, sub_id)["allowed_actions"]["edit"]

    resp = client.put(
        f"{ADMIN_BASE}/{sub_id}",
        headers=admin_headers,
        json={"name": "Fixed by admin", "override_edit_any_status": True},
    )

    assert (published["allowed"], published["override_available"]) == (False, True)
    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).name == "Fixed by admin"


# --------------------------------------------------------------------------- the published consequences


@pytest.mark.parametrize("order", ["unpaid", "paid", "with_driver"])
@pytest.mark.parametrize("action", ["skip", "pause", "cancel"])
def test_the_published_consequences_are_what_the_action_does(db, web, build, monday, order, action):
    sub_id = build(order=order)
    [thursday_order] = _orders(db, sub_id)
    payload = _published(web, sub_id)
    published = payload["allowed_actions"][action]
    body = (
        {"expected_date": published["date"]}
        if action == "skip"
        else {"expected_state": payload["allowed_actions"]["expected_state"]}
    )

    resp = web.post(f"{BASE}/{sub_id}/{'skip-next-delivery' if action == 'skip' else action}", body)

    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert published["will_cancel_orders"] == data["cancelled_orders"]
    assert published["kept_orders"] == data["kept_orders"]
    ref = _order_ref(thursday_order)
    thursday = monday + timedelta(days=3)
    shown = (_ids(published["will_cancel_orders"]), _ids(published["kept_orders"]))
    if order == "unpaid":
        assert shown == ([ref], [])
    elif order == "paid":
        assert shown == ([], [{**ref, "reason_code": "PAID"}])
    elif action == "skip":
        # The driver has it: skip applies from the following delivery (spec §12).
        assert shown == ([], [])
    else:
        assert shown == (
            [],
            [{**ref, "reason_code": "WITH_DRIVER"}],
        )
    if action == "skip":
        assert published["date"] == data["original_delivery_date"]
        assert published["next_date_after"] == data["new_next_delivery_date"]
        assert published["date"] == (thursday if order == "unpaid" else thursday + WEEK).isoformat()


def test_resume_publishes_the_next_delivery_it_will_give(db, web, build):
    sub_id = build("paused")
    preview = _published(web, sub_id)["allowed_actions"]["resume"]["resume_preview"]

    resp = web.post(f"{BASE}/{sub_id}/resume", {})

    assert resp.status_code == 200, resp.get_json()
    assert preview == _row(db, sub_id).next_delivery_date.isoformat()
    assert resp.get_json()["data"]["subscription"]["next_delivery_date"] == preview


def test_paused_at_is_published(db, web, build):
    sub_id = build()
    assert _published(web, sub_id)["paused_at"] is None

    web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(web, sub_id)})

    paused_at = _published(web, sub_id)["paused_at"]
    assert paused_at == _row(db, sub_id).paused_at.isoformat()
    web.post(f"{BASE}/{sub_id}/resume", {})
    assert _published(web, sub_id)["paused_at"] is None


# --------------------------------------------------------------------------- skip: the date the button named


@pytest.fixture
def no_order_yet(web, user_address, sample_product, every_day_slot, monday):
    """Created on Monday for Thursday; Wednesday's run has not happened."""
    return _create(
        web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
        delivery_time_slot_id=every_day_slot.id,
    )


class TestSkipNamesItsDate:
    def test_the_named_date_is_skipped(self, db, web, no_order_yet, monday):
        thursday = monday + timedelta(days=3)
        published = _published(web, no_order_yet)["allowed_actions"]["skip"]

        resp = web.post(
            f"{BASE}/{no_order_yet}/skip-next-delivery",
            {"expected_date": thursday.isoformat(), "reason_code": "other", "reason_text": "Visiting family"},
        )

        assert published["date"] == thursday.isoformat()
        assert resp.status_code == 200, resp.get_json()
        assert _row(db, no_order_yet).next_delivery_date == thursday + WEEK
        [log] = _logs(db, no_order_yet, "delivery_skipped")
        assert (log.extra_data["reason_code"], log.extra_data["reason_text"]) == ("other", "Visiting family")
        assert log.extra_data["original_delivery_date"] == thursday.isoformat()

    def test_paid_meanwhile_answers_409_and_skips_nothing(self, db, web, build, monday):
        """Review Focus 1: the page says "Skip 30 October"; meanwhile the order was paid, so the skip would
        now land on the delivery after it. Nothing is skipped and the customer is told to refresh."""
        sub_id = build()
        named = _published(web, sub_id)["allowed_actions"]["skip"]["date"]
        [order] = _orders(db, sub_id)
        _mark_paid(db, order.id)
        before = _row(db, sub_id).next_delivery_date

        resp = web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": named})

        assert resp.status_code == 409, resp.get_json()
        assert resp.get_json()["message"] in _translated(STATE_CHANGED)
        [after] = _orders(db, sub_id)
        assert (after.status, after.is_paid) == (OrderStatus.PENDING, True)
        assert _row(db, sub_id).next_delivery_date == before
        assert _logs(db, sub_id, "delivery_skipped") == []

    def test_created_meanwhile_still_skips_the_named_date(self, db, web, no_order_yet, clock, monday):
        """The order Wednesday's run created is for the same Thursday: that is the date named, so it is
        skipped (its unpaid order cancelled)."""
        named = _published(web, no_order_yet)["allowed_actions"]["skip"]["date"]
        clock.at(monday + timedelta(days=2))
        process_daily_subscription_billing()

        resp = web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": named})

        assert resp.status_code == 200, resp.get_json()
        assert [o.status for o in _orders(db, no_order_yet)] == [OrderStatus.CANCELLED]

    def test_a_second_tap_on_the_same_date_answers_409(self, db, web, no_order_yet, monday):
        named = (monday + timedelta(days=3)).isoformat()
        assert web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": named}).status_code == 200

        resp = web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": named})

        assert resp.status_code == 409
        assert _row(db, no_order_yet).next_delivery_date == monday + timedelta(days=3) + WEEK

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"expected_date": "next thursday"},
            {"expected_date": "2026-11-06", "reason_code": "bored"},
            {"expected_date": "2026-11-06", "reason_code": "away", "reason_text": "text needs other"},
            {"expected_date": "2026-11-06", "reason": "the old free-text field"},
        ],
        ids=["no-date", "not-a-date", "unknown-reason", "text-without-other", "old-field"],
    )
    def test_a_malformed_body_is_a_400_and_skips_nothing(self, db, web, no_order_yet, body):
        before = _row(db, no_order_yet).next_delivery_date

        resp = web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", body)

        assert resp.status_code == 400, resp.get_json()
        assert _row(db, no_order_yet).next_delivery_date == before


# --------------------------------------------------------------------------- what the screen showed (M2)


def _with(token, **body):
    """A write's body, with the `expected_state` its screen showed; none when `token` is None."""
    return {**body, **({} if token is None else {"expected_state": token})}


# Every customer subscription write that shows consequences, with the body its screen sends (spec 2026-10-27 §16 M2):
# pause, cancel, the schedule save, the items save and the payment-method change.
_STATE_WRITES = {
    "pause": lambda web, sub_id, token, product_id: web.post(f"{BASE}/{sub_id}/pause", _with(token)),
    "cancel": lambda web, sub_id, token, product_id: web.post(f"{BASE}/{sub_id}/cancel", _with(token)),
    "schedule": lambda web, sub_id, token, product_id: web.put(
        f"{BASE}/{sub_id}", _with(token, delivery_day_of_week=THURSDAY + 1)
    ),
    "items": lambda web, sub_id, token, product_id: web.put(
        f"{BASE}/{sub_id}/items", _with(token, items=[{"product_id": product_id, "quantity": 3}])
    ),
    "payment_method": lambda web, sub_id, token, product_id: web.post(
        f"{BASE}/{sub_id}/change-payment-method", _with(token, payment_method="cash")
    ),
}
# What each write answers on a cancelled subscription: its own published refusal.
_REFUSED_WHEN_CANCELLED = {
    "pause": PAUSE_NOT_ACTIVE,
    "cancel": ALREADY_CANCELLED,
    "schedule": CLOSED,
    "items": CLOSED,
    "payment_method": CLOSED,
}


def _snapshot(db, sub_id):
    """Everything these writes could change: status, day, method, items, the orders' statuses and the logs."""
    sub = _row(db, sub_id)
    return (
        sub.status,
        sub.delivery_day_of_week,
        sub.payment_method,
        sorted((item.product_id, item.quantity) for item in sub.subscription_items),
        [(order.id, order.status) for order in _orders(db, sub_id)],
        SubscriptionLog.query.filter_by(subscription_id=sub_id).count(),
    )


def _put_snapshot(db, sub_id):
    """`_snapshot` plus what the schedule and address PUT writes: the address, the slot, the frequency, the day of
    the month and the next delivery."""
    snapshot = _snapshot(db, sub_id)
    sub = _row(db, sub_id)
    return snapshot, (
        sub.delivery_address_id,
        sub.delivery_time_slot_id,
        sub.frequency,
        sub.delivery_day_of_month,
        sub.next_delivery_date,
    )


def _stale_state(web, client, admin_headers, db, sub_id) -> str:
    """The token a screen holds after an admin changed a quantity in another tab: published, then moved."""
    token = _state(web, sub_id)
    resp = client.put(
        f"{ADMIN_BASE}/{sub_id}/items/{_second_item_id(db, sub_id)}", headers=admin_headers, json={"quantity": 3}
    )
    assert resp.status_code == 200, resp.get_json()
    assert _state(web, sub_id) != token
    return token


def _assert_state_changed(resp):
    body = resp.get_json()
    assert (resp.status_code, body["data"], body["message"]) == (
        409,
        {"reason_code": "STATE_CHANGED"},
        "This changed. Refresh to see the latest.",
    ), body


class TestExpectedState:
    """Spec 2026-10-27 §16 M2: a customer subscription write that shows consequences carries the `expected_state`
    its screen was drawn from (`allowed_actions.expected_state`, or the preview's). When what the confirm named
    has moved since, nothing is written and the answer is 409 STATE_CHANGED. The rule answers first, the screen
    second; admin writes take no token."""

    @pytest.mark.parametrize("write", _STATE_WRITES)
    def test_the_published_state_is_accepted(self, db, web, build, sample_product, write):
        sub_id = build()

        resp = _STATE_WRITES[write](web, sub_id, _state(web, sub_id), sample_product.id)

        assert resp.status_code == 200, resp.get_json()

    @pytest.mark.parametrize("write", _STATE_WRITES)
    def test_a_write_after_the_state_moved_is_a_409_and_writes_nothing(self, db, web, build, sample_product, write):
        sub_id = build()
        token = _state(web, sub_id)
        [order] = _orders(db, sub_id)
        _mark_paid(db, order.id)  # paid in another tab after the screen was drawn
        before = _snapshot(db, sub_id)

        resp = _STATE_WRITES[write](web, sub_id, token, sample_product.id)

        _assert_state_changed(resp)
        assert _snapshot(db, sub_id) == before

    @pytest.mark.parametrize("write", _STATE_WRITES)
    def test_a_write_without_a_state_is_stale(self, db, web, build, sample_product, write):
        sub_id = build()
        before = _snapshot(db, sub_id)

        resp = _STATE_WRITES[write](web, sub_id, None, sample_product.id)

        _assert_state_changed(resp)
        assert _snapshot(db, sub_id) == before

    def test_a_rename_needs_no_state(self, db, web, build):
        sub_id = build()

        resp = web.put(f"{BASE}/{sub_id}", {"name": "Renamed water"})

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).name == "Renamed water"

    @pytest.mark.parametrize("write", _STATE_WRITES)
    def test_a_refusal_answers_before_a_stale_state(self, db, web, build, sample_product, write):
        """Cancelled in another tab: the token the screen holds is stale, and the write answers its own refusal."""
        sub_id = build()
        stale = _state(web, sub_id)
        assert web.post(f"{BASE}/{sub_id}/cancel", {"expected_state": stale}).status_code == 200
        before = _snapshot(db, sub_id)

        resp = _STATE_WRITES[write](web, sub_id, stale, sample_product.id)

        assert resp.status_code == 400, resp.get_json()
        assert _refusal_text(resp) in _translated(_REFUSED_WHEN_CANCELLED[write])
        assert _snapshot(db, sub_id) == before

    @pytest.mark.parametrize("screen", ["no_state", "stale_state"])
    @pytest.mark.parametrize(
        "refusal", ["strangers_address", "inactive_slot", "slot_not_on_day", "day_of_month_missing"]
    )
    def test_the_put_answers_its_own_refusal_before_a_stale_state(
        self, db, web, client, admin_headers, build, weekday_slot, refusal, screen
    ):
        """The schedule and address PUT checks its own rule before what the screen showed. Another customer's
        address is the 404 it always was, and a slot or day the schedule cannot have is the schedule's own
        refusal. This holds with no token or a stale one, and nothing is written."""
        sub_id = build()
        if refusal == "strangers_address":
            body, status, key = {"delivery_address_id": make_address(db, make_user(db)).id}, 404, INVALID_ADDRESS
        elif refusal == "inactive_slot":
            retired = DeliveryTimeSlot(name="Retired", start_time="18:00", end_time="20:00", is_active=False,
                                       available_days=[0, 1, 2, 3, 4, 5, 6])
            db.session.add(retired)
            db.session.commit()
            body, status, key = {"delivery_time_slot_id": retired.id}, 404, INACTIVE_SLOT
        elif refusal == "slot_not_on_day":
            body = {"delivery_day_of_week": SATURDAY, "delivery_time_slot_id": weekday_slot.id}
            status, key = 400, SLOT_NOT_ON_DAY
        else:
            body, status, key = {"frequency": "monthly"}, 400, DAY_OF_MONTH_REQUIRED
        token = None if screen == "no_state" else _stale_state(web, client, admin_headers, db, sub_id)
        before = _put_snapshot(db, sub_id)

        resp = web.put(f"{BASE}/{sub_id}", _with(token, **body))

        assert resp.status_code == status, resp.get_json()
        assert _refusal_text(resp) in _translated(key)
        assert _put_snapshot(db, sub_id) == before

    @pytest.mark.parametrize("screen", ["no_state", "stale_state"])
    def test_a_method_the_new_address_does_not_offer_answers_before_a_stale_state(
        self, db, client, admin_headers, capped_office, sample_product, monday, screen
    ):
        """B8 on the customer's address move is a refusal too (it asks for a method the new address offers), so it
        answers before what the screen showed."""
        user, home, office = capped_office
        web = _Web(client.application, client, user)
        sub_id = _create(web, home.id, [{"product_id": sample_product.id, "quantity": 2}], payment_method="cash")
        token = None if screen == "no_state" else _stale_state(web, client, admin_headers, db, sub_id)
        before = _put_snapshot(db, sub_id)

        resp = web.put(f"{BASE}/{sub_id}", _with(token, delivery_address_id=office.id))

        assert resp.status_code == 400, resp.get_json()
        assert _refusal_text(resp) in _translated(METHOD_REQUIRED)
        assert resp.get_json()["data"]["error_code"] == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED"
        assert _put_snapshot(db, sub_id) == before

    def test_the_state_is_one_token_in_every_language_and_in_the_preview(self, web, build):
        """No labels and no language go into it, so the bot and the website send one token."""
        sub_id = build()
        token = _state(web, sub_id)
        answers = {
            language: web.get(f"{BASE}/{sub_id}?lang={language}").get_json()["data"]["subscription"]
            for language in ("en", "ru", "uz")
        }
        [listed] = [row for row in web.get(f"{BASE}/").get_json()["data"]["items"] if row["id"] == sub_id]
        preview = web.post(f"{BASE}/{sub_id}/preview", {"delivery_day_of_week": THURSDAY + 1})

        assert re.fullmatch(r"[0-9a-f]{16}", token), token
        assert len({answers[language]["allowed_actions"]["pause"]["label"] for language in answers}) == 3
        assert {answers[language]["allowed_actions"]["expected_state"] for language in answers} == {token}
        assert listed["allowed_actions"]["expected_state"] == token
        assert preview.status_code == 200, preview.get_json()
        assert preview.get_json()["data"]["preview"]["expected_state"] == token

    @pytest.mark.parametrize(
        "change", ["order_paid", "picked_up", "item_quantity", "schedule_day", "next_order_created"]
    )
    def test_the_token_moves_with_what_a_confirm_names(
        self, request, db, web, client, admin_headers, clock, monday, change
    ):
        sub_id = request.getfixturevalue("no_order_yet" if change == "next_order_created" else "build")
        if change != "next_order_created":
            sub_id = sub_id()
        before = _state(web, sub_id)

        if change == "order_paid":
            _mark_paid(db, _orders(db, sub_id)[0].id)
        elif change == "picked_up":
            _picked_up(db, _orders(db, sub_id)[0].id)
        elif change == "item_quantity":
            item_url = f"{ADMIN_BASE}/{sub_id}/items/{_second_item_id(db, sub_id)}"
            resp = client.put(item_url, headers=admin_headers, json={"quantity": 3})
            assert resp.status_code == 200, resp.get_json()
        elif change == "schedule_day":
            resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"delivery_day_of_week": SATURDAY})
            assert resp.status_code == 200, resp.get_json()
        else:
            clock.at(monday + timedelta(days=2))
            process_daily_subscription_billing()
            assert len(_orders(db, sub_id)) == 1

        assert _state(web, sub_id) != before

    def test_admin_writes_take_no_state(self, db, client, admin_headers, build):
        sub_id = build()

        update = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"delivery_day_of_week": SATURDAY})
        pause = client.post(f"{ADMIN_BASE}/{sub_id}/pause", headers=admin_headers, json={"pause_reason": "x"})
        cancel = client.post(
            f"{ADMIN_BASE}/{sub_id}/cancel", headers=admin_headers, json={"cancellation_reason": "x"}
        )

        assert [resp.status_code for resp in (update, pause, cancel)] == [200, 200, 200]
        row = _row(db, sub_id)
        assert (row.delivery_day_of_week, row.status) == (SATURDAY, SubscriptionStatus.CANCELLED)
        assert "expected_state" not in _admin_published(client, admin_headers, sub_id)["allowed_actions"]

    def test_a_stale_skip_answers_state_changed(self, db, web, build):
        """Skip keeps its `expected_date`; a date that moved answers the same 409 as a moved token."""
        sub_id = build()
        named = _published(web, sub_id)["allowed_actions"]["skip"]["date"]
        _mark_paid(db, _orders(db, sub_id)[0].id)

        resp = web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": named})

        _assert_state_changed(resp)
        assert _logs(db, sub_id, "delivery_skipped") == []

    def test_the_payment_method_change_reads_the_row_locked(self, db, web, build):
        """The token is compared on the locked row, as every other token write compares it."""
        sub_id = build()
        token = _state(web, sub_id)
        locks = []
        real_fetch = SubscriptionService._fetch

        def spy(query, *, lock=False):
            locks.append(lock)
            return real_fetch(query, lock=lock)

        with patch.object(SubscriptionService, "_fetch", staticmethod(spy)):
            resp = web.post(
                f"{BASE}/{sub_id}/change-payment-method", {"payment_method": "cash", "expected_state": token}
            )

        assert resp.status_code == 200, resp.get_json()
        assert locks and all(locks), locks


# --------------------------------------------------------------------------- schedule guards on PUT (B9)


class TestScheduleGuards:
    @pytest.mark.parametrize(
        "body, key",
        [
            ({"frequency": "weekly"}, DAY_OF_WEEK_REQUIRED),
            ({"frequency": "biweekly"}, DAY_OF_WEEK_REQUIRED),
            ({"frequency": "monthly"}, DAY_OF_MONTH_REQUIRED),
        ],
        ids=["weekly", "biweekly", "monthly"],
    )
    def test_a_frequency_that_needs_a_day_without_one_is_refused(
        self, db, web, client, admin_headers, user_address, sample_product, monday, body, key
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         frequency="daily", delivery_day_of_week=None)
        assert _row(db, sub_id).delivery_day_of_week is None

        customer = web.put(f"{BASE}/{sub_id}", {**body, "expected_state": _state(web, sub_id)})
        admin = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=body)

        assert customer.status_code == 400 and _refusal_text(customer) in _translated(key)
        assert admin.status_code == 400 and _refusal_text(admin) in _translated(key)
        assert _row(db, sub_id).frequency == SubscriptionFrequency.DAILY

    def test_a_day_the_slot_is_not_offered_on_is_refused(
        self, db, web, client, admin_headers, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         delivery_time_slot_id=weekday_slot.id)
        before = _row(db, sub_id).next_delivery_date

        customer = web.put(
            f"{BASE}/{sub_id}", {"delivery_day_of_week": SATURDAY, "expected_state": _state(web, sub_id)}
        )
        admin = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"delivery_day_of_week": SATURDAY})

        # The stored slot is not offered on Saturday and none was chosen: the customer must choose (§6.2).
        assert customer.status_code == 400 and _refusal_text(customer) in _translated(SLOT_CHOICE_REQUIRED)
        assert admin.status_code == 400 and _refusal_text(admin) in _translated(SLOT_CHOICE_REQUIRED)
        row = _row(db, sub_id)
        assert (row.delivery_day_of_week, row.next_delivery_date) == (THURSDAY, before)

    def test_a_slot_not_offered_on_the_day_is_refused(
        self, db, web, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         delivery_day_of_week=SATURDAY)

        resp = web.put(
            f"{BASE}/{sub_id}", {"delivery_time_slot_id": weekday_slot.id, "expected_state": _state(web, sub_id)}
        )

        assert resp.status_code == 400 and _refusal_text(resp) in _translated(SLOT_NOT_ON_DAY)
        assert _row(db, sub_id).delivery_time_slot_id is None

    def test_monthly_needs_a_slot_offered_on_every_day(
        self, db, web, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         delivery_time_slot_id=weekday_slot.id)

        refused = web.put(
            f"{BASE}/{sub_id}",
            {"frequency": "monthly", "delivery_day_of_month": 15, "expected_state": _state(web, sub_id)},
        )
        accepted = web.put(
            f"{BASE}/{sub_id}",
            {
                "frequency": "monthly",
                "delivery_day_of_month": 15,
                "delivery_time_slot_id": every_day_slot.id,
                "expected_state": _state(web, sub_id),
            },
        )

        explicit = web.put(
            f"{BASE}/{sub_id}",
            {
                "frequency": "monthly",
                "delivery_day_of_month": 15,
                "delivery_time_slot_id": weekday_slot.id,
                "expected_state": _state(web, sub_id),
            },
        )

        assert refused.status_code == 400 and _refusal_text(refused) in _translated(SLOT_CHOICE_REQUIRED)
        assert explicit.status_code == 400 and _refusal_text(explicit) in _translated(SLOT_NOT_EVERY_DAY)
        assert accepted.status_code == 200, accepted.get_json()
        row = _row(db, sub_id)
        assert (row.frequency, row.delivery_time_slot_id) == (SubscriptionFrequency.MONTHLY, every_day_slot.id)

    @pytest.mark.parametrize(
        "query, body",
        [
            ("weekday=any&frequency=monthly", {"frequency": "monthly", "delivery_day_of_month": 15}),
            (f"weekday={SATURDAY}&frequency=weekly", {"frequency": "weekly", "delivery_day_of_week": SATURDAY}),
            (f"weekday={THURSDAY}&frequency=biweekly", {"frequency": "biweekly", "delivery_day_of_week": THURSDAY}),
        ],
        ids=["monthly", "weekly-saturday", "biweekly-thursday"],
    )
    def test_every_slot_the_picker_offers_is_one_the_save_accepts(
        self, db, client, web, user_address, sample_product, weekday_slot, every_day_slot, monday, query, body
    ):
        """The read query and the write guard are one rule: a widened list is a promise the PUT honours."""
        offered = client.get(f"/api/v1/delivery/time-slots?{query}").get_json()["time_slots"]
        all_active = {weekday_slot.id, every_day_slot.id}
        assert {slot["id"] for slot in offered} == (
            {every_day_slot.id} if "monthly" in query or f"weekday={SATURDAY}" in query else all_active
        )
        for slot in offered:
            sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
            resp = web.put(
                f"{BASE}/{sub_id}", {**body, "delivery_time_slot_id": slot["id"], "expected_state": _state(web, sub_id)}
            )
            assert resp.status_code == 200, (slot, resp.get_json())
        for slot_id in all_active - {slot["id"] for slot in offered}:
            sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
            resp = web.put(
                f"{BASE}/{sub_id}", {**body, "delivery_time_slot_id": slot_id, "expected_state": _state(web, sub_id)}
            )
            assert resp.status_code == 400, (slot_id, resp.get_json())

    @pytest.mark.parametrize(
        "fields, key",
        [
            ({"frequency": "monthly", "delivery_day_of_week": None, "delivery_day_of_month": 15}, SLOT_NOT_EVERY_DAY),
            ({"frequency": "weekly", "delivery_day_of_week": SATURDAY}, SLOT_NOT_ON_DAY),
        ],
        ids=["monthly", "weekly-saturday"],
    )
    @pytest.mark.parametrize("actor", ["customer", "admin"])
    def test_create_runs_the_same_schedule_guard(
        self, db, web, client, admin_headers, sample_user, user_address, sample_product, weekday_slot, every_day_slot,
        monday, fields, key, actor,
    ):
        """Create used to save what the PUT refuses (final review I2): a monthly slot not offered every day, a
        weekday the slot is not offered on. The website builder's, the bot's and the admin form's creates all
        go through this one guard now, with the PUT's translated refusal."""
        body = {
            "name": "Home water", "frequency": "weekly", "delivery_address_id": user_address.id,
            "payment_method": "click", "delivery_time_slot_id": weekday_slot.id, "auto_renew": True,
            "items": [{"product_id": sample_product.id, "quantity": 2}], **fields,
        }
        resp = (
            web.post(f"{BASE}/", body)
            if actor == "customer"
            else client.post(ADMIN_BASE, headers=admin_headers, json={**body, "user_id": sample_user.id})
        )

        assert resp.status_code == 400, resp.get_json()
        assert _refusal_text(resp) in _translated(key)
        db.session.expire_all()
        assert Subscription.query.count() == 0

    @pytest.mark.parametrize("weekday, status", [(2, 201), (SATURDAY, 400)], ids=["wednesday", "saturday"])
    def test_the_admin_forms_body_is_judged_on_the_day_it_names(
        self, db, client, admin_headers, sample_user, user_address, sample_product, weekday_slot, every_day_slot,
        monday, weekday, status,
    ):
        """Re-review G1 (spec 2026-10-26 B9): the admin form requires the day for weekly, biweekly and monthly and
        asks the slot list with it (Subscriptions.test.js pins the query). With the form's exact create body
        (buildSubscriptionPayload) and a Monday-to-Friday slot, Wednesday is accepted and Saturday refused in
        the PUT's translated words; the list the form asked for that day offered the slot exactly when create
        accepts it."""
        offered = client.get(f"/api/v1/delivery/time-slots?weekday={weekday}&frequency=weekly").get_json()
        body = {
            "user_id": sample_user.id, "name": "Weekly Water", "frequency": "weekly",
            "delivery_day_of_week": weekday, "delivery_day_of_month": None,
            "delivery_time_slot_id": weekday_slot.id, "delivery_address_id": user_address.id,
            "payment_method": "click", "auto_renew": True, "discount_percentage": 0,
            "loyalty_points_multiplier": None, "items": [{"product_id": sample_product.id, "quantity": 2}],
        }

        resp = client.post(ADMIN_BASE, headers=admin_headers, json=body)

        assert resp.status_code == status, resp.get_json()
        assert (weekday_slot.id in {slot["id"] for slot in offered["time_slots"]}) is (status == 201)
        db.session.expire_all()
        if status == 201:
            row = _row(db, resp.get_json()["data"]["subscription"]["id"])
            assert (row.delivery_day_of_week, row.delivery_time_slot_id) == (weekday, weekday_slot.id)
        else:
            assert _refusal_text(resp) in _translated(SLOT_NOT_ON_DAY)
            assert Subscription.query.count() == 0

    def test_an_admin_create_without_a_day_still_keeps_the_first_deliverys(
        self, db, client, admin_headers, sample_user, user_address, sample_product, every_day_slot, monday
    ):
        """A create body without the day (the raw admin API; the admin form now requires it, re-review G1): create
        keeps fixing it from the first delivery (step 2, §2.9), so the guard, which runs after that, never
        refuses a day-less weekly create for its day."""
        resp = client.post(ADMIN_BASE, headers=admin_headers, json={
            "user_id": sample_user.id, "name": "Home water", "frequency": "weekly", "delivery_day_of_week": None,
            "delivery_address_id": user_address.id, "payment_method": "click",
            "delivery_time_slot_id": every_day_slot.id, "items": [{"product_id": sample_product.id, "quantity": 2}],
        })

        assert resp.status_code == 201, resp.get_json()
        row = _row(db, resp.get_json()["data"]["subscription"]["id"])
        assert row.delivery_day_of_week == row.next_delivery_date.weekday()

    def test_an_unknown_frequency_on_the_slot_picker_is_a_400(self, client):
        assert client.get("/api/v1/delivery/time-slots?weekday=any&frequency=yearly").status_code == 400

    def test_a_rename_does_not_run_the_schedule_guard(self, db, web, user_address, sample_product, monday):
        """Only a real schedule change is checked; renaming a subscription saved before the guard still works."""
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         frequency="daily", delivery_day_of_week=None)
        row = _row(db, sub_id)
        row.frequency = SubscriptionFrequency.WEEKLY  # a row as old data left it: weekly with no day
        db.session.commit()

        resp = web.put(f"{BASE}/{sub_id}", {"name": "Renamed water"})

        assert resp.status_code == 200, resp.get_json()


# --------------------------------------------------------------------------- delivery days (spec 2026-10-27 §6.4)


class TestDeliveryDays:
    """A delivery day is a weekday an active slot offers (§6.4, §17 Q7). Here `weekday_slot` (Monday to Friday) is
    the only active slot, so Saturday and Sunday are not delivery days: the day pickers offer only the days
    `GET /delivery/time-slots` publishes, and every save refuses any other in the same words."""

    def test_a_weekday_that_is_not_a_delivery_day_is_refused_on_create_put_and_preview(
        self, db, web, client, admin_headers, sample_user, user_address, sample_product, weekday_slot, monday
    ):
        create = {
            "name": "Home water", "frequency": "weekly", "delivery_day_of_week": SATURDAY,
            "delivery_address_id": user_address.id, "payment_method": "click", "auto_renew": True,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
        }
        customer_create = web.post(f"{BASE}/", create)
        admin_create = client.post(ADMIN_BASE, headers=admin_headers, json={**create, "user_id": sample_user.id})
        db.session.expire_all()
        assert Subscription.query.count() == 0
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
        before = _row(db, sub_id)
        before = (before.delivery_day_of_week, before.next_delivery_date, before.updated_at)

        preview = web.post(f"{BASE}/{sub_id}/preview", {"delivery_day_of_week": SATURDAY})
        put = web.put(f"{BASE}/{sub_id}", {"delivery_day_of_week": SATURDAY, "expected_state": _state(web, sub_id)})
        admin_put = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"delivery_day_of_week": SATURDAY})

        for resp in (customer_create, admin_create, preview, put, admin_put):
            assert resp.status_code == 400, resp.get_json()
            assert _refusal_text(resp) in _translated(DAY_NOT_DELIVERY_DAY), resp.get_json()
        row = _row(db, sub_id)
        assert (row.delivery_day_of_week, row.next_delivery_date, row.updated_at) == before
        assert Subscription.query.count() == 1

    def test_every_day_the_chooser_publishes_is_one_the_save_accepts(
        self, db, web, client, user_address, sample_product, weekday_slot, monday
    ):
        """The read and the write are one rule: a day the picker offers is saved, any other is refused."""
        offered = client.get("/api/v1/delivery/time-slots?weekday=any").get_json()["delivery_weekdays"]
        assert offered == [0, 1, 2, 3, 4]

        for day in range(7):
            sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
            resp = web.put(f"{BASE}/{sub_id}", {"delivery_day_of_week": day, "expected_state": _state(web, sub_id)})
            assert (resp.status_code == 200) is (day in offered), (day, resp.get_json())
            assert _row(db, sub_id).delivery_day_of_week == (day if day in offered else THURSDAY)

    def test_daily_without_a_slot_skips_the_weekend_no_slot_offers(
        self, db, web, clock, user_address, sample_product, weekday_slot, monday
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         frequency="daily", delivery_day_of_week=None)
        week = [monday + timedelta(days=n) for n in range(8)]  # Monday to the next Monday

        assert [day["date"] for day in _published(web, sub_id)["upcoming_dates"]] == [
            day.isoformat() for day in week[1:5]  # Tuesday to Friday
        ]
        for today in week[1:7]:  # the daily run, Tuesday to Sunday
            clock.at(today)
            process_daily_subscription_billing()

        assert [order.delivery_date for order in _orders(db, sub_id)] == week[1:5] + [week[7]]
        assert _row(db, sub_id).next_delivery_date == week[7] + timedelta(days=1)  # the Tuesday after

    def test_a_monthly_date_on_a_non_delivery_day_moves_to_monday(
        self, db, web, user_address, sample_product, weekday_slot, monday
    ):
        # A Saturday early enough in its month that the move to Monday stays in that month every month.
        saturday = next(
            day for day in (monday + timedelta(days=n) for n in range(2, 60)) if day.weekday() == SATURDAY
            and day.day <= 26
        )
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         frequency="monthly", delivery_day_of_week=None, delivery_day_of_month=saturday.day)

        row = _row(db, sub_id)
        assert row.delivery_day_of_month == saturday.day
        assert row.next_delivery_date == saturday + timedelta(days=2)  # the Monday after
        dates = [date.fromisoformat(day["date"]) for day in _published(web, sub_id)["upcoming_dates"]]
        assert dates[0] == saturday + timedelta(days=2)
        assert all(day.weekday() < SATURDAY for day in dates)
        # Each later one is its own month's date (or the delivery day after it): no month is skipped.
        months = [saturday + relativedelta(months=n) for n in range(1, len(dates))]
        assert [(day.year, day.month) for day in dates[1:]] == [(day.year, day.month) for day in months]


# --------------------------------------------------------------------------- a change never restores a skip


class TestAChangeNeverBringsBackASkippedDate:
    def test_skip_then_slot_change(self, db, web, no_order_yet, other_every_day_slot, monday):
        thursday = monday + timedelta(days=3)
        assert web.post(
            f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": thursday.isoformat()}
        ).status_code == 200

        resp = web.put(
            f"{BASE}/{no_order_yet}",
            {"delivery_time_slot_id": other_every_day_slot.id, "expected_state": _state(web, no_order_yet)},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, no_order_yet).next_delivery_date == thursday + WEEK

    def test_skip_after_a_paid_order_then_slot_change(self, db, web, build, other_every_day_slot, monday):
        """The paid Thursday stays; the skip took the Thursday after; a slot change gives neither back."""
        sub_id = build(order="paid")
        thursday = monday + timedelta(days=3)
        named = _published(web, sub_id)["allowed_actions"]["skip"]["date"]
        assert named == (thursday + WEEK).isoformat()
        assert web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": named}).status_code == 200

        resp = web.put(
            f"{BASE}/{sub_id}",
            {"delivery_time_slot_id": other_every_day_slot.id, "expected_state": _state(web, sub_id)},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).next_delivery_date == thursday + 2 * WEEK

    def test_admin_change_after_a_skip_too(self, db, web, client, admin_headers, no_order_yet, other_every_day_slot,
                                           monday):
        thursday = monday + timedelta(days=3)
        web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": thursday.isoformat()})

        resp = client.put(
            f"{ADMIN_BASE}/{no_order_yet}", headers=admin_headers,
            json={"delivery_time_slot_id": other_every_day_slot.id},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, no_order_yet).next_delivery_date == thursday + WEEK


# --------------------------------------------------------------------------- the payment guard (B8)


ALL_METHODS = ("business_account", "cash", "click")


@pytest.fixture
def customer_kind(request, app, client, db, sample_user, user_address, sample_product):
    """(user, address, web) for each customer the payment menu treats differently."""
    kind = request.param
    if kind == "individual":
        user, address = sample_user, user_address
    elif kind == "cash_capped":
        user = make_user(db)
        address = make_address(db, user)
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(db, user, total=Decimal("6000.00"))
    else:
        user, address = make_workplace(db)
        make_contract(db, user, sample_product)
    return kind, user, address, _Web(app, client, user)


@pytest.mark.parametrize("customer_kind", ["individual", "cash_capped", "workplace"], indirect=True)
def test_every_method_the_menu_offers_is_accepted_and_no_other(
    db, client, admin_headers, customer_kind, sample_product, monday
):
    kind, user, address, web = customer_kind
    menu = web.get(f"/api/v1/payments/methods?context=subscription&delivery_address_id={address.id}")
    assert menu.status_code == 200, menu.get_json()
    offered = {m["method"] for m in menu.get_json()["data"]["available_methods"]}
    assert "click" in offered
    assert ("cash" in offered) is (kind != "cash_capped")
    assert ("business_account" in offered) is (kind == "workplace")

    items = [{"product_id": sample_product.id, "quantity": 2}]
    base_id = _create(web, address.id, items)  # click, which every kind is offered
    for method in ALL_METHODS:
        is_offered = method in offered
        customer = web.post(f"{BASE}/", {
            "name": "Home water", "frequency": "weekly", "delivery_day_of_week": THURSDAY,
            "delivery_address_id": address.id, "payment_method": method, "items": items,
        })
        admin = client.post(ADMIN_BASE, headers=admin_headers, json={
            "user_id": user.id, "name": "Home water", "frequency": "weekly", "delivery_day_of_week": THURSDAY,
            "delivery_address_id": address.id, "payment_method": method, "items": items,
        })
        assert customer.status_code == (201 if is_offered else 400), (method, customer.get_json())
        assert admin.status_code == (201 if is_offered else 400), (method, admin.get_json())
        if not is_offered:
            assert _refusal_text(customer) in _translated(METHOD_NOT_OFFERED)
            assert _refusal_text(admin) in _translated(METHOD_NOT_OFFERED)
        if method == "click":
            continue
        change = web.post(
            f"{BASE}/{base_id}/change-payment-method",
            {"payment_method": method, "expected_state": _state(web, base_id)},
        )
        assert change.status_code == (200 if is_offered else 400), (method, change.get_json())
        assert _row(db, base_id).payment_method.value == (method if is_offered else "click")
        back = web.post(
            f"{BASE}/{base_id}/change-payment-method",
            {"payment_method": "click", "expected_state": _state(web, base_id)},
        )
        assert back.status_code == 200, back.get_json()
        update = client.put(f"{ADMIN_BASE}/{base_id}", headers=admin_headers, json={"payment_method": method})
        assert update.status_code == (200 if is_offered else 400), (method, update.get_json())
        assert _row(db, base_id).payment_method.value == (method if is_offered else "click")
        if not is_offered:
            assert _refusal_text(change) in _translated(METHOD_NOT_OFFERED)
            assert _refusal_text(update) in _translated(METHOD_NOT_OFFERED)
        client.put(f"{ADMIN_BASE}/{base_id}", headers=admin_headers, json={"payment_method": "click"})


def _admin_menu(client, admin_headers, user_id, address_id):
    """What the admin subscription form asks (adminService.getCustomerSubscriptionPaymentMethods)."""
    return client.get(
        f"/api/v1/admin/users/{user_id}/payment-methods?context=subscription&delivery_address_id={address_id}",
        headers=admin_headers,
    )


@pytest.mark.parametrize("customer_kind", ["individual", "cash_capped", "workplace"], indirect=True)
def test_the_admin_form_offers_the_customers_menu_and_the_admin_create_honours_it(
    db, client, admin_headers, admin_user, customer_kind, sample_product, monday
):
    """Final review I1. The admin form used to read the ADMIN's own `/payments/methods`, so Bank Transfer was
    unreachable for a corporate customer and cash was offered to a capped one, then refused. It now reads the
    CUSTOMER's subscription menu at the chosen address: the very list the B8 guard checks."""
    kind, user, address, _ = customer_kind
    resp = _admin_menu(client, admin_headers, user.id, address.id)

    assert resp.status_code == 200, resp.get_json()
    offered = {m["method"] for m in resp.get_json()["data"]["available_methods"]}
    assert "click" in offered
    assert ("cash" in offered) is (kind != "cash_capped")
    assert ("business_account" in offered) is (kind == "workplace")
    assert "payme" not in offered  # never a subscription method
    items = [{"product_id": sample_product.id, "quantity": 2}]
    for method in ALL_METHODS:
        create = client.post(ADMIN_BASE, headers=admin_headers, json={
            "user_id": user.id, "name": "Home water", "frequency": "weekly", "delivery_day_of_week": THURSDAY,
            "delivery_address_id": address.id, "payment_method": method, "items": items,
        })
        assert create.status_code == (201 if method in offered else 400), (method, create.get_json())
        if method not in offered:
            assert _refusal_text(create) in _translated(METHOD_NOT_OFFERED)


def test_the_admin_menu_is_asked_for_the_address_the_form_names(db, client, admin_headers, capped_office):
    """The place arm: the same customer is offered cash at home and not at the capped office."""
    user, home, office = capped_office

    at_home = _admin_menu(client, admin_headers, user.id, home.id).get_json()["data"]["available_methods"]
    at_office = _admin_menu(client, admin_headers, user.id, office.id).get_json()["data"]["available_methods"]

    assert "cash" in {m["method"] for m in at_home}
    assert "cash" not in {m["method"] for m in at_office}


def test_the_admin_menu_refuses_another_customers_address_and_an_unknown_context(
    db, client, admin_headers, sample_user, capped_office
):
    _, home, _ = capped_office

    foreign = _admin_menu(client, admin_headers, sample_user.id, home.id)
    unknown = client.get(f"/api/v1/admin/users/{sample_user.id}/payment-methods?context=yearly", headers=admin_headers)

    assert foreign.status_code == 404, foreign.get_json()
    assert unknown.status_code == 400, unknown.get_json()


def test_a_payment_method_alone_is_refused_on_the_customer_put(db, web, no_order_yet):
    """A method rides the PUT only with an address change: the B8 re-pick when the new address's menu drops the
    current one (spec 2026-10-27 §7). On its own it is refused; `/change-payment-method` is its writer."""
    resp = web.put(f"{BASE}/{no_order_yet}", {"payment_method": "cash", "expected_state": _state(web, no_order_yet)})

    assert resp.status_code == 400
    assert _refusal_text(resp) in _translated("api.subscriptions.error.validation_failed")
    assert _row(db, no_order_yet).payment_method.value == "click"


# The bodies the admin Edit form submits (re-review tests-G11a):
# admin_ui/src/__tests__/utils/subscriptionPayload.test.js runs the shipped `mapDetailToForm` and
# `buildSubscriptionPayload` on each case of this file; the tests below PUT the same bodies. No copy of the form's
# mapping lives here.
EDIT_BODIES = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "admin_subscription_edit_bodies.json").read_text(
        "utf-8"
    )
)


def _admin_form_edit_body(db, client, admin_headers, sub_id, case_name, rows):
    """The body the admin Edit form submits for this subscription in the fixture case `case_name`. It first checks
    that the case's detail IS what `GET /admin/subscriptions/<id>` publishes for this row (the keys the form reads),
    so the body is the form's for THIS subscription. `rows` maps the fixture's placeholder names ("address",
    "other_address", "slot") to this test's row ids."""
    case = next(case for case in EDIT_BODIES["cases"] if case["name"] == case_name)
    detail = EDIT_BODIES["details"][case["detail"]]
    real_id = {EDIT_BODIES["placeholders"][name]: row_id for name, row_id in rows.items()}
    row = _row(db, sub_id)
    # Create stamps the wall clock as the start; the fixture names one instant.
    row.start_date = datetime.fromisoformat(detail["start_date"])
    db.session.commit()
    published = _admin_published(client, admin_headers, sub_id)
    for key, expected in detail.items():
        assert key in published, key
        actual = published[key]
        if key == "next_delivery_date":  # a placeholder: the form sends it only behind the override switch
            continue
        if key == "start_date":  # the SQLite test database drops the offset; the instant is the same
            actual = datetime.fromisoformat(actual)
            actual, expected = actual if actual.tzinfo else actual.replace(tzinfo=UTC), datetime.fromisoformat(expected)
        elif key in ("delivery_address_id", "delivery_time_slot_id") and expected is not None:
            expected = real_id[expected]
        assert actual == expected, (key, actual, expected)
    body = deepcopy(case["body"])
    for key in ("delivery_address_id", "delivery_time_slot_id"):
        if body[key] is not None:
            body[key] = real_id[body[key]]
    return body


def _cap_the_person(db, user, _address):
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, user, total=Decimal("6000.00"))


def _cap_the_place(db, _user, address):
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, address, label="capped-later")
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=Decimal("6000.00"))


@pytest.mark.parametrize("cap", [_cap_the_person, _cap_the_place], ids=["person-capped", "place-capped"])
def test_the_admin_form_resending_an_unchanged_method_is_not_refused(
    db, client, admin_headers, sample_product, monday, cap
):
    """The admin form sends every field on every save (`_admin_form_edit_body`, case "rename"). Cash is no longer on
    this address's menu (the customer, or the place, hit the cash-debt limit after cash was chosen), but the address
    and the method are UNCHANGED, so B8's re-check (`payment_method_changed or address_changed`, each compared
    with the stored value) does not run, and renaming the subscription saves. Re-review G11: a check keyed on the
    field being SENT would refuse every save of such a subscription."""
    user = make_user(db)
    address = make_address(db, user)
    web = _Web(client.application, client, user)
    sub_id = _create(web, address.id, [{"product_id": sample_product.id, "quantity": 2}], payment_method="cash")
    cap(db, user, address)
    assert "cash" not in _offered(web, address.id)
    body = _admin_form_edit_body(db, client, admin_headers, sub_id, "rename", {"address": address.id})

    resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=body)

    assert resp.status_code == 200, resp.get_json()
    row = _row(db, sub_id)
    assert (row.name, row.delivery_address_id, row.payment_method.value) == ("Renamed by admin", address.id, "cash")


@pytest.fixture
def capped_office(db):
    """A customer with two addresses: home, and an office whose PLACE arm of the cash-debt limit is reached
    (a co-worker at the same place owes the limit), so the payment menu offers cash at home but not there."""
    user = make_user(db)
    home = make_address(db, user)
    office = make_address(db, user)
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, office, label="capped-office")
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=Decimal("6000.00"))
    return user, home, office


def _offered(web, address_id):
    menu = web.get(f"/api/v1/payments/methods?context=subscription&delivery_address_id={address_id}")
    assert menu.status_code == 200, menu.get_json()
    return {m["method"] for m in menu.get_json()["data"]["available_methods"]}


@pytest.mark.parametrize("actor", ["customer", "admin"])
def test_moving_to_an_address_whose_menu_drops_the_method_is_refused(
    db, client, admin_headers, capped_office, sample_product, monday, actor
):
    """B8 on an address change (final review Minor 9): the method is re-checked against the NEW address's
    menu, the place arm of the cash-debt limit included. The admin form is refused in the same translated words; the
    customer is asked for a method from the new address's menu, which the refusal carries (spec 2026-10-27 §7)."""
    user, home, office = capped_office
    web = _Web(client.application, client, user)
    sub_id = _create(web, home.id, [{"product_id": sample_product.id, "quantity": 2}], payment_method="cash")
    assert "cash" in _offered(web, home.id) and "cash" not in _offered(web, office.id)

    resp = (
        web.put(f"{BASE}/{sub_id}", {"delivery_address_id": office.id, "expected_state": _state(web, sub_id)})
        if actor == "customer"
        else client.put(
            f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
            json=_admin_form_edit_body(
                db, client, admin_headers, sub_id, "move to the other address",
                {"address": home.id, "other_address": office.id},
            ),
        )
    )

    assert resp.status_code == 400, resp.get_json()
    if actor == "customer":
        data = resp.get_json()["data"]
        assert _refusal_text(resp) in _translated(METHOD_REQUIRED)
        assert data["error_code"] == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED"
        assert "cash" not in [method["method"] for method in data["payment_methods"]]
        assert {method["method"] for method in data["payment_methods"]} == _offered(web, office.id)
    else:
        assert _refusal_text(resp) in _translated(METHOD_NOT_OFFERED)
    row = _row(db, sub_id)
    assert (row.delivery_address_id, row.payment_method.value) == (home.id, "cash")


@pytest.mark.parametrize("actor", ["customer", "admin"])
def test_moving_to_an_address_whose_menu_keeps_the_method_is_accepted(
    db, client, admin_headers, capped_office, sample_product, monday, actor
):
    user, home, office = capped_office
    web = _Web(client.application, client, user)
    sub_id = _create(web, office.id, [{"product_id": sample_product.id, "quantity": 2}], payment_method="click")
    assert "click" in _offered(web, home.id)

    resp = (
        web.put(f"{BASE}/{sub_id}", {"delivery_address_id": home.id, "expected_state": _state(web, sub_id)})
        if actor == "customer"
        else client.put(
            f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
            json=_admin_form_edit_body(
                db, client, admin_headers, sub_id, "move a card subscription to the other address",
                {"address": office.id, "other_address": home.id},
            ),
        )
    )

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).delivery_address_id == home.id


# --------------------------------------------------------------------------- the admin form and a stored slot


@pytest.fixture
def saturday_slot(db):
    """Offered on Saturday only."""
    slot = DeliveryTimeSlot(name="Saturday morning", start_time="10:00", end_time="12:00", is_active=True,
                            available_days=[SATURDAY])
    db.session.add(slot)
    db.session.commit()
    return slot


@pytest.fixture
def saturday_sub(web, user_address, sample_product, saturday_slot, monday):
    """A weekly-Saturday card subscription on the Saturday-only slot."""
    return _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                   delivery_day_of_week=SATURDAY, delivery_time_slot_id=saturday_slot.id)


class TestTheAdminFormAndAStoredSlot:
    """The admin form resends the stored slot on every save, and keeps a stored slot its list no longer offers while
    the schedule is untouched (Subscriptions.js `slotsUnchanged`, re-review G3). That holds only because the update
    judges a slot, and the schedule, on a REAL change (B9). Re-review tests-G3-unchanged-slot-B9-unpinned and
    admin-bot-backend-3."""

    def _rename(self, db, client, admin_headers, sub_id, user_address, saturday_slot):
        body = _admin_form_edit_body(
            db, client, admin_headers, sub_id, "rename a Saturday subscription that has a slot",
            {"address": user_address.id, "slot": saturday_slot.id},
        )
        return client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=body)

    def test_renaming_keeps_a_stored_slot_its_day_is_no_longer_offered_on(
        self, db, client, admin_headers, user_address, saturday_slot, saturday_sub
    ):
        before = _row(db, saturday_sub).next_delivery_date
        saturday_slot.available_days = [0, 1, 2]  # narrowed after the subscription chose it
        db.session.commit()

        resp = self._rename(db, client, admin_headers, saturday_sub, user_address, saturday_slot)

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, saturday_sub)
        assert (row.name, row.delivery_day_of_week, row.delivery_time_slot_id, row.next_delivery_date) == (
            "Renamed by admin", SATURDAY, saturday_slot.id, before
        )

    def test_renaming_keeps_a_stored_slot_that_was_deactivated(
        self, db, client, admin_headers, user_address, saturday_slot, saturday_sub
    ):
        """An admin may deactivate a slot (PUT /admin/delivery/time-slots/<id>); its subscriptions keep it, and an
        unrelated save must not be refused for it (it used to answer 404 "Delivery time slot not found or inactive",
        in English)."""
        before = _row(db, saturday_sub).next_delivery_date
        saturday_slot.is_active = False
        db.session.commit()

        resp = self._rename(db, client, admin_headers, saturday_sub, user_address, saturday_slot)

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, saturday_sub)
        assert (row.name, row.delivery_time_slot_id, row.next_delivery_date) == (
            "Renamed by admin", saturday_slot.id, before
        )

    def test_choosing_a_deactivated_slot_is_still_refused(
        self, db, client, admin_headers, user_address, sample_product, saturday_slot, every_day_slot, monday, web
    ):
        """Only an unchanged slot is spared the check: picking an inactive slot is refused as before."""
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                         delivery_time_slot_id=every_day_slot.id)
        saturday_slot.is_active = False
        db.session.commit()

        resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
                          json={"delivery_time_slot_id": saturday_slot.id})

        assert resp.status_code == 404, resp.get_json()
        assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id

    def test_the_admin_forms_body_that_clears_a_dropped_slot_saves_any_time(
        self, db, client, admin_headers, user_address, saturday_slot, weekday_slot, saturday_sub
    ):
        """Saturday to Monday: the Monday list has no Saturday-only slot, so the admin picks Any time and the form sends
        `delivery_time_slot_id: null`. An explicit null is "Any time" (spec 2026-10-27 §6.1): the body saves, the slot
        is cleared."""
        body = _admin_form_edit_body(
            db, client, admin_headers, saturday_sub,
            "Saturday to Monday on Any time",
            {"address": user_address.id, "slot": saturday_slot.id},
        )
        assert body["delivery_day_of_week"] == 0 and body["delivery_time_slot_id"] is None

        resp = client.put(f"{ADMIN_BASE}/{saturday_sub}", headers=admin_headers, json=body)

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, saturday_sub)
        assert (row.delivery_day_of_week, row.delivery_time_slot_id) == (0, None)


# --------------------------------------------------------------------------- "Any time" (spec 2026-10-27 §6.1-6.3)


class TestAnyTime:
    def _sub(self, web, user_address, sample_product, every_day_slot):
        return _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                       delivery_time_slot_id=every_day_slot.id)

    def test_an_explicit_null_clears_the_slot_on_the_customer_put(
        self, db, web, user_address, sample_product, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)
        before = _row(db, sub_id).next_delivery_date

        resp = web.put(f"{BASE}/{sub_id}", {"delivery_time_slot_id": None, "expected_state": _state(web, sub_id)})

        assert resp.status_code == 200, resp.get_json()
        assert (_row(db, sub_id).delivery_time_slot_id, _row(db, sub_id).next_delivery_date) == (None, before)
        published = _published(web, sub_id)
        assert (published["time_kind"], published["time_label"], published["schedule_sentence"]) == (
            "any_time", "Any time", "Every Thursday, any time"
        )

    def test_a_missing_key_keeps_the_slot(self, db, web, user_address, sample_product, every_day_slot, monday):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)

        resp = web.put(f"{BASE}/{sub_id}", {"name": "Renamed water"})

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert (row.name, row.delivery_time_slot_id) == ("Renamed water", every_day_slot.id)

    def test_null_on_a_subscription_without_a_slot_changes_nothing(
        self, db, web, user_address, sample_product, every_day_slot, monday
    ):
        sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
        (status, *_), before = _put_snapshot(db, sub_id)[0], _put_snapshot(db, sub_id)[1]

        resp = web.put(f"{BASE}/{sub_id}", {"delivery_time_slot_id": None, "expected_state": _state(web, sub_id)})

        assert resp.status_code == 200, resp.get_json()
        # the slot, the frequency, the day and the next delivery: nothing moved
        assert _put_snapshot(db, sub_id)[1] == before

    def test_the_any_time_preview_is_what_the_save_gives(
        self, db, web, user_address, sample_product, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)
        body = {"frequency": "weekly", "delivery_day_of_week": THURSDAY, "delivery_time_slot_id": None}

        preview = web.post(f"{BASE}/{sub_id}/preview", body).get_json()["data"]["preview"]

        assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id and preview["time_kind"] == "any_time"
        assert preview["time_label"] == "Any time"
        assert web.put(f"{BASE}/{sub_id}", {**body, "expected_state": preview["expected_state"]}).status_code == 200
        saved = _published(web, sub_id)
        assert (saved["schedule_sentence"], saved["upcoming_dates"]) == (
            preview["schedule_sentence"], preview["upcoming_dates"]
        )

    def test_the_admin_put_clears_the_slot(
        self, db, web, client, admin_headers, user_address, sample_product, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)

        resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"delivery_time_slot_id": None})

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).delivery_time_slot_id is None
        assert (resp.get_json()["data"]["subscription"]["time_kind"]) == "any_time"
        assert _admin_published(client, admin_headers, sub_id)["time_label"] == "Any time"

    def test_a_null_name_is_refused_and_never_a_500(
        self, db, web, client, admin_headers, user_address, sample_product, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)

        assert web.put(f"{BASE}/{sub_id}", {"name": None}).status_code == 400
        assert client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={"name": None}).status_code == 400
        assert _row(db, sub_id).name == "Home water"

    @pytest.mark.parametrize("field", ["frequency", "delivery_address_id", "auto_renew"])
    def test_the_other_never_null_fields_are_refused_too(
        self, db, web, client, admin_headers, user_address, sample_product, every_day_slot, monday, field
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)

        assert web.put(f"{BASE}/{sub_id}", {field: None}).status_code == 400
        assert client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json={field: None}).status_code == 400


class TestSlotChoiceRequired:
    """`weekday_slot` (Monday to Friday) and `every_day_slot` are both active."""

    def _sub(self, web, user_address, sample_product, slot, **fields):
        return _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                       delivery_time_slot_id=slot.id, **fields)

    def test_a_day_the_stored_slot_does_not_cover_is_refused_until_the_slot_is_chosen(
        self, db, web, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, weekday_slot)
        before = _put_snapshot(db, sub_id)
        implicit = {"delivery_day_of_week": SATURDAY}

        put = web.put(f"{BASE}/{sub_id}", {**implicit, "expected_state": _state(web, sub_id)})
        preview = web.post(f"{BASE}/{sub_id}/preview", implicit)

        for resp in (put, preview):
            assert resp.status_code == 400 and _refusal_text(resp) in _translated(SLOT_CHOICE_REQUIRED), resp.get_json()
        assert _put_snapshot(db, sub_id) == before
        any_time = web.put(
            f"{BASE}/{sub_id}", {**implicit, "delivery_time_slot_id": None, "expected_state": _state(web, sub_id)}
        )
        assert any_time.status_code == 200, any_time.get_json()
        assert (_row(db, sub_id).delivery_day_of_week, _row(db, sub_id).delivery_time_slot_id) == (SATURDAY, None)

    def test_choosing_a_slot_that_covers_the_new_day_saves(
        self, db, web, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, weekday_slot)

        resp = web.put(
            f"{BASE}/{sub_id}",
            {"delivery_day_of_week": SATURDAY, "delivery_time_slot_id": every_day_slot.id,
             "expected_state": _state(web, sub_id)},
        )

        assert resp.status_code == 200, resp.get_json()
        assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id

    def test_moving_to_daily_keeps_a_slot_the_picker_offers_for_daily(
        self, db, web, client, admin_headers, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        """A daily delivery with a slot lands on that slot's days (spec 2026-10-27 §6.4): the slot the daily picker
        offers (`slot_offered_for`, any active slot) is the one the save accepts, customer and admin."""
        sub_id = self._sub(web, user_address, sample_product, weekday_slot)

        implicit = web.put(f"{BASE}/{sub_id}", {"frequency": "daily", "expected_state": _state(web, sub_id)})
        assert implicit.status_code == 200, implicit.get_json()
        assert _row(db, sub_id).delivery_time_slot_id == weekday_slot.id

        explicit = web.put(
            f"{BASE}/{sub_id}",
            {"frequency": "daily", "delivery_time_slot_id": weekday_slot.id, "expected_state": _state(web, sub_id)},
        )
        assert explicit.status_code == 200, explicit.get_json()
        admin = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
                           json={"frequency": "daily", "delivery_time_slot_id": weekday_slot.id})
        assert admin.status_code == 200, admin.get_json()
        assert _row(db, sub_id).delivery_time_slot_id == weekday_slot.id

    def test_a_slot_that_covers_the_new_day_is_kept_without_asking(
        self, db, web, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot)

        resp = web.put(f"{BASE}/{sub_id}", {"delivery_day_of_week": SATURDAY, "expected_state": _state(web, sub_id)})

        assert resp.status_code == 200, resp.get_json()
        row = _row(db, sub_id)
        assert (row.delivery_day_of_week, row.delivery_time_slot_id) == (SATURDAY, every_day_slot.id)

    def test_an_explicit_slot_not_offered_on_the_day_keeps_its_own_refusal(
        self, db, web, client, admin_headers, user_address, sample_product, weekday_slot, every_day_slot, monday
    ):
        sub_id = self._sub(web, user_address, sample_product, every_day_slot, delivery_day_of_week=SATURDAY)

        customer = web.put(
            f"{BASE}/{sub_id}", {"delivery_time_slot_id": weekday_slot.id, "expected_state": _state(web, sub_id)}
        )
        admin = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
                           json={"delivery_time_slot_id": weekday_slot.id})

        for resp in (customer, admin):
            assert resp.status_code == 400 and _refusal_text(resp) in _translated(SLOT_NOT_ON_DAY), resp.get_json()
        assert _row(db, sub_id).delivery_time_slot_id == every_day_slot.id


# --------------------------------------------------------------------------- admin bulk actions


def _bulk(client, admin_headers, action, ids):
    resp = client.post(
        "/api/v1/admin/bulk-actions",
        headers=admin_headers,
        json={"action": action, "target_type": "subscription", "target_ids": ids, "reason": "Bulk test"},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["results"]


class TestBulkActionsUseTheServiceRules:
    def test_cancelling_an_already_cancelled_subscription_is_refused(self, db, client, admin_headers, build):
        sub_id = build("cancelled")
        ended = _row(db, sub_id).end_date

        results = _bulk(client, admin_headers, "cancel", [sub_id])

        assert (results["success_count"], results["failed_count"]) == (0, 1)
        assert results["errors"][0]["error"] in _translated(ALREADY_CANCELLED)
        assert _row(db, sub_id).end_date == ended

    def test_pause_cancels_the_unpaid_upcoming_order_as_every_pause_does(self, db, client, admin_headers, build):
        sub_id = build()

        results = _bulk(client, admin_headers, "pause", [sub_id])

        assert results["success_count"] == 1
        assert _row(db, sub_id).status == SubscriptionStatus.PAUSED
        assert [o.status for o in _orders(db, sub_id)] == [OrderStatus.CANCELLED]
        assert len(_logs(db, sub_id, "paused")) == 1

    def test_pause_of_a_paused_subscription_is_refused(self, db, client, admin_headers, build):
        sub_id = build("paused")

        results = _bulk(client, admin_headers, "pause", [sub_id])

        assert results["failed_count"] == 1
        assert results["errors"][0]["error"] in _translated(PAUSE_NOT_ACTIVE)

    @pytest.mark.parametrize("action, status, logged", [
        ("pause", SubscriptionStatus.PAUSED, ("paused", "Paused by admin")),
        ("cancel", SubscriptionStatus.CANCELLED, ("cancelled", "Cancelled by admin")),
    ])
    def test_no_reason_is_logged_as_no_reason_not_none(self, db, client, admin_headers, build, action, status, logged):
        """Final review Minor 13: the bulk request's reason is optional (`"reason": null` is accepted), and the
        log said "Paused by admin: None"."""
        sub_id = build()

        body = {"action": action, "target_type": "subscription", "target_ids": [sub_id], "reason": None}
        resp = client.post("/api/v1/admin/bulk-actions", headers=admin_headers, json=body)

        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["results"]["success_count"] == 1
        row = _row(db, sub_id)
        assert row.status == status
        log_action, details = logged
        assert [log.details for log in _logs(db, sub_id, log_action)] == [details]
        assert "None" not in (row.pause_reason or "") + (getattr(row, "cancellation_reason", None) or "")

    def test_resume_dates_the_next_delivery_by_the_rule(self, db, client, admin_headers, build, monday):
        sub_id = build("paused")

        results = _bulk(client, admin_headers, "resume", [sub_id])

        assert results["success_count"] == 1
        row = _row(db, sub_id)
        assert row.status == SubscriptionStatus.ACTIVE
        assert row.next_delivery_date == subscription_schedule.first_delivery(row, monday + timedelta(days=2))
        assert len(_logs(db, sub_id, "resumed")) == 1
