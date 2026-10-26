"""Builders for the reorder-reminder suites (plan 2026-10-15, Phase 2 onwards).

THE builders module of the plan (master Global Constraints): the delivered-order, episode,
setting, `lock` and `clock` builders and fixtures live here. Later tasks and phases import and
extend it; they never copy a builder into a test file or start a second module.

A builder writes the rows the real writers leave, and nothing they would not:

* a delivered order is an `orders` row in DELIVERED with ONE DELIVERED `order_status_history` row
  at the instant the test names. That row IS the delivered instant (spec §4), and it is the shape
  `tests/integration/sales_pay_builders.deliver` leaves after `backdate_delivered`;
* every delivery-bearing order carries a delivery address unless a test asks for `NO_ADDRESS`,
  because Postgres refuses one without it (`ck_orders_address_required_after_pending`), so the
  same builders run on `pg_db`;
* an episode is the row the engine writes, with `created_at` on the test's clock, because the
  close rule compares a superseding delivery with it (spec §6);
* every instant is WRITTEN in UTC (`_utc`), as the real writers write it. SQLite drops the offset
  and hands the column back naive, which every reader takes as UTC (`ensure_utc`), so a local
  instant written as it is would come back five hours late; Postgres stores either form alike.

Every instant is LOCAL (Asia/Tashkent) and counted in days from `DAY0`, so a test reads like the
spec's ladder table: `at(11)` is noon on day 11 and `run_at(11)` the 10:00 run that day.
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from itertools import count
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.customer_link import AddressGroup, CanonicalCustomer
from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent
from business_app.models.user import User, UserAddress
from business_app.utils.order_timing import delivered_instants_by_order
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserStatus, UserType
from shared.user_names import GENERATED_TELEGRAM_NAME

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
DAY0 = date(2026, 3, 2)
NO_ADDRESS = object()
_AUTO = object()
_SEQ = count(1)

# Delivery days of a weekly customer: usual gap 7, threshold 10.5 -> R1 day 11, R2 day 18, E day 25.
WEEKLY = (-21, -14, -7, 0)


def day(n: int) -> date:
    """Day `n` of the ladder; DAY0 is day 0."""
    return DAY0 + timedelta(days=n)


def at(n: int, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    """A local wall-clock instant on day `n`."""
    return datetime.combine(day(n), time(hour, minute, second), tzinfo=TASHKENT)


def run_at(n: int, second: int = 0, *, minute: int = 0) -> datetime:
    """The 10:00 daily run on day `n` (`minute` past it for a later run the same day)."""
    return at(n, 10, minute, second)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """The same instant in UTC, the form a real writer stores (see the module docstring)."""
    return value.astimezone(timezone.utc) if value is not None else None


def make_customer(
    db,
    *,
    first_name: Optional[str] = "Ali",
    last_name=_AUTO,
    phone=_AUTO,
    email: Optional[str] = None,
    telegram: bool = False,
    blocked_at: Optional[datetime] = None,
    last_bot_interaction: Optional[datetime] = None,
    language: Optional[str] = "uz",
    status: UserStatus = UserStatus.ACTIVE,
    user_type: UserType = UserType.INDIVIDUAL,
    role: UserRole = UserRole.CUSTOMER,
    staff_roles: Optional[List[str]] = None,
    entity_subtype=None,
    company_name: Optional[str] = None,
    registration_source: str = "telegram",
    created_at: Optional[datetime] = None,
) -> User:
    """One account. `telegram=True` connects the customer bot. `blocked_at` is the state
    `TelegramReachabilityService.mark_blocked` leaves (Task 4): bot inactive, block stamped.
    The last name defaults to "Test", except beside the generated first name, where it is the
    empty string both Telegram sign-up paths write with it (`AuthService`). `role` / `user_type` /
    `staff_roles` build a staff account too: `staff_roles` is the JSON list a multi-role account
    carries beside its one `role` (an operator who is also a manager)."""
    n = next(_SEQ)
    if last_name is _AUTO:
        last_name = "" if first_name == GENERATED_TELEGRAM_NAME else "Test"
    user = User(
        first_name=first_name,
        last_name=last_name,
        phone=f"+99897{n:07d}" if phone is _AUTO else phone,
        email=email,
        password_hash="not-a-real-hash",
        role=role,
        staff_roles=list(staff_roles or []),
        user_type=user_type,
        status=status,
        entity_subtype=entity_subtype,
        company_name=company_name,
        preferred_language=language,
        telegram_id=str(770000000 + n) if (telegram or blocked_at is not None) else None,
        is_bot_active=telegram and blocked_at is None,
        telegram_blocked_at=_utc(blocked_at),
        last_bot_interaction=_utc(last_bot_interaction),
        registration_source=registration_source,
        created_at=_utc(created_at or at(-400)),
    )
    db.session.add(user)
    db.session.commit()
    return user


def customer_headers(app, user: User) -> Dict[str, str]:
    """The customer JWT the bot sends with every call. Only `Authorization`: the bot's `json=` adds
    its own Content-Type, and a body-less call (Later) has none."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def claim_headers(app, user: User, role: str) -> Dict[str, str]:
    """A JWT whose role CLAIM is `role`, the shape the real login issues. Routes behind
    `manager_or_higher_required` read the claim and cross-check the user row. A test uses the
    conftest `admin_claim_headers` / `manager_claim_headers` for those two roles, and this one for
    every other role (customer, operator, sales_agent, delivery_driver)."""
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": role})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def link(db, *users: User) -> CanonicalCustomer:
    """Link accounts as one person: the state `CustomerLinkService.link_accounts` leaves."""
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.flush()
    for user in users:
        user.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def home_address(db, user: User) -> UserAddress:
    """The user's ungrouped 'home' address, created on first use."""
    address = UserAddress.query.filter_by(user_id=user.id, title="home", address_group_id=None).first()
    if address is None:
        address = UserAddress(
            user_id=user.id,
            title="home",
            full_address=f"{user.id} Home St, Tashkent",
            street_address=f"{user.id} Home St",
            city="Tashkent",
            latitude=41.3111,
            longitude=69.2797,
        )
        db.session.add(address)
        db.session.commit()
    return address


def branch_address(db, user: User, label: str) -> UserAddress:
    """Another ungrouped address of `user`: one branch of a chain account, by its `label`."""
    address = UserAddress(
        user_id=user.id,
        title=label,
        full_address=f"{user.id} {label} St, Tashkent",
        street_address=f"{user.id} {label} St",
        city="Tashkent",
        # A few metres apart, always inside the delivery zone the address model enforces.
        latitude=41.3111 - (next(_SEQ) % 100) * 0.0001,
        longitude=69.2797,
    )
    db.session.add(address)
    db.session.commit()
    return address


def place(db, *users: User, label: str = "office") -> Dict[int, UserAddress]:
    """One place group holding one 'work' address per user (the conftest `place` shape)."""
    group = AddressGroup(label=label)
    db.session.add(group)
    db.session.flush()
    addresses = {}
    for index, user in enumerate(users):
        address = UserAddress(
            user_id=user.id,
            title="work",
            address_group_id=group.id,
            full_address="1 Office St, Tashkent",
            street_address="1 Office St",
            city="Tashkent",
            latitude=41.2746 - index * 0.0001,
            longitude=69.2061 + index * 0.0001,
        )
        db.session.add(address)
        addresses[user.id] = address
    db.session.commit()
    return addresses


def leave_place(db, address: UserAddress) -> None:
    """The address leaves its place group: the membership FK cleared."""
    address.address_group_id = None
    db.session.commit()


def _order(db, user: User, *, status: OrderStatus, created_at: datetime, address, delivery_date=None) -> Order:
    address_id = None if address is NO_ADDRESS else (address or home_address(db, user)).id
    order = Order(
        order_number=f"RR-{next(_SEQ):06d}",
        user_id=user.id,
        status=status,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address_id,
        delivery_date=delivery_date,
        order_source="telegram",
        created_at=_utc(created_at),
    )
    db.session.add(order)
    db.session.flush()
    return order


def _delivered_history(order: Order, when: datetime) -> OrderStatusHistory:
    """The ONE DELIVERED history row of an order: its instant is the delivered instant (spec §4)."""
    return OrderStatusHistory(
        order_id=order.id,
        old_status=OrderStatus.OUT_FOR_DELIVERY,
        new_status=OrderStatus.DELIVERED,
        changed_at=_utc(when),
    )


def delivered_order(db, user: User, when: datetime, *, address=None) -> Order:
    """A DELIVERED order whose delivered instant is `when`."""
    order = _order(db, user, status=OrderStatus.DELIVERED, created_at=when - timedelta(hours=3), address=address)
    db.session.add(_delivered_history(order, when))
    db.session.commit()
    return order


def deliver(db, order: Order, when: datetime) -> Order:
    """A live order is delivered at `when`: DELIVERED, with the one history row `delivered_order` leaves."""
    order.status = OrderStatus.DELIVERED
    db.session.add(_delivered_history(order, when))
    db.session.commit()
    return order


def legacy_delivered_order(db, user: User, created_at: datetime, *, address=None) -> Order:
    """A DELIVERED order with NO DELIVERED history row: the legacy shape dev still holds twice
    (Review Focus #2). It has no delivered instant."""
    order = _order(db, user, status=OrderStatus.DELIVERED, created_at=created_at, address=address)
    db.session.commit()
    return order


def live_order(
    db,
    user: User,
    created_at: datetime,
    *,
    status: OrderStatus = OrderStatus.PENDING,
    delivery_date: Optional[date] = None,
    address=None,
    failed: bool = False,
) -> Order:
    """A live order. `failed=True` adds the FAILED delivery that leaves it awaiting a new date."""
    order = _order(db, user, status=status, created_at=created_at, address=address, delivery_date=delivery_date)
    if failed:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.FAILED,
                failed_delivery_reason="customer_unavailable",
                delivery_attempts=1,
                scheduled_date=_utc(created_at),
                scheduled_time_slot="anytime",
            )
        )
    db.session.commit()
    return order


def connect_telegram(db, user: User) -> None:
    """The customer connects the bot: the `telegram_id` and active bot that registration leaves."""
    user.telegram_id = str(770000000 + next(_SEQ))
    user.is_bot_active = True
    db.session.commit()


def set_status(db, order: Order, status: OrderStatus) -> None:
    order.status = status
    db.session.commit()


def customer_with_deliveries(db, days: Iterable[int], *, address=None, **customer_fields) -> Tuple[User, List[Order]]:
    """A customer and one delivered order at noon on each of `days`, oldest first."""
    user = make_customer(db, **customer_fields)
    return user, [delivered_order(db, user, at(n), address=address) for n in days]


def subscription(db, user: User, *, status):
    """An auto-delivery plan in `status` on `user`'s home address."""
    from business_app.models.subscription import Subscription
    from shared.enums import PaymentMethod, SubscriptionFrequency

    plan = Subscription(
        subscription_number=f"RR-SUB-{next(_SEQ):05d}",
        user_id=user.id,
        status=status,
        name="Standard",
        frequency=SubscriptionFrequency.WEEKLY,
        billing_amount=Decimal("50000.00"),
        next_delivery_date=(_utc(at(7))).date(),
        delivery_address_id=home_address(db, user).id,
        payment_method=PaymentMethod.CARD,
        start_date=_utc(at(-30)),
    )
    db.session.add(plan)
    db.session.commit()
    return plan


def subscription_delivery(db, user: User, plan, when: datetime, *, address=None) -> Order:
    """A DELIVERED order that `plan` billed. `subscription_id` and `is_subscription_order` are set
    together, as `OrderService.create_order` writes them (order_service.py:302-303)."""
    order = delivered_order(db, user, when, address=address)
    order.subscription_id = plan.id
    order.is_subscription_order = True
    db.session.commit()
    return order


def outlet(
    db,
    customer: User,
    *,
    stage: str,
    agent: User,
    dormant_since: Optional[datetime] = None,
    address: Optional[UserAddress] = None,
):
    """A sales outlet on `customer`'s account, in `stage`, owned by `agent`. `dormant_since` writes
    the `dormant` stage-history row `OutletService._record_stage` leaves when the stage job moves it.
    `address` is the outlet's own delivery address: two live outlets with addresses on one account
    are a chain in branch mode (D25), each reading only the orders delivered to its own address."""
    from business_app.models.sales import Outlet

    row = Outlet(
        name=f"Shop {next(_SEQ):03d}",
        outlet_type="grocery_store",
        stage=stage,
        district="chilanzar",
        user_id=customer.id,
        address_id=address.id if address is not None else None,
        assigned_agent_user_id=agent.id,
        onboarded_by_user_id=agent.id,
        created_at=_utc(at(-200)),
    )
    db.session.add(row)
    db.session.flush()
    if dormant_since is not None:
        db.session.add(_dormant_history(row, dormant_since))
    db.session.commit()
    return row


def turn_dormant(db, row, since: datetime) -> None:
    """The 01:10 stage job's move of an outlet to `dormant` at `since`: the stage, and its history row."""
    row.stage = "dormant"
    db.session.add(_dormant_history(row, since))
    db.session.commit()


def _dormant_history(row, since: datetime):
    """The `dormant` stage-history row `OutletService._record_stage` leaves, written in UTC as it writes it."""
    from business_app.models.sales import OutletStageHistory

    return OutletStageHistory(outlet_id=row.id, from_stage="at_risk", to_stage="dormant", created_at=_utc(since))


def make_episode(
    db,
    user: User,
    *,
    anchor: Order,
    supply: Optional[Order] = None,
    status: str = "open",
    step: int = 0,
    next_action_on: Optional[date] = None,
    is_dry_run: bool = False,
    created_at: Optional[datetime] = None,
    escalation_flag: Optional[str] = None,
    suspended_reason: Optional[str] = None,
) -> ReorderReminderEpisode:
    """An episode in a state the §10 state matrix allows, as the engine leaves it. A suspended row
    with an `escalation_flag` is one that was escalated before it was suspended."""
    supply = supply or anchor
    instants = delivered_instants_by_order([anchor.id, supply.id])
    created = _utc(created_at or run_at(11))
    escalated = status in ("escalated", "resolved") or (status == "suspended" and escalation_flag is not None)
    if next_action_on is None and status in ("open", "suspended") and not escalated:
        next_action_on = day(11)
    episode = ReorderReminderEpisode(
        user_id=user.id,
        anchor_order_id=anchor.id,
        anchor_delivered_at=instants[anchor.id],
        supply_order_id=supply.id,
        supply_delivered_at=instants[supply.id],
        usual_gap_days=7,
        threshold_days=Decimal("10.5"),
        due_on=day(11),
        status=status,
        step=step,
        reminders_sent=0,
        next_action_on=next_action_on,
        escalated_at=created if escalated else None,
        escalation_flag=(escalation_flag or "no_response") if escalated else None,
        suspended_at=created if status == "suspended" else None,
        suspended_reason=(suspended_reason or "admin_off") if status == "suspended" else None,
        resolved_at=created if status == "resolved" else None,
        outcome="other" if status == "resolved" else None,
        closed_at=created if status == "closed" else None,
        close_reason="ordered" if status == "closed" else None,
        is_dry_run=is_dry_run,
        created_at=created,
        updated_at=created,
    )
    db.session.add(episode)
    db.session.commit()
    return episode


def lapses(anchor: Order, *, is_dry_run: bool = False) -> List[ReorderReminderEpisode]:
    """Every episode whose OWN anchor is `anchor`, oldest first, closed ones included."""
    return (
        ReorderReminderEpisode.query.filter_by(anchor_order_id=anchor.id, is_dry_run=is_dry_run)
        .order_by(ReorderReminderEpisode.id)
        .all()
    )


def only_lapse(anchor: Order, *, is_dry_run: bool = False) -> ReorderReminderEpisode:
    rows = lapses(anchor, is_dry_run=is_dry_run)
    assert len(rows) == 1, f"expected one episode for order {anchor.id}, found {[(r.id, r.status) for r in rows]}"
    return rows[0]


def event_rows(*, episode=None, user=None) -> List[Tuple[str, Optional[str], Optional[int], Optional[str]]]:
    """`(kind, detail, step, channel)` of the matching events, oldest first."""
    query = ReorderReminderEvent.query
    if episode is not None:
        query = query.filter(ReorderReminderEvent.episode_id == episode.id)
    if user is not None:
        query = query.filter(ReorderReminderEvent.user_id == user.id)
    return [(e.kind, e.detail, e.step, e.channel) for e in query.order_by(ReorderReminderEvent.id).all()]


def daily_engine(n: int, *, mode: str = "on", second: int = 0, minute: int = 0, **config):
    """The engine of one daily run on day `n` at 10:00 (plus `minute` and `second`), in `mode`, not
    yet run. The app's other tunables apply unless `config` overrides them by their Flask names
    (e.g. `REORDER_REMINDER_RATIO="1.25"`)."""
    from flask import current_app

    from business_app.services.reorder_reminders.config import ReminderConfig
    from business_app.services.reorder_reminders.engine import ReorderReminderEngine

    cfg = ReminderConfig.from_mapping({**current_app.config, **config, "REORDER_REMINDERS_MODE": mode})
    return ReorderReminderEngine(cfg, now=run_at(n, second, minute=minute))


def run_engine(n: int, *, mode: str = "on", second: int = 0, minute: int = 0, **config):
    """One daily run on day `n` at 10:00 (plus `minute` and `second`), in `mode`, with the app's
    other tunables."""
    return daily_engine(n, mode=mode, second=second, minute=minute, **config).run()


def capture_sends(monkeypatch) -> List[Tuple[int, int]]:
    """Replace the after-commit publisher with a recorder that keeps its exact signature.
    Returns the `(episode_id, step)` pairs published, in order."""
    from business_app.utils import reorder_reminder_dispatch

    published: List[Tuple[int, int]] = []

    def publish(episode_id: int, step: int) -> None:
        published.append((episode_id, step))

    monkeypatch.setattr(reorder_reminder_dispatch, "_publish_send", publish)
    return published


@pytest.fixture(name="sends")
def sends_fixture(monkeypatch) -> List[Tuple[int, int]]:
    """`sends`: every `(episode_id, step)` the after-commit hook published, in order; no real task is
    published. A test module imports `sends_fixture` to use it (as with `store_fixture`)."""
    return capture_sends(monkeypatch)


class FakeRedis:
    """The three calls the run lock and the email claim make, with Redis' SET NX EX semantics and
    nothing more."""

    def __init__(self):
        self.store, self.expiries = {}, {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value.encode() if isinstance(value, str) else value
        self.expiries[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture(name="lock")
def lock_fixture(monkeypatch) -> FakeRedis:
    """`lock`: the daily run's Redis lock, faked: `reorder_reminder_tasks.redis_client` becomes a
    `FakeRedis`. A test module imports `lock_fixture` to use it (as with `store_fixture`)."""
    from business_app.tasks import reorder_reminder_tasks

    fake = FakeRedis()
    monkeypatch.setattr(reorder_reminder_tasks, "redis_client", fake)
    return fake


@pytest.fixture(name="email_claims")
def email_claims_fixture(monkeypatch) -> FakeRedis:
    """`email_claims`: the store of the reminder email's (episode, step) claims, faked:
    `messaging.redis_client` becomes a `FakeRedis`, so no test claims in the Redis DBs that test runs
    share. A test that drives `_send_email` for real uses it; a test module imports
    `email_claims_fixture` to use it (as with `store_fixture`)."""
    from business_app.services.reorder_reminders import messaging

    fake = FakeRedis()
    monkeypatch.setattr(messaging, "redis_client", fake)
    return fake


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch) -> Callable[[datetime], datetime]:
    """`clock`: freeze the wall clock the reminder tasks read. `clock(when)` freezes `get_utc_now` in
    the task module and in `messaging` at `when` and returns it as UTC. A later module that reads
    the wall clock on the same path is patched here too, never in a second fixture. A test module
    imports `clock_fixture` to use it (as with `store_fixture`).

    The customer actions (`actions`) read the same clock when a route calls them without a `now`
    (Later and Stop over HTTP), so a frozen day reaches them too."""
    from business_app.services.reorder_reminders import actions, messaging
    from business_app.tasks import reorder_reminder_tasks

    def freeze(when: datetime) -> datetime:
        frozen = when.astimezone(timezone.utc)
        monkeypatch.setattr(reorder_reminder_tasks, "get_utc_now", lambda: frozen)
        monkeypatch.setattr(messaging, "get_utc_now", lambda: frozen)
        monkeypatch.setattr(actions, "get_utc_now", lambda: frozen)
        return frozen

    return freeze


@pytest.fixture(name="mode")
def mode_fixture(monkeypatch, app) -> Callable[[str], None]:
    """`mode`: `mode("on")` sets the running app's `REORDER_REMINDERS_MODE` for this test only, the
    value `current_config()` reads in the actions and the send task (`run_engine` takes its own
    `mode=`). A test module imports `mode_fixture` to use it (as with `store_fixture`)."""

    def set_mode(value: str) -> None:
        monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", value)

    return set_mode


@pytest.fixture(name="store", params=["db", "pg_db"])
def store_fixture(request):
    """`store`: the scenario's database, twice: the SQLite suite and a migrated Postgres, where the
    partial unique index and the RESTRICT foreign keys are real. A test module imports
    `store_fixture` to use it (the other name keeps the `store` argument from shadowing the import)."""
    return request.getfixturevalue(request.param)


@contextmanager
def captured_logs(caplog, logger: logging.Logger, level: int = logging.ERROR) -> Iterator[None]:
    """`caplog` for one service module's logger.

    The app sets `propagate = False` on the `business_app` logger (business_app/utils/logging_config.py),
    so caplog's root handler never sees a service module's records. For the block, caplog's handler
    is attached to `logger` itself, at `level`."""
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(level, logger=logger.name):
            yield
    finally:
        logger.removeHandler(caplog.handler)


# ---------------------------------------------------------------------------------------------
# Phase 3 (customer surfaces): what a reminder's words are built from. `delivered_order` above
# writes no order lines. These add the lines, the products they name (with a name per language
# when a test needs one) and the seeded copy rows the bot and the email render. The imports stay
# local, so this block needs nothing from the top of the module but `Decimal`, `Dict`,
# `Iterable`, `List` and `Order`.
# ---------------------------------------------------------------------------------------------

UNIT_PRICE = Decimal("15000.00")


def make_product(db, category, name: str):
    """An active product in `category`. `name` is the column; `name_product` adds per-language names."""
    from business_app.models.product import Product

    product = Product(
        name=name,
        category_id=category.id,
        size="19L",
        base_price=UNIT_PRICE,
        stock_quantity=100,
        is_active=True,
    )
    db.session.add(product)
    db.session.commit()
    return product


def add_order_lines(db, order: Order, lines=(), *, reward_lines=()) -> Order:
    """`(product, qty)` lines on `order`, as checkout writes them. `reward_lines` are free loyalty
    lines (`is_reward_item`), which a reminder never calls "your usual"."""
    from business_app.models.order import OrderItem

    for product, qty in lines:
        db.session.add(
            OrderItem(order_id=order.id, product_id=product.id, quantity=qty,
                      unit_price=UNIT_PRICE, total_price=UNIT_PRICE * qty)
        )
    for product, qty in reward_lines:
        db.session.add(
            OrderItem(order_id=order.id, product_id=product.id, quantity=qty,
                      unit_price=Decimal("0.00"), total_price=Decimal("0.00"), is_reward_item=True)
        )
    db.session.commit()
    return order


def name_product(db, product, names: Dict[str, str]) -> None:
    """The product's name in each of `names`' languages: the per-language rows an admin writes.
    Give all three, because `get_translated` falls back across languages for a missing one."""
    for language, name in names.items():
        product.set_translated("name", name, language)
    db.session.commit()


def reminder_copy_keys() -> List[str]:
    """Every key the composer renders: the reminder rows plus the two menu labels stop_ok names."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    own = sorted(key for key in BACKEND_TRANSLATIONS if key.startswith("telegram.reorder_reminder."))
    return own + ["telegram.menu.profile", "telegram.profile.notifications"]


def seed_translations(db, keys: Iterable[str]) -> None:
    """The keys' rows exactly as the canonical seeder ships them."""
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for key in keys:
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


# What `trigger_bot_webhook` hands back from the customer bot's `/internal/reorder-reminder`
# route: the bot's own 200 body when it sent the message, and when it recognised a request id
# (or the same episode step) it had already processed (telegram_bot/webhook_server.py); and
# `trigger_bot_webhook`'s own shape for a non-200 (business_app/utils/bot_webhook.py): the route's
# body is NOT passed through. Only its `message` survives, prefixed "Bot webhook failed: ", and
# the HTTP status becomes `status_code`; the body's `error_code` is dropped, which is why the
# backend reads the status. BOT_BLOCKED is that shape for the route's 410
# ({"success": False, "error_code": "BOT_BLOCKED", "message": "Bot was blocked by the user"}),
# returned when Telegram refused the send because the account blocked the bot.
BOT_DELIVERED = {"success": True, "message": "Notification sent"}
BOT_DEDUPLICATED = {"success": True, "message": "Already processed (deduplicated)"}
BOT_BLOCKED = {"success": False, "message": "Bot webhook failed: Bot was blocked by the user", "status_code": 410}


@pytest.fixture(name="bot_webhook")
def bot_webhook_fixture(monkeypatch):
    """`bot_webhook`: the HTTP hop to the customer bot, replaced. `trigger_bot_webhook` in
    `messaging` becomes a recorder with its exact signature; no request leaves the test.

    Each POST is recorded in `calls` as `{"endpoint", "payload", "request_id"}` and answered with
    `answer` (`BOT_DELIVERED` until a test sets another), or with `answer(call)` when `answer` is
    callable: a test that needs something to happen while the POST is in flight. A test module
    imports `bot_webhook_fixture` to use it (as with `store_fixture`)."""
    from types import SimpleNamespace

    from business_app.services.reorder_reminders import messaging

    state = SimpleNamespace(calls=[], answer=BOT_DELIVERED)

    def trigger(endpoint, payload=None, *, request_id=None):  # trigger_bot_webhook's own signature
        call = {"endpoint": endpoint, "payload": dict(payload or {}), "request_id": request_id}
        state.calls.append(call)
        return state.answer(call) if callable(state.answer) else state.answer

    monkeypatch.setattr(messaging, "trigger_bot_webhook", trigger)
    return state


# ---------------------------------------------------------------------------
# Staff surfaces (plan Phase 4: worklist, summary, resolve, digest, analytics). Appended by Task 20a.
# ---------------------------------------------------------------------------

# 10:00 local (Asia/Tashkent) on 15 October 2026, day 227 of the ladder. The staff-surface tests
# freeze the business clock here (the `frozen` fixture, or the `clock` fixture).
WORKLIST_NOW = run_at(227)


@pytest.fixture(name="frozen")
def frozen_fixture(monkeypatch) -> datetime:
    """`frozen`: freeze the business clock (`local_now`, so "today" and the 30-day windows) at
    `WORKLIST_NOW` and return that instant in Tashkent time. A test module imports `frozen_fixture`
    to use it (as with `store_fixture`). The wall clock the writers stamp events with
    (`get_utc_now`) is NOT frozen: a test that needs an event at a given instant passes it to
    `record_event_at` or the writer's `now`/`at`."""
    from tests.integration.sales_pay_builders import freeze_local_now

    return freeze_local_now(monkeypatch, WORKLIST_NOW)


def staff_episode(
    db,
    user: User,
    *,
    anchor: Order,
    status: str,
    due_on: date,
    escalation_flag: Optional[str] = None,
    escalated_at: Optional[datetime] = None,
    usual_gap_days: Optional[int] = None,
    threshold_days: Decimal = Decimal("30.0"),
    reminders_sent: int = 0,
    outcome: Optional[str] = None,
    close_reason: Optional[str] = None,
    closed_at: Optional[datetime] = None,
    created_at: Optional[datetime] = None,
    **ladder_fields,
) -> ReorderReminderEpisode:
    """`make_episode`, then the columns the staff surfaces read and the ladder tests fix.

    The worklist, the summary metrics and the digest sort, count and print `due_on`, the gap and
    threshold, `reminders_sent`, the outcome and the close, which `make_episode` pins to the
    ladder's day 11. The episode opens at 10:00 local on its due day unless `created_at` says
    otherwise, so it is never older than its anchor delivery. `escalation_flag` / `escalated_at`
    also mark a CLOSED row that had escalated (the "before contact" and "stuck order delivered"
    metrics read them). `ladder_fields` (`supply`, `step`, `next_action_on`, `suspended_reason`,
    `is_dry_run`) pass through to `make_episode` unchanged. Instants are written in UTC, as every
    builder here writes them (module docstring).
    """
    opened = created_at or datetime.combine(due_on, time(10, 0), tzinfo=TASHKENT)
    episode = make_episode(
        db, user, anchor=anchor, status=status, escalation_flag=escalation_flag, created_at=opened, **ladder_fields
    )
    episode.due_on = due_on
    episode.usual_gap_days = usual_gap_days
    episode.threshold_days = threshold_days
    episode.reminders_sent = reminders_sent
    if escalation_flag is not None:
        episode.escalation_flag = escalation_flag
        episode.escalated_at = _utc(escalated_at) or episode.escalated_at or _utc(opened)
    elif escalated_at is not None:
        episode.escalated_at = _utc(escalated_at)
    if outcome is not None:
        episode.outcome = outcome
    if close_reason is not None:
        episode.close_reason = close_reason
    if closed_at is not None:
        episode.closed_at = _utc(closed_at)
    db.session.commit()
    return episode
