"""AgentOrderConfirmationService: the request, the store's answer, the expiry fallback.

Real path throughout: a sales agent opens a visit at a linked outlet and places the
order on the store's behalf (Task 5a). This module owns everything that happens to
that order afterwards — who may confirm it, what the customer bot is asked to show,
and which backstop is allowed to touch it while the store has not answered.
"""

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fnmatch import fnmatch
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.sales_visits import CONFIRMATION_STATUSES, OrderConfirmationRequest
from business_app.models.user import UserAddress
from business_app.services.sales.agent_order_confirmation_service import (
    CONFIRMATION_STATES,
    AgentOrderConfirmationService,
)
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_service import VisitService
from business_app.tasks import sales_agent_tasks
from business_app.tasks.order_tasks import auto_confirm_pending_orders, cancel_abandoned_orders
from business_app.utils import bot_webhook
from business_app.utils.exceptions import ConflictError
from shared.enums import EntitySubtype, OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_order_tasks_auto_confirm import _aged_cash_order
from tests.unit.test_outlet_dedupe import PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user


def _spy(monkeypatch, obj, attr, result=None):
    """Signature-enforcing spy: a wrong-arity or wrong-keyword call raises here
    instead of being silently accepted (a `lambda *a, **k: None` stub would let
    payload drift ship green).

    For a Celery task's `.delay` the signature that carries the payload is the TASK's:
    `Task.delay(self, *args, **kwargs)` binds anything at all, so enforcing IT enforced
    nothing. `run` is the task's own function with `self` already bound on a `bind=True`
    task, which is what every `.delay` target in this file is.
    """
    real = getattr(obj, attr)
    signature = inspect.signature(obj.run if attr == "delay" else real)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(obj, attr, fake)
    return calls


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    user.telegram_id = "777000111"
    db.session.commit()
    return user


@pytest.fixture
def linked_outlet(db, agent):
    """A store with a real customer account AND Telegram — the confirmation path."""
    customer = _customer(db, "+998901112299", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    customer.telegram_id = "555000111"
    db.session.add(
        UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    db.session.commit()
    return OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=customer.id)


def _place_order(agent, outlet, product, *, payment_method="cash", quantity=2):
    """Exactly what the staff bot posts to POST /sales/visits/<id>/order — from the step the
    server's script puts the agent on. Check-in and the shelf count are walked because
    `place_order` is step-guarded (M02); an empty count is a legitimate submission (the product
    here is not on the stock-check list) and it is what moves `current_step` to "order"."""
    visit = VisitService.start(agent.id, outlet.id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)
    VisitService.stock_check(visit, [])
    return VisitService.place_order(
        visit,
        {
            "items": [{"product_id": product.id, "quantity": quantity}],
            "payment_method": payment_method,
            "delivery_date": None,
            "delivery_window_start": None,
            "delivery_window_end": None,
            "delivery_notes": None,
        },
        agent.id,
    )


def test_the_delay_spy_binds_the_tasks_own_signature(app, monkeypatch):
    """`_spy` exists to refuse a wrong-arity call; on `.delay` it refused nothing.

    `Task.delay(self, *args, **kwargs)` accepts literally any call, so binding ITS signature
    made the enforcement vacuous for the two dispatch points this file watches -- exactly the
    `lambda *a, **k: None` failure mode the helper's docstring warns about. The task's own
    `run` carries the payload contract (`self` is already bound on a `bind=True` task).
    """
    _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")

    with pytest.raises(TypeError):
        sales_agent_tasks.push_sales_event.delay(555000111, "agent_order_confirmed")  # payload missing

    with pytest.raises(TypeError):
        sales_agent_tasks.push_agent_order_confirmation.delay()  # order_id missing

    # ...and the real call still goes through untouched.
    sales_agent_tasks.push_sales_event.delay(555000111, "agent_order_confirmed", {"order_id": 1})


def test_the_three_confirmation_states_are_one_published_vocabulary(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """C31/#44: `open_or_confirm`'s answer is rendered by the staff bot as three different lines.

    The states were three bare literals inside one method, so a fourth one (or a rename) would
    have reached the bot as an unrenderable string. The tuple is what the returns are made of --
    they are unpacked from it -- and it is DISTINCT from `CONFIRMATION_STATUSES`, the request
    ROW's statuses, which share the word "confirmed" and do not mean the same thing by it.
    """
    assert CONFIRMATION_STATES == ("auto_confirmed", "pending_confirmation", "confirmed")
    assert set(CONFIRMATION_STATES) != set(CONFIRMATION_STATUSES)
    assert "pending" in CONFIRMATION_STATUSES and "pending" not in CONFIRMATION_STATES

    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    # (1) somebody to ask
    _order, pending_state = _place_order(agent, linked_outlet, sample_product)
    assert pending_state in CONFIRMATION_STATES and pending_state == "pending_confirmation"

    # (2) nobody to ask — the store has no Telegram account
    customer = linked_outlet.user
    customer.telegram_id = None
    db.session.commit()
    VisitService.close(
        VisitService.current(agent.id),
        outcome=None,
        no_order_reason=None,
        notes=None,
        next_visit_at=None,
        dm_present=None,
    )
    _order2, confirmed_state = _place_order(agent, linked_outlet, sample_product)
    assert confirmed_state in CONFIRMATION_STATES and confirmed_state == "confirmed"


def test_linked_store_gets_one_pending_request_and_one_push(app, db, agent, linked_outlet, sample_product, monkeypatch):
    delays = _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")

    order, state = _place_order(agent, linked_outlet, sample_product)

    assert state == "pending_confirmation"
    assert order.status is OrderStatus.PENDING
    rows = OrderConfirmationRequest.query.filter_by(order_id=order.id).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.status, row.outlet_id, row.responded_at) == ("pending", linked_outlet.id, None)
    assert len(row.request_id) == 16
    assert app.config["SALES_CONFIRMATION_TTL_HOURS"] == 3
    ttl = timedelta(hours=app.config["SALES_CONFIRMATION_TTL_HOURS"])
    assert abs((row.expires_at - row.requested_at) - ttl) < timedelta(seconds=5)
    assert delays == [((order.id,), {})]


def test_the_stock_stays_held_for_as_long_as_the_store_has_to_answer(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """M28: the 3 h question outlived the 30 min hold behind it.

    `create_order` reserves these units for `INVENTORY_RESERVATION_TTL` -- 30 minutes, the life
    of a checkout somebody is still typing. This order then waits `SALES_CONFIRMATION_TTL_HOURS`
    for a shopkeeper and is CONFIRMED either way, which decrements stock. For the hours in
    between the water was held by nobody: any other channel could sell it, and the store's
    confirmed order had nothing behind it. The hold is re-stamped with the window's OWN number,
    from the same config that sets `expires_at`, so the two can never be different lengths.
    """
    from business_app.services.inventory_service import InventoryService

    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    holds = _spy(
        monkeypatch,
        InventoryService,
        "reserve_inventory",
        result={"success": True, "reservations": [], "expires_at": None},
    )
    extensions = _spy(
        monkeypatch,
        InventoryService,
        "extend_reservations",
        result={"success": True, "extended_products": [], "lapsed_products": []},
    )

    order, state = _place_order(agent, linked_outlet, sample_product, quantity=2)

    assert state == "pending_confirmation"
    window_seconds = int(app.config["SALES_CONFIRMATION_TTL_HOURS"]) * 3600
    # create_order takes the default TTL; the extension names the window's length explicitly.
    assert [call[1].get("ttl") for call in holds] == [None]
    assert [call[1].get("ttl") for call in extensions] == [window_seconds]
    assert extensions[0][1]["order_id"] == order.id
    assert extensions[0][1]["items"] == [{"product_id": sample_product.id, "quantity": 2}]


def test_the_hold_covers_exactly_the_lines_create_order_itself_held(
    app, db, agent, linked_outlet, sample_product, sample_category, monkeypatch
):
    """The re-stamp asks the SAME question `create_order` asked: which lines gate this order?

    `create_order` reserves `self._stock_gated_items(items, is_cash=...)`, and a cash order
    draws no marking code, so a marking-code product's pool does not constrain it
    (order_service.py:1476-1493). Holding every line regardless did two wrong things at once:
    it stamped a phantom 3 h reservation on a pool `confirm_reservations` never looks at, and
    it made the whole re-stamp all-or-nothing against a code pool that has nothing to do with
    this order -- so a short pool silently cost the PLAIN product its extended hold too.
    """
    from business_app.models.product import Product, ProductFiscalProfile
    from business_app.services.inventory_service import InventoryService

    # `Product.requires_marking_codes` is a read-only property over the fiscal profile, so the
    # product is made marking-code-backed the only way production can make one.
    coded = Product(
        name="Coded 19L",
        category_id=sample_category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        stock_quantity=50,
        is_active=True,
    )
    db.session.add(coded)
    db.session.flush()
    db.session.add(ProductFiscalProfile(product_id=coded.id, requires_marking_codes=True))
    db.session.commit()
    assert coded.requires_marking_codes is True

    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    holds = _spy(
        monkeypatch,
        InventoryService,
        "reserve_inventory",
        result={"success": True, "reservations": [], "expires_at": None},
    )
    extensions = _spy(
        monkeypatch,
        InventoryService,
        "extend_reservations",
        result={"success": True, "extended_products": [], "lapsed_products": []},
    )

    visit = VisitService.start(agent.id, linked_outlet.id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)
    VisitService.stock_check(visit, [])
    order, state = VisitService.place_order(
        visit,
        {
            "items": [
                {"product_id": sample_product.id, "quantity": 2},
                {"product_id": coded.id, "quantity": 3},
            ],
            "payment_method": "cash",
            "delivery_date": None,
            "delivery_window_start": None,
            "delivery_window_end": None,
            "delivery_notes": None,
        },
        agent.id,
    )

    assert state == "pending_confirmation"
    # create_order's own hold already dropped the marking-code line; the extension matches it.
    assert holds[0][1]["items"] == [{"product_id": sample_product.id, "quantity": 2}]
    assert extensions[0][1]["items"] == [{"product_id": sample_product.id, "quantity": 2}]
    assert extensions[0][1]["ttl"] == int(app.config["SALES_CONFIRMATION_TTL_HOURS"]) * 3600


def test_a_raising_stock_hold_never_costs_the_agent_the_order(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """The hold is best effort, and an EXCEPTION is as best-effort as a False.

    A Redis outage raises straight out of the client -- through `open_or_confirm`, into
    `place_order`, AFTER the order and the request row are committed and BEFORE the push is
    enqueued. The agent's POST became a 500 over an order that really exists, and the store
    was never asked, which is strictly worse than the 30-minute hold this call was trying to
    improve on.
    """
    from business_app.services.inventory_service import InventoryService

    delays = _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    calls = []

    def blow_up_on_the_extension(self, *args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("redis is down")

    monkeypatch.setattr(InventoryService, "extend_reservations", blow_up_on_the_extension)

    order, state = _place_order(agent, linked_outlet, sample_product, quantity=2)

    assert state == "pending_confirmation"
    assert len(calls) == 1
    assert order.status is OrderStatus.PENDING
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).one().status == "pending"
    # The store is still asked: the push is the whole point of the branch the hold sits in.
    assert delays == [((order.id,), {})]


class _RedisThatDropsMidExtension:
    """In-memory redis whose connection dies part-way through the window extension.

    The operations a reservation round-trip uses, plus `delete`, which is RECORDED as
    well as performed so the assertion can name who deleted what: the whole point of
    the fix is that the extension deletes nothing at all. The drop is aimed at one
    product's key under the LONG ttl, so `create_order`'s own 30-minute hold — written
    through the same fake, at the default ttl — is unaffected and is what the order has
    left when the extension gives up.
    """

    def __init__(self, long_ttl, fail_product_id):
        self.values = {}
        self.ttls = {}
        self.hashes = {}
        self.deleted = []
        self._long_ttl = long_ttl
        self._fail_key_tail = f":{fail_product_id}"

    def _stamp(self, key, ttl):
        if ttl == self._long_ttl and key.endswith(self._fail_key_tail):
            raise ConnectionError("redis went away")
        self.ttls[key] = ttl

    def setex(self, key, ttl, value):
        self._stamp(key, ttl)
        self.values[key] = str(value)

    def get(self, key):
        return self.values.get(key)

    def keys(self, pattern):
        return [key for key in (*self.values, *self.hashes) if fnmatch(key, pattern)]

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def expire(self, key, ttl):
        if key not in self.values and key not in self.hashes:
            return False
        self._stamp(key, ttl)
        return True

    def delete(self, *keys):
        self.deleted.extend(keys)
        for key in keys:
            self.values.pop(key, None)
            self.hashes.pop(key, None)


def test_a_fault_mid_extension_never_takes_back_the_hold_create_order_left(
    app, db, agent, linked_outlet, sample_product, sample_category, monkeypatch
):
    """C00: the call that exists to LENGTHEN the hold could destroy it outright.

    `reserve_inventory`'s failure path is an order-scoped blanket delete — any exception
    inside its loop calls `release_reservations(order_id)`, which cannot tell the keys it
    just wrote from the 30-minute keys `create_order` wrote milliseconds earlier. So a
    Redis drop between two lines left the order with NO hold at all: strictly worse than
    the hold the extension was improving on, and the opposite of what the docstring
    promised. The extension now stamps the EXISTING keys and never releases, so the worst
    case is the 30 minutes `create_order` left, never less.
    """
    from business_app.models.product import Product
    from business_app.services.inventory_service import get_inventory_service
    from shared.redis_keyspace import RedisKeyspace

    plain = Product(
        name="Plain 10L",
        category_id=sample_category.id,
        size="10L",
        base_price=Decimal("9000.00"),
        stock_quantity=50,
        is_active=True,
    )
    db.session.add(plain)
    db.session.commit()

    delays = _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    window_seconds = int(app.config["SALES_CONFIRMATION_TTL_HOURS"]) * 3600
    fake = _RedisThatDropsMidExtension(long_ttl=window_seconds, fail_product_id=plain.id)
    inventory = get_inventory_service()
    monkeypatch.setattr(inventory, "redis_client", fake)
    monkeypatch.setattr(type(inventory), "_get_redis_client", lambda self: fake)

    visit = VisitService.start(agent.id, linked_outlet.id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)
    VisitService.stock_check(visit, [])
    order, state = VisitService.place_order(
        visit,
        {
            "items": [
                {"product_id": sample_product.id, "quantity": 2},
                {"product_id": plain.id, "quantity": 3},
            ],
            "payment_method": "cash",
            "delivery_date": None,
            "delivery_window_start": None,
            "delivery_window_end": None,
            "delivery_notes": None,
        },
        agent.id,
    )

    assert state == "pending_confirmation"
    assert delays == [((order.id,), {})]

    first_key = RedisKeyspace.inventory_reservation(order.id, sample_product.id)
    dropped_key = RedisKeyspace.inventory_reservation(order.id, plain.id)
    # Nothing was released — not the line the fault hit, and not the line already extended.
    assert fake.deleted == []
    assert fake.values[first_key] == "2" and fake.values[dropped_key] == "3"
    # The line the extension reached carries the window; the one it did not still carries
    # the 30 minutes `create_order` gave it.
    assert fake.ttls[first_key] == window_seconds
    assert fake.ttls[dropped_key] == inventory.reservation_ttl


def test_returning_cash_customer_is_auto_confirmed_without_a_request(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """The instant-COD block already confirmed it — asking the store to confirm a
    CONFIRMED order would put two dead buttons in its chat."""
    delays = _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    db.session.add(
        Order(
            user_id=linked_outlet.user_id,
            order_number="WB_000999_01",
            status=OrderStatus.DELIVERED,
            subtotal=15000,
            total_amount=15000,
            payment_method=PaymentMethod.CASH,
        )
    )
    db.session.commit()

    order, state = _place_order(agent, linked_outlet, sample_product)

    assert state == "auto_confirmed"
    assert order.status is OrderStatus.CONFIRMED
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).count() == 0
    assert delays == []


def test_auto_confirm_sweep_skips_an_order_awaiting_store_confirmation(
    app, db, agent, linked_outlet, sample_product, sample_user, monkeypatch
):
    """A/B on one sweep: two aged PENDING cash orders, one of which the store still owns."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    awaiting, state = _place_order(agent, linked_outlet, sample_product)
    assert state == "pending_confirmation"
    # Nothing but the pending request distinguishes it from `plain`: same status,
    # same age, same cash payment the sweep looks for.
    assert awaiting.payment is not None and awaiting.payment.payment_method is PaymentMethod.CASH
    awaiting.created_at = datetime.now(UTC) - timedelta(hours=2)
    db.session.commit()
    plain = _aged_cash_order(db, sample_user, delivery_date=date.today() + timedelta(days=3))

    result = auto_confirm_pending_orders()

    db.session.refresh(awaiting)
    db.session.refresh(plain)
    assert awaiting.status is OrderStatus.PENDING
    assert plain.status is OrderStatus.CONFIRMED
    assert result["confirmed_count"] == 1


def test_cancel_abandoned_sweep_also_leaves_an_order_the_store_still_owns(
    app, db, agent, linked_outlet, sample_product, sample_user, monkeypatch
):
    """C29: D5's rule reached only ONE of the two beat sweeps that scan PENDING orders.

    `cancel_abandoned_orders` does not confirm — it CANCELS, releases the reservation and tells
    the store "no payment received within 24 hours" about a COD order nobody was ever going to
    pay online. An agent order's Payment row is PENDING by construction, so every agent order
    still awaiting its shop matched. A/B on one sweep: two aged PENDING cash orders, one of
    which the store still owns.
    """
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    awaiting, state = _place_order(agent, linked_outlet, sample_product)
    assert state == "pending_confirmation"
    assert awaiting.payment is not None and awaiting.payment.status is not PaymentStatus.COMPLETED
    awaiting.created_at = datetime.now(UTC) - timedelta(hours=25)
    db.session.commit()
    plain = _aged_cash_order(db, sample_user, delivery_date=date.today() + timedelta(days=3))
    plain.created_at = datetime.now(UTC) - timedelta(hours=25)
    db.session.commit()

    cancel_abandoned_orders()

    db.session.refresh(awaiting)
    db.session.refresh(plain)
    assert awaiting.status is OrderStatus.PENDING
    assert plain.status is OrderStatus.CANCELLED

    # ...and once the request is no longer pending, nothing shields the order any more: the
    # exclusion is the OPEN request, not "was placed by an agent".
    row = OrderConfirmationRequest.query.filter_by(order_id=awaiting.id).one()
    row.status = "expired"
    db.session.commit()

    cancel_abandoned_orders()

    db.session.refresh(awaiting)
    assert awaiting.status is OrderStatus.CANCELLED


def test_expiry_sweep_marks_the_request_expired_and_confirms_the_order(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()

    expired = AgentOrderConfirmationService.expire_due(now=row.expires_at + timedelta(minutes=1))

    assert expired == 1
    db.session.refresh(row)
    db.session.refresh(order)
    assert row.status == "expired"
    assert row.responded_at is not None
    assert order.status is OrderStatus.CONFIRMED
    notes = [
        h.notes
        for h in OrderStatusHistory.query.filter_by(order_id=order.id, new_status=OrderStatus.CONFIRMED).all()
    ]
    assert "Confirmed after confirmation timeout — delivery confirms" in notes


def test_expiry_sweep_leaves_a_request_that_is_not_due_alone(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()

    assert AgentOrderConfirmationService.expire_due(now=row.expires_at - timedelta(minutes=1)) == 0

    db.session.refresh(row)
    db.session.refresh(order)
    assert row.status == "pending"
    assert order.status is OrderStatus.PENDING


def test_customer_confirm_confirms_the_order_creates_the_delivery_and_pushes_the_agent(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")

    result = AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "confirm", None)

    assert result["confirmation"] == {"status": "confirmed"}
    assert result["order"]["id"] == order.id
    assert result["order"]["status"] == OrderStatus.CONFIRMED.value
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    assert (row.status, row.response_channel) == ("confirmed", "telegram")
    assert row.responded_at is not None
    db.session.refresh(order)
    assert order.status is OrderStatus.CONFIRMED
    assert Delivery.query.filter_by(order_id=order.id).count() == 1
    assert pushes == [
        (
            (
                777000111,
                "agent_order_confirmed",
                {
                    "outlet_id": linked_outlet.id,
                    "outlet_name": "Bahor market",
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "reason": None,
                },
            ),
            {},
        )
    ]


def test_the_answer_reaches_the_agent_who_placed_the_order_not_the_outlets_own(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """C06/#09: the store's answer is news for whoever stood in the shop.

    Both agents pass `get_for_agent` -- one onboarded the store, a manager later assigned it to
    the other -- so a covering agent really can place the order. Routing the push by the outlet's
    assignment sent "Bahor market confirmed your order" to an agent who never placed one, while
    the agent waiting at the counter heard nothing.
    """
    stand_in = make_sales_agent_user(db, phone="+998901234599", staff_roles=["sales_agent"])
    stand_in.telegram_id = "777000222"
    linked_outlet.onboarded_by_user_id = stand_in.id
    db.session.commit()
    assert linked_outlet.assigned_agent_user_id == agent.id and agent.telegram_id == "777000111"

    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(stand_in, linked_outlet, sample_product)
    assert order.created_by_staff_id == stand_in.id
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")

    AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "confirm", None)

    assert [call[0][0] for call in pushes] == [777000222]


def test_customer_decline_cancels_the_order_and_carries_the_reason_to_the_agent(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")

    result = AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "decline", "Bugun pul yo'q")

    assert result["confirmation"] == {"status": "declined"}
    assert result["order"]["status"] == OrderStatus.CANCELLED.value
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    assert (row.status, row.decline_reason) == ("declined", "Bugun pul yo'q")
    db.session.refresh(order)
    assert order.status is OrderStatus.CANCELLED
    assert pushes == [
        (
            (
                777000111,
                "agent_order_declined",
                {
                    "outlet_id": linked_outlet.id,
                    "outlet_name": "Bahor market",
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "reason": "Bugun pul yo'q",
                },
            ),
            {},
        )
    ]


def test_a_decline_with_no_reason_uses_the_documented_default(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """Spec *Failure modes*: a wordless decline still cancels with the documented reason
    `customer_declined_agent_order`. Both other decline tests pass a reason, so without this
    the literal could be renamed — or fall through to None — and the suite would stay green."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")

    AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "decline", None)

    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    assert (row.status, row.decline_reason) == ("declined", None)
    db.session.refresh(order)
    assert order.status is OrderStatus.CANCELLED
    # `cancel_order(reason=...)` lands the reason on the CANCELLED status-history row
    # (`order_service.py:1115` passes it to `update_order_status` as `notes`).
    cancelled = (
        OrderStatusHistory.query.filter_by(order_id=order.id, new_status=OrderStatus.CANCELLED)
        .order_by(OrderStatusHistory.id.desc())
        .first()
    )
    assert cancelled.notes == "customer_declined_agent_order"
    # The agent is told the store declined, with no words to quote.
    assert pushes[0][0][1] == "agent_order_declined"
    assert pushes[0][0][2]["reason"] is None


def test_answering_twice_is_refused(app, db, agent, linked_outlet, sample_product, monkeypatch):
    """Telegram re-delivers a tap; the second answer must not re-cancel or re-confirm."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "confirm", None)

    with pytest.raises(ConflictError) as excinfo:
        AgentOrderConfirmationService.respond(order.id, linked_outlet.user_id, "decline", "changed my mind")

    assert excinfo.value.error_code == "SALES_CONFIRMATION_NOT_PENDING"
    db.session.refresh(order)
    assert order.status is OrderStatus.CONFIRMED


def test_push_task_posts_the_proposal_with_the_request_id(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """The customer bot dedups on X-Request-ID, so the row's request_id must be
    BOTH the header id and a body field — the callback quotes it back."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    calls = _spy(monkeypatch, sales_agent_tasks, "trigger_bot_webhook", result={"success": True})

    assert sales_agent_tasks.push_agent_order_confirmation.run(order.id) == {
        "success": True,
        "order_id": order.id,
        "request_id": row.request_id,
    }

    assert len(calls) == 1
    (endpoint, payload), kwargs = calls[0]
    assert endpoint == "/internal/agent-order-proposed"
    assert kwargs == {"request_id": row.request_id}
    assert payload == {
        "telegram_id": 555000111,
        "order_id": order.id,
        "order_number": order.order_number,
        "request_id": row.request_id,
        "agent_name": "Sardor Agent",
        "items": [{"name": "Pure Water 19L", "qty": 2}],
        "total": float(order.total_amount),
        "delivery_date": None,
    }


def test_the_push_task_sends_the_request_id_as_the_x_request_id_header(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """M26: the dedup contract the customer bot rests on is a HEADER, not a kwarg.

    The task side is pinned above (`kwargs == {"request_id": row.request_id}`), and the bot
    side dedups on `X-Request-ID` -- but NOTHING proved the helper turns one into the other.
    A rename of `trigger_bot_webhook`'s keyword-only argument, or a default that quietly won
    over it, would leave the id in the body only, and a retried push would put two sets of
    live Confirm/Decline buttons in the store's chat with every test still green. Only the
    transport is faked here: the real task calls the real helper.
    """
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    sent = []

    class _FakeResponse:
        status = 200

        async def json(self):
            return {"success": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, data=None, headers=None, timeout=None):
            sent.append({"url": url, "body": json.loads(data), "headers": headers})
            return _FakeResponse()

    monkeypatch.setattr(
        bot_webhook,
        "aiohttp",
        SimpleNamespace(ClientSession=_FakeSession, ClientTimeout=lambda total=None: total),
    )
    # `trigger_bot_webhook` installs its own event loop and closes it; leave the thread's
    # loop as it was found so a later async test in this xdist worker still has a live one.
    monkeypatch.setattr(bot_webhook.asyncio, "set_event_loop", lambda _loop: None)

    assert sales_agent_tasks.push_agent_order_confirmation.run(order.id) == {
        "success": True,
        "order_id": order.id,
        "request_id": row.request_id,
    }

    assert len(sent) == 1
    assert sent[0]["url"] == f"{app.config['BOT_WEBHOOK_URL']}/internal/agent-order-proposed"
    assert sent[0]["headers"]["X-Request-ID"] == row.request_id
    assert sent[0]["body"]["request_id"] == row.request_id
    assert sent[0]["body"]["order_number"] == order.order_number

    # A retry of the SAME task sends the SAME id -- that, and only that, is what collapses
    # two deliveries of one proposal into one message in the store's chat.
    sales_agent_tasks.push_agent_order_confirmation.run(order.id)
    assert [call["headers"]["X-Request-ID"] for call in sent] == [row.request_id, row.request_id]


def test_push_task_does_not_retry_when_the_bot_webhook_is_unconfigured(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """Spec failure mode: no bot URL is a permanent condition. Retrying it burns
    the queue; the expiry sweep still confirms the order, so nothing is stranded."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    _spy(
        monkeypatch,
        sales_agent_tasks,
        "trigger_bot_webhook",
        result={"success": False, "message": "Bot webhook URL not configured"},
    )

    assert sales_agent_tasks.push_agent_order_confirmation.run(order.id) == {
        "success": False,
        "reason": "bot_webhook_unconfigured",
        "order_id": order.id,
    }
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).one().status == "pending"


def test_push_task_retries_a_transient_webhook_failure(
    app, db, agent, linked_outlet, sample_product, monkeypatch
):
    """The OTHER arm of the same branch (spec: "Customer-bot webhook down | task retries").

    Only the permanent "not configured" case was pinned; an edit that turned the transient
    arm into a silent `return` would strand the store's proposal and stay green."""
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    order, _ = _place_order(agent, linked_outlet, sample_product)
    _spy(
        monkeypatch,
        sales_agent_tasks,
        "trigger_bot_webhook",
        result={"success": False, "message": "connection refused"},
    )
    retries = []

    def fake_retry(*args, **kwargs):
        inspect.signature(sales_agent_tasks.push_agent_order_confirmation.retry).bind(*args, **kwargs)
        retries.append((args, kwargs))
        raise Retry()

    monkeypatch.setattr(sales_agent_tasks.push_agent_order_confirmation, "retry", fake_retry)

    with pytest.raises(Retry):
        sales_agent_tasks.push_agent_order_confirmation.run(order.id)

    assert len(retries) == 1
    assert isinstance(retries[0][1]["exc"], RuntimeError)
    assert "connection refused" in str(retries[0][1]["exc"])
    # The row is untouched, so the retry (or the expiry sweep) still has work to do.
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).one().status == "pending"
