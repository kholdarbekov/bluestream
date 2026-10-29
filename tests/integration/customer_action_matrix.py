"""The one customer order state matrix (spec 2026-10-27 §13; plan 2026-10-27 Task 1).

Each `Row` is one order a customer can hold: its status, rail and payment status, its subscription, which side of
the payment deadline it is on, and whether the driver has it or it waits for a new date. `seed` builds that order
for a customer, and the row names what the published answer must say for it (`pay_refusal`, `has_deadline`,
`cancel_via`).

The published answer and the write guard read the same function, so the published-answer tests and the write
tests (`retry-payment`, `cancel`) all run over these rows, and they live here once. Tasks 3 and 4a add fields and
rows; a field's default keeps every existing row as it is.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.sales import Outlet
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.subscription import Subscription
from business_app.models.user import User, UserAddress
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from business_app.utils.local_windows import local_date
from business_app.utils.password_security import hash_password
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    SubscriptionFrequency,
    SubscriptionStatus,
    UserRole,
    UserType,
)

# Where the payment window started, before `now`: inside the 24 hours, or one hour past them.
PAYABLE_FOR = timedelta(hours=1)
PAST_DEADLINE = timedelta(hours=25)
# The delivery window every dated row is delivered in.
WINDOW: Tuple[time, time] = (time(10, 0), time(12, 0))
# A subscription row is delivered tomorrow unless it names its own day.
SUBSCRIPTION_DELIVERY_DAY = 1
# The sales agent who placed every agent row's order (one per database, found again on a second seed).
AGENT_PHONE = "+998901247001"
# How long the store has to answer an agent row's question.
QUESTION_OPEN_FOR = timedelta(hours=24)


@dataclass(frozen=True)
class Row:
    """One order shape and what its published answer must say.

    `rail` is the order's payment method; `payment` is its payment row's status (None: no payment row), on
    `payment_rail` when that differs from the order's. `collected` is the money already taken on that row.
    `delivery_date` is the delivery day in local days from `now` ("tomorrow" is 1), delivered 10:00-12:00;
    None leaves the order undated, except that a subscription row is delivered tomorrow.
    The order was created, and became payable on Click (`online_payable_since`), when its payment window started
    (`past_deadline`), unless `created_offset` / `payable_since_offset` name how long before `now` either happened.
    `pay_refusal` is what `payment_projection.pay_refusal` answers (None: Pay is allowed), and `has_deadline`
    whether an allowed Pay publishes a "Pay by" sentence (`OrderService.payment_deadline_applies`).
    `agent_pending` makes it an order a sales agent placed for the store (the customer), whose answer is still
    awaited (a pending `OrderConfirmationRequest`). `cancel_via` is the customer's published cancel
    (`customer_actions.cancel.via`): "skip", "cancel", "decline", or None when `customer_cancel_block_code` refuses.
    """

    id: str
    status: OrderStatus
    rail: PaymentMethod
    payment: Optional[PaymentStatus]
    payment_rail: Optional[PaymentMethod] = None
    collected: Decimal = Decimal("0")
    is_paid: bool = False
    total: Decimal = Decimal("30000.00")
    subscription: Optional[SubscriptionStatus] = None
    past_deadline: bool = False
    with_driver: bool = False
    awaiting_new_date: bool = False
    delivery_date: Optional[int] = None
    created_offset: Optional[timedelta] = None
    payable_since_offset: Optional[timedelta] = None
    pay_refusal: Optional[str] = None
    has_deadline: bool = True
    agent_pending: bool = False
    cancel_via: Optional[str] = None


_S = OrderStatus
_M = PaymentMethod
_P = PaymentStatus

ROWS = [
    Row("click_pending", _S.PENDING, _M.CLICK, _P.PENDING, cancel_via="cancel"),
    Row("click_failed", _S.PENDING, _M.CLICK, _P.FAILED, cancel_via="cancel"),
    Row("click_cancelled_attempt", _S.PENDING, _M.CLICK, _P.CANCELLED, cancel_via="cancel"),
    Row("click_no_payment_row", _S.PENDING, _M.CLICK, None, cancel_via="cancel"),
    Row(
        "click_past_deadline",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        past_deadline=True,
        pay_refusal="deadline_passed",
        cancel_via="cancel",
    ),
    # Created three days ago, payable on Click only for the last hour (a staff move onto Click, or the deploy's
    # backfill): its 24 hours count from that hour, not from its creation (spec §4.4, §17 Q3).
    Row(
        "cash_era_order_payable_recently",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        created_offset=timedelta(days=3),
        payable_since_offset=timedelta(hours=1),
        cancel_via="cancel",
    ),
    Row(
        "confirmed_click_past_deadline",
        _S.CONFIRMED,
        _M.CLICK,
        _P.PENDING,
        past_deadline=True,
        pay_refusal="deadline_passed",
        cancel_via="cancel",
    ),
    Row("preparing_click", _S.PREPARING, _M.CLICK, _P.PENDING, cancel_via="cancel"),
    # The driver has it, so it has no deadline (spec §4.4): Pay stays until it is paid.
    Row(
        "preparing_with_driver_past_deadline",
        _S.PREPARING,
        _M.CLICK,
        _P.PENDING,
        with_driver=True,
        past_deadline=True,
        has_deadline=False,
    ),
    Row(
        "out_for_delivery_past_deadline",
        _S.OUT_FOR_DELIVERY,
        _M.CLICK,
        _P.PENDING,
        past_deadline=True,
        has_deadline=False,
    ),
    Row(
        "awaiting_new_date_past_deadline",
        _S.CONFIRMED,
        _M.CLICK,
        _P.PENDING,
        awaiting_new_date=True,
        past_deadline=True,
        has_deadline=False,
    ),
    Row("delivered_click_failed", _S.DELIVERED, _M.CLICK, _P.FAILED, has_deadline=False),
    # The shape of dev order 12: a legacy Payme order, delivered, its Payme attempt cancelled.
    Row("delivered_payme_cancelled", _S.DELIVERED, _M.PAYME, _P.CANCELLED, has_deadline=False),
    # The shape of dev order 76: a card order whose payment row is a cancelled Payme attempt.
    Row(
        "delivered_card_order_payme_row",
        _S.DELIVERED,
        _M.CARD,
        _P.CANCELLED,
        payment_rail=_M.PAYME,
        has_deadline=False,
    ),
    Row(
        "click_partly_paid",
        _S.PENDING,
        _M.CLICK,
        _P.PARTIALLY_PAID,
        collected=Decimal("10000.00"),
        pay_refusal="partly_paid",
    ),
    Row(
        "delivered_click_some_collected",
        _S.DELIVERED,
        _M.CLICK,
        _P.PENDING,
        collected=Decimal("10000.00"),
        pay_refusal="partly_paid",
    ),
    Row("click_completed_flag_lagging", _S.PENDING, _M.CLICK, _P.COMPLETED, pay_refusal="already_paid"),
    Row("click_paid", _S.CONFIRMED, _M.CLICK, _P.COMPLETED, is_paid=True, pay_refusal="already_paid"),
    # Review Focus 5: nothing to pay is not a Pay.
    Row(
        "click_zero_total",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        total=Decimal("0.00"),
        pay_refusal="already_paid",
        cancel_via="cancel",
    ),
    Row("click_cancelled", _S.CANCELLED, _M.CLICK, _P.CANCELLED, pay_refusal="order_closed"),
    Row("click_returned", _S.RETURNED, _M.CLICK, _P.CANCELLED, pay_refusal="order_closed"),
    Row("cash_pending", _S.PENDING, _M.CASH, _P.PENDING, pay_refusal="not_online", cancel_via="cancel"),
    # An online order on an offline payment row. Every rail writer moves both columns, so only legacy data holds
    # this; the write would refuse to move the row off cash (RAIL_MOVE_NOT_ALLOWED), so no Pay is published.
    Row(
        "click_order_cash_row",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        payment_rail=_M.CASH,
        pay_refusal="not_online",
        cancel_via="cancel",
    ),
    Row(
        "business_account_unsettled",
        _S.PENDING,
        _M.BUSINESS_ACCOUNT,
        _P.PENDING,
        pay_refusal="not_online",
        cancel_via="cancel",
    ),
    Row(
        "sub_active_click",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        subscription=SubscriptionStatus.ACTIVE,
        cancel_via="skip",
    ),
    # A leftover of a paused or cancelled subscription is cancelled on its own (D11).
    Row(
        "sub_paused_click",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        subscription=SubscriptionStatus.PAUSED,
        cancel_via="cancel",
    ),
    Row(
        "sub_cancelled_click",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        subscription=SubscriptionStatus.CANCELLED,
        cancel_via="cancel",
    ),
    # A subscription order may be paid until its delivery window is over, even past the 24 hours.
    Row(
        "sub_click_past_24h_inside_window",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        subscription=SubscriptionStatus.ACTIVE,
        past_deadline=True,
        delivery_date=1,
        cancel_via="skip",
    ),
    Row(
        "sub_click_past_deadline",
        _S.PENDING,
        _M.CLICK,
        _P.PENDING,
        subscription=SubscriptionStatus.ACTIVE,
        past_deadline=True,
        delivery_date=-1,
        pay_refusal="deadline_passed",
        cancel_via="skip",
    ),
    # A sales agent placed it for the store, and the store has not answered yet (D13): the store's own
    # Cancel declines it and tells the agent. Agent orders are cash or bank transfer in the real flow; the
    # Click one is the rail an admin can move it to.
    Row("agent_pending_click", _S.PENDING, _M.CLICK, _P.PENDING, agent_pending=True, cancel_via="decline"),
    Row(
        "agent_pending_cash",
        _S.PENDING,
        _M.CASH,
        _P.PENDING,
        agent_pending=True,
        pay_refusal="not_online",
        cancel_via="decline",
    ),
]

ROW = {row.id: row for row in ROWS}
assert len(ROW) == len(ROWS), "row ids must be unique"


def seed(db, user, row: Row, *, now: datetime) -> Order:
    """Build `row`'s order for `user`, as the real flows leave it, with the clock at `now` (an aware instant).

    The payment window starts at `now - 25h` when the row is past its deadline, else at `now - 1h`; the order is
    created and stamped payable then, unless the row names its own offsets. The order has an address of its own;
    a subscription row's order belongs to a weekly subscription in that status, whose next delivery is a week after
    this one. An agent row's order was placed by a sales agent at an outlet of `user`, who has not answered yet."""
    start = now - (PAST_DEADLINE if row.past_deadline else PAYABLE_FOR)
    address = UserAddress(
        user_id=user.id, full_address="1 Matrix street", city="Tashkent", latitude=41.3111, longitude=69.2797
    )
    db.session.add(address)
    db.session.flush()

    offset = row.delivery_date
    if offset is None and row.subscription is not None:
        offset = SUBSCRIPTION_DELIVERY_DAY
    day = local_date(now) + timedelta(days=offset) if offset is not None else None

    subscription = _subscription(db, user, row, address, day, now) if row.subscription is not None else None
    order = Order(
        user_id=user.id,
        status=row.status,
        subtotal=row.total,
        total_amount=row.total,
        payment_method=row.rail,
        is_paid=row.is_paid,
        delivery_address_id=address.id,
        delivery_date=day,
        delivery_window_start=WINDOW[0] if day is not None else None,
        delivery_window_end=WINDOW[1] if day is not None else None,
        is_subscription_order=subscription is not None,
        subscription_id=subscription.id if subscription is not None else None,
    )
    if row.agent_pending:
        order.order_source = ORDER_SOURCE_SALES_AGENT
        order.created_by_staff_id = _agent(db).id
    db.session.add(order)
    db.session.flush()
    order.created_at = now - row.created_offset if row.created_offset is not None else start
    order.online_payable_since = now - row.payable_since_offset if row.payable_since_offset is not None else start
    if row.payment is not None:
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=user.id,
                amount=row.total,
                payment_method=row.payment_rail or row.rail,
                status=row.payment,
                amount_collected=row.collected,
                outstanding_amount=row.total - row.collected,
            )
        )
    if row.with_driver:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.PICKED_UP,
                scheduled_date=now,
                scheduled_time_slot="anytime",
            )
        )
    if row.awaiting_new_date:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.FAILED,
                failed_delivery_reason="customer_unavailable",
                delivery_attempts=1,
                scheduled_date=now - timedelta(hours=1),
                scheduled_time_slot="anytime",
            )
        )
    if row.agent_pending:
        _question(db, user, order, start)
    db.session.commit()
    return order


def _agent(db) -> User:
    """The sales agent who placed the agent rows' orders."""
    agent = User.query.filter_by(phone=AGENT_PHONE).one_or_none()
    if agent is None:
        agent = User(
            phone=AGENT_PHONE,
            password_hash=hash_password("AgentPassword123!"),
            first_name="Matrix",
            last_name="Agent",
            user_type=UserType.STAFF,
            role=UserRole.SALES_AGENT,
            is_verified=True,
        )
        db.session.add(agent)
        db.session.flush()
    return agent


def _question(db, user, order: Order, asked_at: datetime) -> None:
    """The store's still unanswered Confirm/Decline question on `order`, at an outlet of `user`
    (`OrderConfirmationRequest.outlet_id` is NOT NULL)."""
    outlet = Outlet(name="Matrix shop", outlet_type="grocery_store", user_id=user.id)
    db.session.add(outlet)
    db.session.flush()
    db.session.add(
        OrderConfirmationRequest(
            order_id=order.id,
            outlet_id=outlet.id,
            status="pending",
            requested_at=asked_at,
            expires_at=asked_at + QUESTION_OPEN_FOR,
            request_id=secrets.token_hex(8),
        )
    )


def _subscription(db, user, row: Row, address: UserAddress, day, now: datetime) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        status=row.subscription,
        name="Home water",
        frequency=SubscriptionFrequency.WEEKLY,
        billing_amount=row.total,
        delivery_day_of_week=day.weekday(),
        delivery_address_id=address.id,
        next_delivery_date=day + timedelta(days=7),
        start_date=now - timedelta(days=28),
        payment_method=row.rail,
        paused_at=now if row.subscription == SubscriptionStatus.PAUSED else None,
        end_date=now if row.subscription == SubscriptionStatus.CANCELLED else None,
    )
    db.session.add(subscription)
    db.session.flush()
    return subscription
