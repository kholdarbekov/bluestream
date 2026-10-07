"""The same-day staff approval hold (compensation spec C14, §4.18), driven the way people drive it.

An agent's second or later order at one outlet on one local day waits PENDING for a manager or an
admin (I-21). Each test goes through the routes the staff bot, the admin UI and the customer bot
call, with the bodies they send. Only external I/O is mocked: Celery publishes (bound against each
task's own `run` signature), Telegram, and the payment gateway's word that a payment completed.

Wall clock. `create_order` stamps `created_at` from the wall clock and the rule reads the LOCAL
day of `place_order`'s own `now_local`, so every assertion here holds on any real date. Two tests
freeze a clock (T-HOLD-7's midnight edge, Review Focus 1), and two backdate `created_at` (T-HOLD-3's
25 hours, T-HOLD-7's yesterday order): the plan's declared exceptions to the builder rule.
"""

import inspect
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import event

from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_pay import SalesPayLedgerLine
from business_app.models.sales_visits import AgentOrderApproval, OrderConfirmationRequest
from business_app.models.translation import Translation
from business_app.models.user import UserAddress
from business_app.serializers.sales_serializers import _iso, serialize_order_brief, serialize_order_placed
from business_app.services import inventory_service as inventory_service_module
from business_app.services import order_schedule_service as order_schedule_service_module
from business_app.services.inventory_service import InventoryService, get_inventory_service
from business_app.services.notification_service import NotificationService
from business_app.services.order_service import ADMIN_REASON_MAX_LENGTH, CUSTOMER_CANCEL_PAID, OrderService
from business_app.services.sales.agent_order_approval_service import ORDER_APPROVALS_PATH
from business_app.services.sales.exception_feed_service import ExceptionFeedService
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.tasks import sales_agent_tasks
from business_app.tasks.inventory_tasks import cleanup_expired_inventory_reservations
from business_app.tasks.order_tasks import auto_confirm_pending_orders, cancel_abandoned_orders
from business_app.tasks.payment_tasks import process_payment_confirmation
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.local_windows import local_date
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import EntitySubtype, OrderStatus, PaymentMethod, PaymentStatus, UserRole
from shared.redis_keyspace import RedisKeyspace
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_APPROVED, SALES_EVENT_AGENT_ORDER_REJECTED
from tests.integration.sales_pay_builders import (
    APPROVALS,
    deliver,
    freeze_local_now,
    make_order,
    place_visit_order,
)
from tests.integration.test_scheduled_order_release_e2e import driver_on_shift  # noqa: F401  (fixture)
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, _create
from tests.integration.test_staff_sales_visits_api import (
    WORKPLACE_PAYLOAD,
    _approved_outlet,
    _fund_business_account,
)
from tests.unit.test_order_tasks_auto_confirm import _aged_cash_order
from tests.unit.test_outlet_dedupe import FAR, PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration

ADMIN_ORDERS = "/api/v1/admin/orders"
HELD = "awaiting_staff_approval"
TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
ALERT_SUBJECT = "staff.notification.subject.agent_order_awaiting_approval"
ALERT_CONTENT = "staff.notification.content.agent_order_awaiting_approval"


def _spy_task(monkeypatch, task, trail=None, label=None):
    """Record `task.delay`, bound against the task's own `run` (`Task.delay` binds anything).

    Patched through the instance `__dict__` (`_patch_task_publish`), never `setattr`, so no stale
    no-op outlives the test. With a `trail`, each publish appends `label`, which a commit listener
    on the same list orders against the commits.
    """
    signature = inspect.signature(task.run)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append((args, kwargs))
        if trail is not None:
            trail.append(label)

    monkeypatch.setitem(vars(task), "delay", fake)
    return calls


@pytest.fixture
def spies(monkeypatch):
    trail = []
    return SimpleNamespace(
        trail=trail,
        questions=_spy_task(monkeypatch, sales_agent_tasks.push_agent_order_confirmation),
        pushes=_spy_task(monkeypatch, sales_agent_tasks.push_sales_event, trail, "push"),
        alerts=_spy_task(
            monkeypatch, sales_agent_tasks.notify_managers_agent_order_awaiting_approval, trail, "alert"
        ),
    )


@pytest.fixture
def commit_trail(db, spies):
    """`spies.trail`, with "commit" appended on every session commit."""

    def _on_commit(_session):
        spies.trail.append("commit")

    event.listen(db.session, "after_commit", _on_commit)
    yield spies.trail
    event.remove(db.session, "after_commit", _on_commit)


def _items(product, quantity=4):
    return [{"product_id": product.id, "quantity": quantity}]


def _store(client, db, agent_headers, operator_headers, *, telegram_id=None, payload=None):
    """An activated outlet (agent-created, operator-approved) with its customer account and
    address, re-read as the model. `telegram_id` links the store to the customer bot."""
    data = _approved_outlet(client, agent_headers, operator_headers, payload)
    outlet = db.session.get(Outlet, data["id"])
    if telegram_id is not None:
        outlet.user.telegram_id = telegram_id
        db.session.commit()
    return outlet


def _make_returning(db, outlet):
    """One DELIVERED order on the store's account, the trust `create_order`'s instant-COD block
    reads (the `test_a_returning_store_is_auto_confirmed…` precedent)."""
    db.session.add(
        Order(
            user_id=outlet.user_id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("90000"),
            total_amount=Decimal("90000"),
            delivery_address_id=outlet.address_id,
            order_source="web",
        )
    )
    db.session.commit()


def _hold_of(order_id):
    return AgentOrderApproval.query.filter_by(order_id=order_id).one_or_none()


def _token_headers(app, user_id, role=None):
    """A JWT for `user_id`; `role` adds the claim `manager_or_higher_required` reads."""
    with app.app_context():
        token = create_access_token(
            identity=str(user_id), additional_claims={"role": role} if role is not None else None
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _seed_backend_rows(db, *keys):
    """The rows `seed_backend_translations.py` writes for `keys`, so the rendered copy is what a
    manager reads (an unseeded key renders as itself)."""
    for key in keys:
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category="staff_notification"))
    db.session.commit()


@pytest.fixture
def held(client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, sample_product, spies):
    """A grocery store without Telegram and two cash orders from one agent today: the first went
    straight to the pool ("confirmed"), the second waits for a manager. The agent has a staff-bot
    chat, so a decision's outcome push has somewhere to go: linked AFTER the store's activation,
    whose own `outlet_approved` push is not a decision's and would otherwise sit on the spy."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    sales_agent_user.telegram_id = "777000111"
    db.session.commit()
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")
    assert first["confirmation"] == {"state": "confirmed"}
    assert second["confirmation"] == {"state": HELD}
    return SimpleNamespace(
        outlet=outlet,
        agent=sales_agent_user,
        first_id=first["order"]["id"],
        first_number=first["order"]["order_number"],
        order_id=second["order"]["id"],
        order_number=second["order"]["order_number"],
    )


# ---------------------------------------------------------------- T-HOLD-1, T-HOLD-2: placing


@pytest.mark.parametrize(
    "rail,telegram,returning,expected",
    [
        ("cash", True, False, "pending_confirmation"),
        ("cash", True, True, "auto_confirmed"),
        ("business_account", False, False, "auto_confirmed"),
        ("cash", False, False, "confirmed"),
    ],
    ids=["telegram-store", "returning-store", "business-account", "no-telegram-store"],
)
def test_t_hold_1_the_first_order_of_the_day_is_unchanged(
    rail, telegram, returning, expected, client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    """T-HOLD-1: the first agent order at an outlet today opens no hold and takes exactly the path
    it took before C14, for all four placement answers. A hold leaking into the first order would
    show here as a changed state, not as an order quietly waiting in a queue nobody watches."""
    outlet = _store(
        client,
        db,
        sales_agent_auth_headers,
        operator_auth_headers,
        telegram_id="555000111" if telegram else None,
        payload=WORKPLACE_PAYLOAD if rail == "business_account" else None,
    )
    if rail == "business_account":
        _fund_business_account(db, outlet.user_id, sample_product)
    if returning:
        _make_returning(db, outlet)

    placed = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), rail)

    order_id = placed["order"]["id"]
    asked = expected == "pending_confirmation"
    assert placed["confirmation"] == {"state": expected}
    assert _hold_of(order_id) is None
    assert spies.alerts == []
    assert OrderConfirmationRequest.query.filter_by(order_id=order_id).count() == (1 if asked else 0)
    assert spies.questions == ([((order_id,), {})] if asked else [])


def test_t_hold_2_the_second_order_of_the_day_waits_for_a_manager(
    client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, commit_trail
):
    """T-HOLD-2: the second order at a Telegram store is neither asked nor confirmed; it waits."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers, telegram_id="555000111")
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    assert first["confirmation"] == {"state": "pending_confirmation"}
    commit_trail.clear()

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")

    held_id = second["order"]["id"]
    assert second["confirmation"] == {"state": HELD}
    assert second["order"]["status"] == "pending"
    assert db.session.get(Order, held_id).status is OrderStatus.PENDING
    row = _hold_of(held_id)
    assert (row.status, row.earlier_order_ids, row.outlet_id, row.agent_user_id) == (
        "pending",
        [first["order"]["id"]],
        outlet.id,
        sales_agent_user.id,
    )
    assert (row.decided_at, row.decided_by_user_id, row.reason) == (None, None, None)
    assert row.requested_at is not None
    # The store is not asked: no request row and no customer-bot push for the held order. The one
    # question on the spy is the FIRST order's.
    assert OrderConfirmationRequest.query.filter_by(order_id=held_id).count() == 0
    assert spies.questions == [((first["order"]["id"],), {})]
    # One manager alert, published after the commit that made the hold row readable by the task.
    assert spies.alerts == [((held_id,), {})]
    assert commit_trail[commit_trail.index("alert") - 1] == "commit"


def test_m4_a_broker_error_on_the_managers_alert_never_answers_500_over_a_committed_hold(
    client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, monkeypatch
):
    """Final-review M4. The hold row and the order are committed before the managers' alert is
    queued, so a broker that refuses the publish must not turn the placement into a 500 (a retry
    would get 409 "already ordered"), and must not skip the AGENT_ORDER_CREATED activity row.
    The queue and the nav badge still show the held order."""
    from business_app.models.staff import StaffActivityLog
    from shared.staff_constants import STAFF_ACTIONS

    task = sales_agent_tasks.notify_managers_agent_order_awaiting_approval
    signature = inspect.signature(task.run)

    def broker_down(*args, **kwargs):
        signature.bind(*args, **kwargs)
        raise ConnectionError("broker unreachable")

    monkeypatch.setitem(vars(task), "delay", broker_down)
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")

    held_id = second["order"]["id"]
    assert second["confirmation"] == {"state": HELD}
    assert _hold_of(held_id).status == "pending"
    created = StaffActivityLog.query.filter_by(
        action=STAFF_ACTIONS["AGENT_ORDER_CREATED"], entity_type="order", entity_id=held_id
    ).all()
    assert [(row.user_id, row.metadata_["confirmation_state"]) for row in created] == [(sales_agent_user.id, HELD)]


def test_t_hold_2_the_alert_reaches_every_admin_and_manager_in_app_with_no_amount(
    db, held, admin_user, manager_user, monkeypatch
):
    """T-HOLD-2, the task half: one IN_APP SYSTEM_ALERT per active admin and manager, naming the
    agent, the outlet, the order number and the queue's path, and no money (C11)."""
    _seed_backend_rows(db, ALERT_SUBJECT, ALERT_CONTENT)
    in_app = _spy(monkeypatch, NotificationService, "send_notification")

    result = sales_agent_tasks.notify_managers_agent_order_awaiting_approval.run(held.order_id)

    assert result == {"success": True, "order_id": held.order_id, "managers": 2}
    assert sorted(kwargs["user_id"] for _args, kwargs in in_app) == sorted([admin_user.id, manager_user.id])
    total = db.session.get(Order, held.order_id).total_amount
    for _args, kwargs in in_app:
        assert kwargs["channels"] == [NotificationChannel.IN_APP]
        assert kwargs["notification_type"] == NotificationType.SYSTEM_ALERT
        assert kwargs["template_data"] == {
            "order_id": held.order_id,
            "order_number": held.order_number,
            "outlet_id": held.outlet.id,
        }
        override = kwargs["template_override"]
        assert override.subject == "Agent order awaiting approval"
        assert override.content == (
            f"🕒 Sardor Agent placed another order today for {held.outlet.name} ({held.order_number}). "
            f"Approve or reject it at {ORDER_APPROVALS_PATH}"
        )
        assert str(int(total)) not in override.content


def test_t_hold_2_a_returning_store_is_held_instead_of_instantly_confirmed(
    client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    """T-HOLD-2, the instant-COD half: a store with a delivered order would be confirmed inside
    `create_order`; a held order must skip that block and stay PENDING, never confirmed once."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    _make_returning(db, outlet)
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    assert first["confirmation"] == {"state": "auto_confirmed"}

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")

    order = db.session.get(Order, second["order"]["id"])
    assert second["confirmation"] == {"state": HELD}
    assert order.status is OrderStatus.PENDING
    assert _hold_of(order.id).status == "pending"
    assert OrderStatus.CONFIRMED not in {history.new_status for history in order.status_history}


# ---------------------------------------------------------------- T-HOLD-7: the one same-day rule


def test_t_hold_7_an_order_the_store_declined_does_not_count(
    app, client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers, telegram_id="555000111")
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    declined = client.post(
        f"/api/v1/orders/{first['order']['id']}/agent-confirmation",
        json={"action": "decline", "reason": "Bugun pul yo'q"},
        headers=_token_headers(app, outlet.user_id),
    )
    assert declined.status_code == 200, declined.get_data(as_text=True)

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    assert second["confirmation"] == {"state": "pending_confirmation"}
    assert _hold_of(second["order"]["id"]) is None


def test_t_hold_7_an_earlier_order_by_another_agent_counts(
    app, client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, admin_claim_headers,
    sample_product, spies,
):
    """The rule is the OUTLET's day, not the agent's: a colleague's order at the shop counts."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    colleague = make_sales_agent_user(db, phone="+998901239111", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=colleague.id, districts=["chilanzar"]))
    db.session.commit()
    assigned = client.post(
        f"/api/v1/admin/sales/outlets/{outlet.id}/assign",
        json={"agent_user_id": colleague.id},
        headers=admin_claim_headers,
    )
    assert assigned.status_code == 200, assigned.get_data(as_text=True)
    first = place_visit_order(client, _token_headers(app, colleague.id), outlet, _items(sample_product), "cash")

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    row = _hold_of(second["order"]["id"])
    assert second["confirmation"] == {"state": HELD}
    assert (row.earlier_order_ids, row.agent_user_id) == ([first["order"]["id"]], sales_agent_user.id)


def test_t_hold_7_a_delivered_earlier_order_still_counts(
    client, db, admin_user, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    # DELIVERED completes the pool delivery, which needs a driver at the door
    # (`assert_delivery_person_for_status`): the builders' one delivery walk takes it there with
    # the real writers. Delivered now; the instant is not what this rule reads.
    deliver(db, first["order"]["id"], at=datetime.now(UTC), actor=admin_user)

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    assert second["confirmation"] == {"state": HELD}
    assert _hold_of(second["order"]["id"]).earlier_order_ids == [first["order"]["id"]]


def test_t_hold_7_a_held_earlier_order_counts_so_the_third_names_both(
    client, db, held, sales_agent_auth_headers, sample_product
):
    third = place_visit_order(client, sales_agent_auth_headers, held.outlet, _items(sample_product), "cash")

    assert third["confirmation"] == {"state": HELD}
    assert _hold_of(third["order"]["id"]).earlier_order_ids == [held.first_id, held.order_id]


def test_t_hold_7_the_rule_reads_the_local_day_of_the_placement(
    client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, monkeypatch
):
    """An order created at 23:59:59 local yesterday is not today's (I-21 reads the local day of
    `place_order`'s `now_local`). The third order is the control: the second was created on the
    wall clock, inside the frozen local day, so it does count."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    first = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    midnight = datetime.combine(local_date(), time.min, tzinfo=TASHKENT)
    first_order = db.session.get(Order, first["order"]["id"])
    first_order.created_at = (midnight - timedelta(seconds=1)).astimezone(UTC)  # declared exception
    db.session.commit()
    freeze_local_now(monkeypatch, midnight + timedelta(seconds=1), visit_service=True)

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    third = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    assert second["confirmation"] == {"state": "confirmed"}
    assert _hold_of(second["order"]["id"]) is None
    assert third["confirmation"] == {"state": HELD}
    assert _hold_of(third["order"]["id"]).earlier_order_ids == [second["order"]["id"]]


def test_t_hold_7_orders_from_other_channels_do_not_count(
    client, db, operator_user, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    """A same-day Telegram order and an operator's phone order at the store are not agent orders."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    make_order(db, customer=outlet.user, lines=[(sample_product, 2)], source="telegram", outlet=outlet)
    make_order(
        db, customer=outlet.user, lines=[(sample_product, 2)], source="phone", created_by_staff=operator_user,
        outlet=outlet,
    )

    placed = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    assert placed["confirmation"] == {"state": "confirmed"}
    assert _hold_of(placed["order"]["id"]) is None


def test_t_hold_7_each_branch_of_one_account_is_its_own_outlet(
    client, db, sales_agent_auth_headers, sample_product, spies
):
    """Branch outlets B1 and B2 of one account: an order at B1 does not hold B2's (the rule keys on
    the visit's outlet, never on the customer account)."""
    customer = _customer(db, "+998901112277", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(
        UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    db.session.commit()
    first = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": customer.id})
    second = _create(
        client,
        sales_agent_auth_headers,
        {
            **BOT_PAYLOAD,
            "name": "Bahor market, Yunusobod",
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 19-kvartal, 4",
            "link_user_id": customer.id,
        },
    )
    b1, b2 = db.session.get(Outlet, first["id"]), db.session.get(Outlet, second["id"])

    at_b1 = place_visit_order(client, sales_agent_auth_headers, b1, _items(sample_product), "cash")
    at_b2 = place_visit_order(client, sales_agent_auth_headers, b2, _items(sample_product), "cash")

    assert at_b1["confirmation"] == at_b2["confirmation"] == {"state": "confirmed"}
    assert _hold_of(at_b2["order"]["id"]) is None


def test_t_hold_7_cancelling_the_earlier_order_later_does_not_release_the_hold(
    client, db, held, admin_claim_headers
):
    """The evidence is frozen at placement (I-22): the earlier order's later fate changes nothing."""
    cancelled = client.put(
        f"{ADMIN_ORDERS}/{held.first_id}/status",
        json={"status": "cancelled", "reason": "Customer changed the address"},
        headers=admin_claim_headers,
    )
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)

    row = _hold_of(held.order_id)
    assert (row.status, row.earlier_order_ids) == ("pending", [held.first_id])
    assert db.session.get(Order, held.order_id).status is OrderStatus.PENDING


# ---------------------------------------------------------------- T-HOLD-2 (business account), T-HOLD-3, T-HOLD-6


@pytest.fixture
def workplace(client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies):
    """A workplace outlet with an active, funded business-account contract: both rails offered."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers, payload=WORKPLACE_PAYLOAD)
    _fund_business_account(db, outlet.user_id, sample_product)
    return outlet


def _hold_two(client, agent_headers, outlet, product, rail):
    """Two same-day orders on one rail at `outlet`; returns both route replies, the second held."""
    first = place_visit_order(client, agent_headers, outlet, _items(product), rail)
    second = place_visit_order(client, agent_headers, outlet, _items(product, 3), rail)
    assert second["confirmation"] == {"state": HELD}
    return first, second


def test_t_hold_2_a_business_account_order_is_paid_but_still_held(
    client, db, workplace, sales_agent_auth_headers, sample_product
):
    """The payment completes inside `create_order` (`initialize_order_payment` ->
    `_handle_successful_payment`); the order must not be confirmed by it."""
    first, second = _hold_two(client, sales_agent_auth_headers, workplace, sample_product, "business_account")

    order = db.session.get(Order, second["order"]["id"])
    assert first["confirmation"] == {"state": "auto_confirmed"}
    assert order.payment.status is PaymentStatus.COMPLETED
    assert order.status is OrderStatus.PENDING
    assert _hold_of(order.id).status == "pending"


def test_t_hold_3_no_sweep_decides_a_held_order(db, held, sample_user):
    """I-25: 25 hours on, the three sweeps that touch PENDING orders leave the held order and its
    row pending. A plain aged cash order is confirmed by the same run, so the run did run."""
    order = db.session.get(Order, held.order_id)
    # Declared exception: the sweeps read `datetime.now(timezone.utc)` and have no clock seam.
    order.created_at = datetime.now(UTC) - timedelta(hours=25)
    db.session.commit()
    control = _aged_cash_order(db, sample_user, delivery_date=date.today() + timedelta(days=3))

    confirmed = auto_confirm_pending_orders()
    cancel_abandoned_orders()
    sales_agent_tasks.expire_agent_order_confirmations()

    db.session.refresh(order)
    db.session.refresh(control)
    assert order.status is OrderStatus.PENDING
    assert _hold_of(order.id).status == "pending"
    assert control.status is OrderStatus.CONFIRMED
    assert confirmed["confirmed_count"] == 1


def test_t_hold_3_a_completed_click_payment_does_not_confirm_a_held_order(
    client, db, workplace, sales_agent_auth_headers, admin_claim_headers, sample_product, monkeypatch
):
    """A held business-account order edited to Click, whose link the store then pays. The task
    that confirms paid PENDING orders must skip it, still send the payment notification, and not
    retry (the guard's 409 would make it retry three times and never notify)."""
    _first, second = _hold_two(client, sales_agent_auth_headers, workplace, sample_product, "business_account")
    edited = client.post(
        f"{ADMIN_ORDERS}/{second['order']['id']}/payment-method",
        json={"new_method": "click", "reason": "Store pays by the Click link"},
        headers=admin_claim_headers,
    )
    assert edited.status_code == 200, edited.get_data(as_text=True)
    order = db.session.get(Order, second["order"]["id"])
    payment = order.payment
    assert (order.payment_method, payment.status) == (PaymentMethod.CLICK, PaymentStatus.PENDING)
    # The gateway's word (external I/O): the store paid the link.
    payment.status = PaymentStatus.COMPLETED
    payment.paid_at = datetime.now(UTC)
    db.session.commit()
    notified = _spy(monkeypatch, NotificationService, "send_payment_notification")

    result = process_payment_confirmation.run(payment.id)

    assert result == {"success": True, "skipped": True, "reason": "awaiting_staff_approval"}
    db.session.refresh(order)
    assert order.status is OrderStatus.PENDING
    assert _hold_of(order.id).status == "pending"
    assert [args[1:] for args, _kwargs in notified] == [(payment.id,)]


@pytest.mark.parametrize("headers_fixture", ["operator_auth_headers", "manager_claim_headers", "admin_claim_headers"])
def test_t_hold_6_no_status_change_confirms_a_held_order(request, client, db, held, headers_fixture):
    """The Orders page's dropdown, as each role that may use it: 409 with the code, nothing written."""
    headers = request.getfixturevalue(headers_fixture)

    response = client.put(f"{ADMIN_ORDERS}/{held.order_id}/status", json={"status": "confirmed"}, headers=headers)

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "error_code": "ORDER_AWAITING_STAFF_APPROVAL",
        "details": {"order_id": held.order_id, "path": ORDER_APPROVALS_PATH},
    }
    order = db.session.get(Order, held.order_id)
    assert order.status is OrderStatus.PENDING
    assert OrderStatus.CONFIRMED not in {history.new_status for history in order.status_history}
    assert _hold_of(held.order_id).status == "pending"


def test_t_hold_6_bulk_confirm_fails_that_order_and_names_it(client, held, admin_claim_headers):
    response = client.post(
        "/api/v1/admin/bulk-actions",
        json={"action": "confirm", "target_type": "order", "target_ids": [held.order_id], "reason": "Morning batch"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    results = response.get_json()["data"]["results"]
    assert (results["success_count"], results["failed_count"]) == (0, 1)
    assert results["errors"] == [{"order_id": held.order_id, "error": "This order waits for a manager's approval"}]
    assert _hold_of(held.order_id).status == "pending"


def test_t_hold_6_settling_a_held_cash_order_on_the_business_account_does_not_confirm_it(
    client, db, workplace, sales_agent_auth_headers, admin_claim_headers, sample_product
):
    """`_settle_as_business_account` -> `initialize_order_payment` -> `_handle_successful_payment`."""
    _first, second = _hold_two(client, sales_agent_auth_headers, workplace, sample_product, "cash")

    response = client.post(
        f"{ADMIN_ORDERS}/{second['order']['id']}/payment-method",
        json={"new_method": "business_account", "reason": "Store moved to its contract"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    order = db.session.get(Order, second["order"]["id"])
    assert order.payment.status is PaymentStatus.COMPLETED
    assert order.status is OrderStatus.PENDING
    assert _hold_of(order.id).status == "pending"


def test_t_hold_6_the_store_cannot_confirm_a_held_order(
    app, client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies
):
    """There is no customer question for a held order, so the store's own answer route refuses."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers, telegram_id="555000111")
    _first, second = _hold_two(client, sales_agent_auth_headers, outlet, sample_product, "cash")

    response = client.post(
        f"/api/v1/orders/{second['order']['id']}/agent-confirmation",
        json={"action": "confirm"},
        headers=_token_headers(app, outlet.user_id),
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "SALES_CONFIRMATION_NOT_PENDING"
    assert db.session.get(Order, second["order"]["id"]).status is OrderStatus.PENDING


def test_t_hold_6_the_admin_order_screens_publish_the_hold(client, held, manager_claim_headers):
    """The flag is published (S-27), never re-derived by Orders.js: the list, the detail the modal
    swaps in, and the user drawer's recent orders all say which order waits."""
    listed = client.get(ADMIN_ORDERS, headers=manager_claim_headers, query_string={"per_page": 50})
    detail = client.get(f"{ADMIN_ORDERS}/{held.order_id}", headers=manager_claim_headers)
    first_detail = client.get(f"{ADMIN_ORDERS}/{held.first_id}", headers=manager_claim_headers)
    user = client.get(f"/api/v1/admin/users/{held.outlet.user_id}", headers=manager_claim_headers)

    for response in (listed, detail, first_detail, user):
        assert response.status_code == 200, response.get_data(as_text=True)
    flags = {row["id"]: row["awaiting_staff_approval"] for row in listed.get_json()["data"]["items"]}
    assert flags == {held.first_id: False, held.order_id: True}
    assert detail.get_json()["data"]["order"]["awaiting_staff_approval"] is True
    assert first_detail.get_json()["data"]["order"]["awaiting_staff_approval"] is False
    recent = {row["id"]: row["awaiting_staff_approval"] for row in user.get_json()["data"]["recent_orders"]}
    assert recent == {held.first_id: False, held.order_id: True}


# ---------------------------------------------------------------- T-HOLD-12: the stock hold


def _frozen_datetime(instant):
    """A `datetime` whose `now()` is `instant`, for `inventory_service`'s module-level name (the
    plan's declared seam: that module has no clock of its own)."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    return _FrozenDatetime


def test_t_hold_12_the_stock_hold_and_its_stored_expiry_move_to_the_approval_window(
    app, client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, monkeypatch
):
    """§4.18.4, S-34: the held order's reservation is stretched to SALES_STAFF_APPROVAL_HOLD_HOURS,
    and the details hash's `expires_at` moves with the key TTLs. The 01:10 cleanup reads the
    stored `expires_at`, never the TTL, so a hold stretched by EXPIRE alone still said "30
    minutes" on paper."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    t0 = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(inventory_service_module, "datetime", _frozen_datetime(t0))
    extended = []
    real_extend = InventoryService.extend_reservations

    def spy_extend(self, order_id, items, ttl):
        extended.append((order_id, [item["product_id"] for item in items], ttl))
        return real_extend(self, order_id, items, ttl)

    monkeypatch.setattr(InventoryService, "extend_reservations", spy_extend)

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")

    held_id = second["order"]["id"]
    window = app.config["SALES_STAFF_APPROVAL_HOLD_HOURS"] * 3600
    assert window == 12 * 3600
    assert extended == [(held_id, [sample_product.id], window)]
    redis = get_inventory_service()._get_redis_client()
    key = RedisKeyspace.inventory_reservation(held_id, sample_product.id)
    details = RedisKeyspace.reservation_details(held_id, sample_product.id)
    for name in (key, details):
        assert window - 60 <= redis.ttl(name) <= window
    assert redis.hget(details, "expires_at").decode("utf-8") == (t0 + timedelta(hours=12)).isoformat()

    # 01:10 local the next night is at most 11 hours on for a hold placed after 14:10. This half
    # is vacuous while the cleanup deletes nothing; the strict-xfail control below makes that
    # visible and flips the day the cleanup is repaired (PR16).
    monkeypatch.setattr(inventory_service_module, "datetime", _frozen_datetime(t0 + timedelta(hours=11)))
    cleanup_expired_inventory_reservations.run()

    assert redis.exists(key) == 1 and redis.exists(details) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "cleanup_expired_reservations checks a str key against the bytes-keyed hgetall, "
        "so it deletes nothing (owner follow-up, handover)"
    ),
)
def test_t_hold_12_control_the_cleanup_deletes_a_lapsed_reservation(
    client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, monkeypatch
):
    """The control for the keep-assertion above (PR16): the same held reservation, once its stored
    `expires_at` has passed, is deleted by the cleanup run.

    Without this, "the cleanup at +11 h keeps the hold" would pass just as well against a cleanup
    that never deletes anything, which is today's: `cleanup_expired_reservations` tests the str
    `"expires_at"` against the bytes keys `hgetall` returns. So this is a strict xfail. When the
    owner repairs the cleanup, it passes, the strict marker turns it red, and the marker comes
    off; from then on the keep-assertion above proves something. `raises=AssertionError` keeps
    a broken setup (no reservation hash: an `AttributeError`) a real failure, not an xfail.
    """
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")
    t0 = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(inventory_service_module, "datetime", _frozen_datetime(t0))
    held = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")
    held_id = held["order"]["id"]
    redis = get_inventory_service()._get_redis_client()
    key = RedisKeyspace.inventory_reservation(held_id, sample_product.id)
    details = RedisKeyspace.reservation_details(held_id, sample_product.id)
    stored = datetime.fromisoformat(redis.hget(details, "expires_at").decode("utf-8"))

    # One minute past the stored expiry: the hold has lapsed on paper, while Redis's real TTL
    # (the wall clock is not frozen) still keeps both keys for the cleanup to find.
    monkeypatch.setattr(inventory_service_module, "datetime", _frozen_datetime(stored + timedelta(minutes=1)))
    cleanup_expired_inventory_reservations.run()

    assert redis.exists(key) == 0 and redis.exists(details) == 0


def test_t_hold_12_a_failing_extension_never_fails_the_placement(
    client, db, sales_agent_auth_headers, operator_auth_headers, sample_product, spies, monkeypatch
):
    """The order and its hold are committed before the stock is stretched; a Redis outage there
    is a log line, never a 500 over an order that exists (`_hold_stock_for_the_question`)."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash")

    def blow_up(self, order_id, items, ttl):
        raise ConnectionError("redis went away")

    monkeypatch.setattr(InventoryService, "extend_reservations", blow_up)

    second = place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash")

    assert second["confirmation"] == {"state": HELD}
    assert spies.alerts == [((second["order"]["id"],), {})]


# ---------------------------------------------------------------- OA2 / OA3: the decisions

POOL = "/api/v1/staff/delivery/pool"


def _outcome_payload(held_ns, reason=None):
    """`notify_agent_order_event`'s payload (§7.6): the recipient is the agent who PLACED it."""
    return {
        "outlet_id": held_ns.outlet.id,
        "outlet_name": held_ns.outlet.name,
        "order_id": held_ns.order_id,
        "order_number": held_ns.order_number,
        "reason": reason,
    }


def _pool_order_ids(app, client, driver_id):
    response = client.get(POOL, headers=_token_headers(app, driver_id, "delivery_driver"))
    assert response.status_code == 200, response.get_data(as_text=True)
    return [item["order_id"] for item in response.get_json()["data"]["items"]]


def test_t_hold_4_approve_confirms_the_order_and_puts_it_in_the_pool(
    app, client, db, held, manager_user, manager_claim_headers, admin_claim_headers, driver_on_shift, spies
):
    """T-HOLD-4: OA2 is "exactly the normal confirmed path" plus the agent's one outcome push."""
    stray_key = client.post(f"{APPROVALS}/{held.order_id}/approve", json={"note": "ok"}, headers=manager_claim_headers)
    no_hold = client.post(f"{APPROVALS}/{held.first_id}/approve", json={}, headers=manager_claim_headers)
    assert stray_key.status_code == 400, stray_key.get_data(as_text=True)
    assert no_hold.status_code == 404, no_hold.get_data(as_text=True)
    assert (no_hold.get_json()["error_code"], no_hold.get_json()["details"]) == (
        "SALES_ORDER_APPROVAL_NOT_FOUND",
        {"order_id": held.first_id},
    )

    response = client.post(f"{APPROVALS}/{held.order_id}/approve", json={}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    approval = response.get_json()["data"]["approval"]
    assert (approval["status"], approval["decided_by"]) == (
        "approved",
        {"id": manager_user.id, "name": manager_user.full_name},
    )
    assert (approval["can_decide"], approval["self_decided"]) == (False, False)
    order = db.session.get(Order, held.order_id)
    assert order.status is OrderStatus.CONFIRMED
    confirmed = [row for row in order.status_history if row.new_status is OrderStatus.CONFIRMED]
    assert [(row.changed_by, row.notes) for row in confirmed] == [(manager_user.id, "Confirmed at sales visit")]
    assert order.delivery is not None
    assert held.order_id in _pool_order_ids(app, client, driver_on_shift)
    hold = _hold_of(held.order_id)
    assert spies.pushes == [
        (
            (777000111, SALES_EVENT_AGENT_ORDER_APPROVED, _outcome_payload(held)),
            {"event_id": f"order-approval:{hold.id}:approved"},
        )
    ]
    listed = client.get(ADMIN_ORDERS, headers=admin_claim_headers, query_string={"per_page": 50})
    assert {row["id"]: row["awaiting_staff_approval"] for row in listed.get_json()["data"]["items"]}[
        held.order_id
    ] is False

    again = client.post(f"{APPROVALS}/{held.order_id}/approve", json={}, headers=manager_claim_headers)

    assert again.status_code == 409, again.get_data(as_text=True)
    assert (again.get_json()["error_code"], again.get_json()["details"]) == (
        "SALES_ORDER_APPROVAL_NOT_PENDING",
        {"order_id": held.order_id, "status": "approved", "order_status": "confirmed"},
    )
    assert len(spies.pushes) == 1


def test_t_hold_5_reject_cancels_the_order_with_a_staff_only_reason(
    client, db, held, manager_user, manager_claim_headers, spies, monkeypatch
):
    """T-HOLD-5: the order is cancelled; its history `notes` (the customer's Track screen) stays
    empty and the reason is staff-only; the reservation is released; one push with the reason;
    the agent's cancelled count moves and the strike rate does not (§4.18.8)."""
    released = []
    real_release = InventoryService.release_reservations

    def spy_release(self, order_id):
        released.append(order_id)
        return real_release(self, order_id)

    monkeypatch.setattr(InventoryService, "release_reservations", spy_release)
    metrics_url = f"/api/v1/admin/sales/agents/{held.agent.id}/metrics"
    before = client.get(metrics_url, headers=manager_claim_headers).get_json()["data"]["metrics"]
    reason = f"Same order as {held.first_number}"

    response = client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": reason}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    approval = response.get_json()["data"]["approval"]
    assert (approval["status"], approval["reason"], approval["decided_by"]) == (
        "rejected",
        reason,
        {"id": manager_user.id, "name": manager_user.full_name},
    )
    order = db.session.get(Order, held.order_id)
    assert order.status is OrderStatus.CANCELLED
    [cancelled] = [row for row in order.status_history if row.new_status is OrderStatus.CANCELLED]
    assert (cancelled.notes, cancelled.reason, cancelled.changed_by) == (None, reason, manager_user.id)
    assert held.order_id in released
    hold = _hold_of(held.order_id)
    assert (hold.status, hold.reason, hold.decided_by_user_id) == ("rejected", reason, manager_user.id)
    assert spies.pushes == [
        (
            (777000111, SALES_EVENT_AGENT_ORDER_REJECTED, _outcome_payload(held, reason)),
            {"event_id": f"order-approval:{hold.id}:rejected"},
        )
    ]
    after = client.get(metrics_url, headers=manager_claim_headers).get_json()["data"]["metrics"]
    assert after["agent_orders_cancelled"] == before["agent_orders_cancelled"] + 1
    assert after["strike_rate_pct"] == before["strike_rate_pct"]


def test_t_hold_5_a_rejection_committed_before_a_later_step_raised_still_pushes_and_audits_once(
    client, db, held, manager_user, manager_claim_headers, spies, monkeypatch
):
    """T9-R1, the reject twin of R2-3. `cancel_order` commits the CANCELLED transition, and with it
    the `rejected` row, inside `update_order_status`; the corporate-unit release and the final
    commit come after it. A raise there is re-raised, as approve's is, but the decision is already
    committed: it must still reach the agent and the audit log exactly once, and a retry must meet
    the queue's one code instead of deciding twice."""
    from business_app.services.corporate_contract_service import CorporateContractService
    from business_app.utils.exceptions import NotFoundError

    audits = _spy_audit(monkeypatch)
    release_signature = inspect.signature(CorporateContractService.release_for_order)

    def release_fails(*args, **kwargs):
        release_signature.bind(*args, **kwargs)
        raise NotFoundError("Corporate prepayment balance not found")

    monkeypatch.setattr(CorporateContractService, "release_for_order", release_fails)
    reason = "Duplicate of the morning order"

    response = client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": reason}, headers=manager_claim_headers)

    # The raise reaches the manager as it would for approve: the step after the commit failed.
    assert response.status_code == 404, response.get_data(as_text=True)
    db.session.expire_all()
    hold = _hold_of(held.order_id)
    assert (hold.status, hold.reason, hold.decided_by_user_id) == ("rejected", reason, manager_user.id)
    assert db.session.get(Order, held.order_id).status is OrderStatus.CANCELLED
    assert spies.pushes == [
        (
            (777000111, SALES_EVENT_AGENT_ORDER_REJECTED, _outcome_payload(held, reason)),
            {"event_id": f"order-approval:{hold.id}:rejected"},
        )
    ]
    [audit] = [call for call in audits if call.get("action", "").startswith("sales_agent_order_")]
    assert (audit["action"], audit["event_type"], audit["resource_id"]) == (
        "sales_agent_order_rejected",
        AuditEventType.ORDER_CANCELLED,
        str(held.order_id),
    )
    assert audit["additional_data"] == {
        "order_id": held.order_id,
        "order_number": held.order_number,
        "outlet_id": held.outlet.id,
        "agent_user_id": held.agent.id,
        "approval_id": hold.id,
        "self_decided": False,
        "reason": reason,
    }

    retry = client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": reason}, headers=manager_claim_headers)

    assert retry.status_code == 409, retry.get_data(as_text=True)
    assert (retry.get_json()["error_code"], retry.get_json()["details"]) == (
        "SALES_ORDER_APPROVAL_NOT_PENDING",
        {"order_id": held.order_id, "status": "rejected", "order_status": "cancelled"},
    )
    assert len(spies.pushes) == 1
    assert len([call for call in audits if call.get("action", "").startswith("sales_agent_order_")]) == 1


@pytest.mark.parametrize(
    "body,code",
    [
        ({"reason": "  "}, "ADMIN_REASON_REQUIRED"),
        ({"reason": 5}, "ADMIN_REASON_REQUIRED"),
        ({}, "ADMIN_REASON_REQUIRED"),
        ({"reason": "x" * (ADMIN_REASON_MAX_LENGTH + 1)}, "ADMIN_REASON_TOO_LONG"),
        ({"reason": "Duplicate", "notify_store": True}, None),
    ],
    ids=["blank", "number", "missing", "too-long", "extra-key"],
)
def test_t_hold_5_a_refused_reject_writes_nothing(client, db, held, manager_claim_headers, spies, body, code):
    response = client.post(f"{APPROVALS}/{held.order_id}/reject", json=body, headers=manager_claim_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    if code is not None:
        assert response.get_json()["error_code"] == code
    hold = _hold_of(held.order_id)
    assert (hold.status, hold.decided_by_user_id, hold.reason) == ("pending", None, None)
    assert db.session.get(Order, held.order_id).status is OrderStatus.PENDING
    assert spies.pushes == []


def test_t_hold_7_a_rejected_earlier_order_does_not_count(
    client, db, held, manager_claim_headers, sales_agent_auth_headers, sample_product
):
    """The rejected order is cancelled, so the next one names only the first (I-21, I-22)."""
    rejected = client.post(
        f"{APPROVALS}/{held.order_id}/reject", json={"reason": "Duplicate of the morning order"},
        headers=manager_claim_headers,
    )
    assert rejected.status_code == 200, rejected.get_data(as_text=True)

    third = place_visit_order(client, sales_agent_auth_headers, held.outlet, _items(sample_product), "cash")

    assert third["confirmation"] == {"state": HELD}
    assert _hold_of(third["order"]["id"]).earlier_order_ids == [held.first_id]


def _assert_closed_by_the_cancel(client, held_ns, headers, spies, canceller_id):
    """T-HOLD-9's common half: the row is `cancelled` by whoever cancelled the order, leaves the
    default (pending) queue, is listed under `cancelled` with no buttons, and OA2 answers the one
    code the page handles. No agent push: only a manager's decision promises one (§7.6)."""
    hold = _hold_of(held_ns.order_id)
    assert (hold.status, hold.decided_by_user_id, hold.reason) == ("cancelled", canceller_id, None)
    assert hold.decided_at is not None
    pending = client.get(APPROVALS, headers=headers).get_json()["data"]
    assert (pending["items"], pending["pending_count"]) == ([], 0)
    listed = client.get(APPROVALS, headers=headers, query_string={"status": "cancelled"}).get_json()["data"]
    assert [(row["order_id"], row["status"], row["can_decide"], row["self_decided"]) for row in listed["items"]] == [
        (held_ns.order_id, "cancelled", False, False)
    ]
    again = client.post(f"{APPROVALS}/{held_ns.order_id}/approve", json={}, headers=headers)
    assert again.status_code == 409, again.get_data(as_text=True)
    assert again.get_json()["error_code"] == "SALES_ORDER_APPROVAL_NOT_PENDING"
    assert spies.pushes == []


def test_t_hold_9_the_store_cancelling_its_unpaid_held_order_closes_the_hold(
    app, client, db, held, manager_claim_headers, spies
):
    cancelled = client.post(
        f"/api/v1/orders/{held.order_id}/cancel",
        json={"reason": "Ordered twice by mistake"},
        headers=_token_headers(app, held.outlet.user_id),
    )

    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    assert db.session.get(Order, held.order_id).status is OrderStatus.CANCELLED
    _assert_closed_by_the_cancel(client, held, manager_claim_headers, spies, held.outlet.user_id)


def test_t_hold_9_an_admin_cancel_from_the_orders_page_closes_the_hold(
    client, db, held, admin_user, admin_claim_headers, manager_claim_headers, spies
):
    cancelled = client.put(
        f"{ADMIN_ORDERS}/{held.order_id}/status",
        json={"status": "cancelled", "reason": "Store asked to drop it"},
        headers=admin_claim_headers,
    )

    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    _assert_closed_by_the_cancel(client, held, manager_claim_headers, spies, admin_user.id)


def test_t_hold_9_the_store_cannot_cancel_a_paid_held_order(
    app, client, db, workplace, sales_agent_auth_headers, sample_product, spies
):
    """A held business-account order is paid at creation: `customer_cancel_block_code` answers
    CUSTOMER_CANCEL_PAID, and the hold stays pending."""
    _first, second = _hold_two(client, sales_agent_auth_headers, workplace, sample_product, "business_account")

    response = client.post(
        f"/api/v1/orders/{second['order']['id']}/cancel",
        json={"reason": "Changed our mind"},
        headers=_token_headers(app, workplace.user_id),
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()["data"]
    assert (body["error_code"], body["reason_code"]) == ("ORDER_NOT_CUSTOMER_CANCELLABLE", CUSTOMER_CANCEL_PAID)
    assert _hold_of(second["order"]["id"]).status == "pending"


def _dual_role(db, phone, role, first_name):
    """A manager or an admin who also holds a sales-agent profile (D-Q11, I-24)."""
    user = make_sales_agent_user(db, phone=phone, role=role, staff_roles=["sales_agent"])
    user.first_name = first_name
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def _assign(client, admin_headers, outlet, agent):
    """Q10: reassign the outlet through the admin route; the onboarder stays the onboarder."""
    response = client.post(
        f"/api/v1/admin/sales/outlets/{outlet.id}/assign", json={"agent_user_id": agent.id}, headers=admin_headers
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def test_t_hold_9_a_manager_may_cancel_their_own_held_order_as_an_ordinary_cancel(
    app, client, db, sales_agent_auth_headers, operator_auth_headers, admin_claim_headers, sample_product, spies
):
    """An ordinary cancel is not a pay decision (I-24, R1-10): allowed, recorded with the manager
    as the canceller, and not tagged as self-decided."""
    mansur = _dual_role(db, "+998901239121", UserRole.MANAGER, "Mansur")
    mansur_headers = _token_headers(app, mansur.id, "manager")
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    _assign(client, admin_claim_headers, outlet, mansur)
    _first, second = _hold_two(client, mansur_headers, outlet, sample_product, "cash")
    order_id = second["order"]["id"]

    cancelled = client.put(
        f"{ADMIN_ORDERS}/{order_id}/status",
        json={"status": "cancelled", "reason": "Placed twice by mistake"},
        headers=mansur_headers,
    )

    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    hold = _hold_of(order_id)
    assert (hold.status, hold.decided_by_user_id) == ("cancelled", mansur.id)
    listed = client.get(APPROVALS, headers=admin_claim_headers, query_string={"status": "cancelled"}).get_json()
    assert [(row["order_id"], row["self_decided"]) for row in listed["data"]["items"]] == [(order_id, False)]


# ---------------------------------------------------------------- T-HOLD-14: approve, then the store cancels


def test_t_hold_14_a_store_cancel_after_the_approval_cancels_an_ordinary_order(
    app, client, db, held, manager_claim_headers, spies
):
    """The sequential half of T-HOLD-14 (PR17). Once OA2 has approved, the order is an ordinary
    confirmed order with a driverless delivery, and the store's own cancel is two decisions in a
    row, not a race lost: `customer_cancel_block_code` lets it through. The decision stands as
    made (the row stays `approved`; a cancel rewrites only a pending hold), the order is
    cancelled, and the ledger sync, which reads only delivered-and-paid orders, writes no line
    for it. The race of the same two requests is the Postgres test (Step 38)."""
    approved = client.post(f"{APPROVALS}/{held.order_id}/approve", json={}, headers=manager_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert db.session.get(Order, held.order_id).status is OrderStatus.CONFIRMED

    cancelled = client.post(
        f"/api/v1/orders/{held.order_id}/cancel",
        json={"reason": "Ordered twice by mistake"},
        headers=_token_headers(app, held.outlet.user_id),
    )

    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    db.session.expire_all()
    assert _hold_of(held.order_id).status == "approved"
    assert db.session.get(Order, held.order_id).status is OrderStatus.CANCELLED
    SalesPayLedgerService.sync([held.agent.id])
    assert SalesPayLedgerLine.query.filter_by(order_id=held.order_id).count() == 0


# ---------------------------------------------------------------- T-HOLD-10: who may decide (I-24)


def _spy_audit(monkeypatch):
    """Record `audit_logger.log_event`, bound against its real signature."""
    signature = inspect.signature(audit_logger.log_event)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append(kwargs)
        return "audit-id"

    monkeypatch.setattr(audit_logger, "log_event", fake)
    return calls


def _alert_recipients(monkeypatch, order_id):
    in_app = _spy(monkeypatch, NotificationService, "send_notification")
    sales_agent_tasks.notify_managers_agent_order_awaiting_approval.run(order_id)
    return sorted(kwargs["user_id"] for _args, kwargs in in_app)


def _can_decide(client, headers, order_id):
    rows = client.get(APPROVALS, headers=headers).get_json()["data"]["items"]
    return {row["order_id"]: row["can_decide"] for row in rows}[order_id]


def test_t_hold_10_only_managers_and_admins_reach_the_queue(
    app, client, held, operator_user, sales_agent_auth_headers, manager_claim_headers, admin_claim_headers
):
    operator_headers = _token_headers(app, operator_user.id, "operator")
    for headers in (sales_agent_auth_headers, operator_headers):
        assert client.get(APPROVALS, headers=headers).status_code == 403
        assert client.post(f"{APPROVALS}/{held.order_id}/approve", json={}, headers=headers).status_code == 403
        assert (
            client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": "Not today"}, headers=headers).status_code
            == 403
        )
    for headers in (manager_claim_headers, admin_claim_headers):
        response = client.get(APPROVALS, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert [row["order_id"] for row in response.get_json()["data"]["items"]] == [held.order_id]
    assert _hold_of(held.order_id).status == "pending"


@pytest.mark.parametrize("subject", ["placer", "onboarder"])
def test_t_hold_10_a_manager_cannot_decide_a_hold_that_changes_their_own_pay(
    subject, app, client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, admin_user,
    admin_claim_headers, manager_user, manager_claim_headers, sample_product, spies, monkeypatch,
):
    """Mansur, a manager with an agent profile, is a decision subject either as the placer (the
    outlet reassigned to him) or as the outlet's onboarder (the outlet reassigned to the agent,
    Q10). Either way: `can_decide` false for him, OA2 and OA3 refuse with his id, nothing is
    written, and the alert skipped him. Malika, another manager, may decide it."""
    mansur = _dual_role(db, "+998901239121", UserRole.MANAGER, "Mansur")
    mansur_headers = _token_headers(app, mansur.id, "manager")
    if subject == "placer":
        outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
        _assign(client, admin_claim_headers, outlet, mansur)
        placer_headers = mansur_headers
    else:
        outlet = _store(client, db, mansur_headers, operator_auth_headers)
        _assign(client, admin_claim_headers, outlet, sales_agent_user)
        placer_headers = sales_agent_auth_headers
    _first, second = _hold_two(client, placer_headers, outlet, sample_product, "cash")
    order_id = second["order"]["id"]

    assert _alert_recipients(monkeypatch, order_id) == sorted([admin_user.id, manager_user.id])
    assert _can_decide(client, mansur_headers, order_id) is False
    assert _can_decide(client, manager_claim_headers, order_id) is True
    approve = client.post(f"{APPROVALS}/{order_id}/approve", json={}, headers=mansur_headers)
    reject = client.post(f"{APPROVALS}/{order_id}/reject", json={"reason": "Not needed after all"}, headers=mansur_headers)

    for response in (approve, reject):
        assert response.status_code == 403, response.get_data(as_text=True)
        assert (response.get_json()["error_code"], response.get_json()["details"]) == (
            "SALES_PAY_SELF_DECISION",
            {"agent_user_id": mansur.id},
        )
    hold = _hold_of(order_id)
    assert (hold.status, hold.decided_by_user_id, hold.decided_at) == ("pending", None, None)
    assert db.session.get(Order, order_id).status is OrderStatus.PENDING
    assert spies.pushes == []


@pytest.mark.parametrize("subject", ["placer", "onboarder"])
def test_t_hold_10_an_admin_may_decide_a_hold_that_changes_their_own_pay_and_it_is_tagged(
    subject, app, client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, admin_user,
    admin_claim_headers, manager_user, sample_product, spies, monkeypatch,
):
    """Anvar, an admin with an agent profile (D-Q11): allowed, and the row, OA1 and the audit row
    say `self_decided`. The statement half is T-NEW-11 (Tasks 6 and 7)."""
    anvar = _dual_role(db, "+998901239122", UserRole.ADMIN, "Anvar")
    anvar_headers = _token_headers(app, anvar.id, "admin")
    if subject == "placer":
        outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
        _assign(client, admin_claim_headers, outlet, anvar)
        placer, placer_headers = anvar, anvar_headers
    else:
        outlet = _store(client, db, anvar_headers, operator_auth_headers)
        _assign(client, admin_claim_headers, outlet, sales_agent_user)
        placer, placer_headers = sales_agent_user, sales_agent_auth_headers
    _first, second = _hold_two(client, placer_headers, outlet, sample_product, "cash")
    order_id = second["order"]["id"]
    assert _alert_recipients(monkeypatch, order_id) == sorted([admin_user.id, manager_user.id])
    assert _can_decide(client, anvar_headers, order_id) is True
    audits = _spy_audit(monkeypatch)

    approved = client.post(f"{APPROVALS}/{order_id}/approve", json={}, headers=anvar_headers)

    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["approval"]["self_decided"] is True
    listed = client.get(APPROVALS, headers=admin_claim_headers, query_string={"status": "approved"}).get_json()
    assert [(row["order_id"], row["self_decided"]) for row in listed["data"]["items"]] == [(order_id, True)]
    hold = _hold_of(order_id)
    [audit] = [call for call in audits if call.get("action") == "sales_agent_order_approved"]
    assert (audit["event_type"], audit["severity"], audit["resource_type"], audit["resource_id"]) == (
        AuditEventType.ORDER_UPDATED,
        AuditSeverity.MEDIUM,
        "order",
        str(order_id),
    )
    assert audit["additional_data"] == {
        "order_id": order_id,
        "order_number": second["order"]["order_number"],
        "outlet_id": outlet.id,
        "agent_user_id": placer.id,
        "approval_id": hold.id,
        "self_decided": True,
    }


def test_t_hold_10_a_rejection_is_audited_with_its_reason(client, held, admin_user, admin_claim_headers, monkeypatch):
    audits = _spy_audit(monkeypatch)

    response = client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": "Duplicate"}, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    [audit] = [call for call in audits if call.get("action") == "sales_agent_order_rejected"]
    assert (audit["event_type"], audit["severity"], audit["resource_type"]) == (
        AuditEventType.ORDER_CANCELLED,
        AuditSeverity.MEDIUM,
        "order",
    )
    assert audit["additional_data"] == {
        "order_id": held.order_id,
        "order_number": held.order_number,
        "outlet_id": held.outlet.id,
        "agent_user_id": held.agent.id,
        "approval_id": _hold_of(held.order_id).id,
        "self_decided": False,
        "reason": "Duplicate",
    }


# ---------------------------------------------------------------- T-HOLD-11: order data, never pay

FORBIDDEN_PAY_KEYS = {
    "commission", "estimated_commission", "rate", "rate_value", "bonus", "bonus_amount",
    "pay", "salary", "base", "base_salary", "penalty", "amount",
    # v5 (D-BANDS): the tier vocabulary never reaches the approval queue either.
    "units", "share", "tiers", "tier",
}
APPROVAL_ROW_KEYS = {
    "order_id", "status", "requested_at", "decided_at", "agent", "outlet", "visit_id", "order",
    "earlier_orders", "decided_by", "reason", "self_decided", "can_decide",
}


def _keys(value):
    """Every key at every depth of a JSON value."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_t_hold_11_the_queue_carries_order_data_and_no_pay(
    client, db, held, admin_claim_headers, manager_claim_headers, sales_agent_auth_headers, sample_product
):
    extra = [
        place_visit_order(client, sales_agent_auth_headers, held.outlet, _items(sample_product), "cash")["order"]["id"]
        for _ in range(3)
    ]
    approved_id, rejected_id, cancelled_id = extra
    decisions = [
        client.post(f"{APPROVALS}/{approved_id}/approve", json={}, headers=admin_claim_headers),
        client.post(f"{APPROVALS}/{rejected_id}/reject", json={"reason": "Duplicate"}, headers=admin_claim_headers),
    ]
    cancelled = client.put(
        f"{ADMIN_ORDERS}/{cancelled_id}/status",
        json={"status": "cancelled", "reason": "Store closed today"},
        headers=admin_claim_headers,
    )
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    pages = {
        status: client.get(APPROVALS, headers=manager_claim_headers, query_string={"status": status})
        for status in ("pending", "approved", "rejected", "cancelled")
    }

    for response in [*decisions, *pages.values()]:
        assert response.status_code == 200, response.get_data(as_text=True)
        assert FORBIDDEN_PAY_KEYS.isdisjoint(set(_keys(response.get_json()["data"])))
    assert {status: [row["order_id"] for row in page.get_json()["data"]["items"]] for status, page in pages.items()} == {
        "pending": [held.order_id],
        "approved": [approved_id],
        "rejected": [rejected_id],
        "cancelled": [cancelled_id],
    }
    pending = pages["pending"].get_json()["data"]
    assert pending["statuses"] == ["pending", "approved", "rejected", "cancelled"]
    assert pending["pending_count"] == 1
    assert pending["meta"] == {"page": 1, "per_page": 20, "total": 1, "pages": 1, "has_next": False, "has_prev": False}
    [row] = pending["items"]
    assert set(row) == APPROVAL_ROW_KEYS
    hold = _hold_of(held.order_id)
    order = db.session.get(Order, held.order_id)
    first = db.session.get(Order, held.first_id)
    # N-TZ: the response middleware re-zones every `created_at` to local time, so the earlier
    # order's placement is compared as an instant, not as a string with a UTC offset.
    placed_at = row["earlier_orders"][0].pop("created_at")
    assert datetime.fromisoformat(placed_at) == datetime.fromisoformat(_iso(first.created_at))
    assert row == {
        "order_id": held.order_id,
        "status": "pending",
        "requested_at": _iso(hold.requested_at),
        "decided_at": None,
        "agent": {"id": held.agent.id, "name": "Sardor Agent"},
        "outlet": {"id": held.outlet.id, "name": held.outlet.name},
        "visit_id": order.visit_id,
        "order": {
            **serialize_order_placed(order),
            "payment_method": "cash",
            "items": [{"product_id": sample_product.id, "product_name": "Pure Water 19L", "quantity": 3}],
        },
        "earlier_orders": [
            {
                **serialize_order_brief(first),
                "placed_by": {"id": held.agent.id, "name": "Sardor Agent"},
            }
        ],
        "decided_by": None,
        "reason": None,
        "self_decided": False,
        "can_decide": True,
    }
    # Order money is present, exactly as on /admin/orders; the earlier order's status is LIVE.
    assert row["order"]["total_amount"] == float(order.total_amount)
    assert row["earlier_orders"][0]["status"] == "confirmed"


def test_t_hold_11_the_queue_reads_in_a_fixed_number_of_queries(
    client, held, manager_claim_headers, sales_agent_auth_headers, sample_product, count_queries
):
    with count_queries() as one_row:
        assert client.get(APPROVALS, headers=manager_claim_headers).status_code == 200
    for _ in range(2):
        place_visit_order(client, sales_agent_auth_headers, held.outlet, _items(sample_product), "cash")

    with count_queries() as three_rows:
        response = client.get(APPROVALS, headers=manager_claim_headers)

    assert len(response.get_json()["data"]["items"]) == 3
    assert three_rows.count == one_row.count, three_rows.statements


# ---------------------------------------------------------------- T-HOLD-12 (clamp) and Review Focus 1


def test_t_hold_12_approving_after_the_stock_was_sold_clamps_it_at_zero(
    client, db, workplace, sales_agent_auth_headers, manager_claim_headers, sample_product, spies
):
    """R1-2, R29: approval never refuses for stock. A business-account order deducts at
    CONFIRMED; if the shelf was sold while it waited, the deduction clamps at 0."""
    _first, second = _hold_two(client, sales_agent_auth_headers, workplace, sample_product, "business_account")
    order_id = second["order"]["id"]
    # The 12-hour hold lapsed and another channel sold all but one unit.
    get_inventory_service().release_reservations(order_id)
    product = db.session.get(Product, sample_product.id)
    product.stock_quantity = 1
    db.session.commit()

    approved = client.post(f"{APPROVALS}/{order_id}/approve", json={}, headers=manager_claim_headers)

    assert approved.status_code == 200, approved.get_data(as_text=True)
    db.session.refresh(product)
    assert product.stock_quantity == 0
    assert db.session.get(Order, order_id).status is OrderStatus.CONFIRMED


def test_review_focus_1_a_held_order_approved_the_next_morning_goes_straight_to_the_pool(
    app, client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, manager_claim_headers,
    sample_product, driver_on_shift, spies, monkeypatch,
):
    """Managers decide the next morning (I-25, R31). The placement-time date rule is not
    re-applied: `withhold_reason` is None once `release_at` has passed, so the delivery is created
    and offered at once; the store gets its ordinary confirmation; the agent gets one push."""
    outlet = _store(client, db, sales_agent_auth_headers, operator_auth_headers)
    # Linked after the activation, whose `outlet_approved` push is not the decision's.
    sales_agent_user.telegram_id = "777000111"
    db.session.commit()
    today = local_date()
    # Placed at 10:00 local on D, so the "today" delivery date is valid whatever the real hour.
    freeze_local_now(monkeypatch, datetime.combine(today, time(10, 0), tzinfo=TASHKENT), visit_service=True)
    place_visit_order(client, sales_agent_auth_headers, outlet, _items(sample_product), "cash", delivery_date=today.isoformat())
    second = place_visit_order(
        client, sales_agent_auth_headers, outlet, _items(sample_product, 3), "cash", delivery_date=today.isoformat()
    )
    held_id = second["order"]["id"]
    assert second["confirmation"] == {"state": HELD}
    next_morning = datetime.combine(today + timedelta(days=1), time(9, 0), tzinfo=TASHKENT)
    freeze_local_now(monkeypatch, next_morning)
    monkeypatch.setattr(order_schedule_service_module, "get_utc_now", lambda: next_morning.astimezone(UTC))
    sent = []
    real_send = OrderService._send_order_notification

    def spy_send(self, order, notification_type):
        sent.append((order.id, notification_type))
        return real_send(self, order, notification_type)

    monkeypatch.setattr(OrderService, "_send_order_notification", spy_send)

    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=manager_claim_headers)

    assert approved.status_code == 200, approved.get_data(as_text=True)
    order = db.session.get(Order, held_id)
    assert (order.status, order.delivery_date) == (OrderStatus.CONFIRMED, today)
    assert order.delivery is not None
    assert held_id in _pool_order_ids(app, client, driver_on_shift)
    assert sent == [(held_id, "status_changed_confirmed")]
    hold = _hold_of(held_id)
    assert [(args[1], kwargs) for args, kwargs in spies.pushes] == [
        (SALES_EVENT_AGENT_ORDER_APPROVED, {"event_id": f"order-approval:{hold.id}:approved"})
    ]


# ---------------------------------------------------------------- T-HOLD-5: the rejection is itemised


def test_t_hold_5_a_rejected_order_is_itemised_in_the_exception_feed(
    client, db, held, manager_claim_headers, spies
):
    """§4.18.8: a rejection does not un-strike the visit; it is itemised as `rejected_agent_order`,
    a DATED type (its instant is the decision), so the managers' 08:00 count includes it."""
    reason = "Duplicate of the morning order"
    day = local_date()
    before = ExceptionFeedService.count_for_day(day)

    rejected = client.post(f"{APPROVALS}/{held.order_id}/reject", json={"reason": reason}, headers=manager_claim_headers)

    assert rejected.status_code == 200, rejected.get_data(as_text=True)
    feed = client.get(
        "/api/v1/admin/sales/exceptions", headers=manager_claim_headers, query_string={"type": "rejected_agent_order"}
    )
    assert feed.status_code == 200, feed.get_data(as_text=True)
    body = feed.get_json()["data"]
    hold = _hold_of(held.order_id)
    assert body["exceptions"] == [
        {
            "type": "rejected_agent_order",
            "occurred_at": _iso(hold.decided_at),
            "agent_user_id": held.agent.id,
            "agent_name": "Sardor Agent",
            "outlet_id": held.outlet.id,
            "outlet_name": held.outlet.name,
            "visit_id": db.session.get(Order, held.order_id).visit_id,
            "detail": {"order_id": held.order_id, "order_number": held.order_number, "reason": reason},
        }
    ]
    assert body["types"].index("rejected_agent_order") == body["types"].index("declined_agent_order") + 1
    assert ExceptionFeedService.count_for_day(day) == before + 1
