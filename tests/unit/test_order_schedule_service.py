from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, UserRole

TZ = ZoneInfo("Asia/Tashkent")


def _driver(db, *, shift_start, is_active=True, status="active", suffix="1"):
    user = User(
        email=f"driver{suffix}@example.com",
        phone=f"+9989000000{suffix}",
        password_hash=hash_password("TestPassword123!"),
        first_name="D",
        last_name=suffix,
        role=UserRole.DELIVERY_DRIVER,
        status=status,
    )
    db.session.add(user)
    db.session.flush()
    dp = DeliveryPerson(
        user_id=user.id,
        full_name=f"Driver {suffix}",
        phone=user.phone,
        working_hours_start=shift_start,
        working_hours_end="18:00",
        is_active=is_active,
        is_available=True,
    )
    db.session.add(dp)
    db.session.commit()
    return dp


def _confirmed_order(db, *, delivery_date, status=OrderStatus.CONFIRMED):
    order = Order(
        user_id=1,
        status=status,
        total_amount=1000,
        delivery_date=delivery_date,
        order_source="admin",
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_earliest_shift_start_is_the_min_over_active_drivers(app, db):
    with app.app_context():
        _driver(db, shift_start="10:00", suffix="a")
        _driver(db, shift_start="07:30", suffix="b")
        assert OrderScheduleService.earliest_shift_start() == time(7, 30)


def test_deactivated_driver_does_not_move_the_release(app, db):
    with app.app_context():
        _driver(db, shift_start="10:00", suffix="c")
        _driver(db, shift_start="05:00", is_active=False, suffix="d")
        assert OrderScheduleService.earliest_shift_start() == time(10, 0)


def test_empty_roster_falls_back_to_the_configured_default(app, db):
    with app.app_context():
        assert OrderScheduleService.earliest_shift_start() == time(9, 0)


def test_release_at_is_local_morning_converted_to_utc(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="e")
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        # 08:00 Tashkent (UTC+5) == 03:00 UTC
        assert OrderScheduleService.release_at(order) == datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


def test_release_at_is_none_without_a_delivery_date(app, db):
    with app.app_context():
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000, delivery_date=None)
        assert OrderScheduleService.release_at(order) is None


def test_is_awaiting_release_true_before_the_morning(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="f")
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        frozen = datetime(2026, 8, 19, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen):
            assert OrderScheduleService.is_awaiting_release(order) is True


def test_is_awaiting_release_false_once_the_morning_has_arrived(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="g")
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        frozen = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen):
            assert OrderScheduleService.is_awaiting_release(order) is False


def test_undated_order_is_never_awaiting_release(app, db):
    with app.app_context():
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000, delivery_date=None)
        assert OrderScheduleService.is_awaiting_release(order) is False


def test_pending_order_is_never_awaiting_release(app, db):
    """The sweep only releases CONFIRMED/PREPARING orders; an unpaid PENDING
    order is left to cancel_abandoned_orders exactly as today."""
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="h")
        order = Order(user_id=1, status=OrderStatus.PENDING, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        assert OrderScheduleService.is_awaiting_release(order) is False


def test_order_with_existing_delivery_is_never_awaiting_release(app, db):
    """The load-bearing branch: once a real Delivery row exists, the order is
    already visible to drivers and must never be reported as awaiting
    release — regardless of status or how far in the future delivery_date is.

    The clock is frozen strictly BEFORE release_at (03:00Z) so the
    release-time branch alone would say "still awaiting" (True); only the
    `order.delivery is not None` short-circuit can make this False. Without
    freezing before that instant, this test would pass for the wrong reason
    once real time crosses the release instant (2026-08-20 03:00 UTC) and
    stay silently worthless forever after.
    """
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="i")
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        db.session.add(order)
        db.session.flush()
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
            scheduled_time_slot="08:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()
        # Prove the relationship actually resolved before trusting the predicate.
        assert order.delivery is not None
        # One hour before release_at (03:00Z) — the release-time branch alone
        # would report True here; only the existing-Delivery short-circuit
        # can make this False.
        frozen = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen):
            assert OrderScheduleService.is_awaiting_release(order) is False


def test_is_awaiting_release_false_at_the_exact_release_instant(app, db):
    """release_at > get_utc_now() must be a strict inequality: at the instant
    the shift opens, the order is released, not still waiting."""
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="j")
        order = Order(user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000,
                      delivery_date=date(2026, 8, 20))
        # 08:00 Tashkent (UTC+5) == 03:00 UTC — the exact release() instant.
        frozen = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen):
            assert OrderScheduleService.is_awaiting_release(order) is False


def test_earliest_shift_start_skips_malformed_working_hours_start(app, db):
    """A garbage working_hours_start (free-text String(5)) must be ignored,
    not crash the roster scan or empty it out."""
    with app.app_context():
        _driver(db, shift_start="9am", suffix="k")
        _driver(db, shift_start="10:00", suffix="l")
        assert OrderScheduleService.earliest_shift_start() == time(10, 0)


def test_earliest_shift_start_falls_back_when_only_driver_is_malformed(app, db):
    """When every rostered shift is unparseable, fall back to the configured
    default rather than raising or stranding orders with an empty roster."""
    with app.app_context():
        _driver(db, shift_start="9am", suffix="m")
        assert OrderScheduleService.earliest_shift_start() == time(9, 0)


def test_gate_creates_a_delivery_for_an_undated_order(app, db):
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None)
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            create.return_value = "DELIVERY"
            assert OrderScheduleService.ensure_delivery_if_due(order) == "DELIVERY"
            create.assert_called_once_with(order.id)


def test_gate_refuses_to_create_a_delivery_before_release(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="i")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        frozen = datetime(2026, 8, 19, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen), \
             patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            assert OrderScheduleService.ensure_delivery_if_due(order) is None
            create.assert_not_called()


def test_gate_creates_the_delivery_once_release_has_passed(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="j")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        frozen = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen), \
             patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            create.return_value = "DELIVERY"
            assert OrderScheduleService.ensure_delivery_if_due(order) == "DELIVERY"
            create.assert_called_once_with(order.id)


# ---------------------------------------------------------------------------
# The status half of the gate. `is_awaiting_release` answers "is this order
# being HELD BACK?" and correctly says False for PENDING/CANCELLED -- those
# orders are not held back, they are simply not candidates. The gate used to
# read that False as "due now" and fall straight through to `create_delivery`,
# so `reschedule` and the `assign_delivery` bulk action -- the two callers that
# pass unfiltered statuses -- could put an unpaid or cancelled order in front
# of every on-shift driver. The gate is the one place that decides, so the
# status precondition lives here, not in each caller.
# ---------------------------------------------------------------------------


def test_gate_refuses_to_create_a_delivery_for_a_pending_order(app, db):
    """A future-dated PENDING (unpaid card) order must get NO delivery, even
    though `is_awaiting_release` reports False for it."""
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="pend")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 25), status=OrderStatus.PENDING)
        assert OrderScheduleService.is_awaiting_release(order) is False  # the trap
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            assert OrderScheduleService.ensure_delivery_if_due(order) is None
            create.assert_not_called()


def test_gate_refuses_to_create_a_delivery_for_an_undated_pending_order(app, db):
    """Not a scheduling question at all: an unpaid order with no date must not
    reach a driver either. Pins that the status check is unconditional rather
    than an extra clause hidden inside the future-date branch."""
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None, status=OrderStatus.PENDING)
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            assert OrderScheduleService.ensure_delivery_if_due(order) is None
            create.assert_not_called()


def test_gate_refuses_to_create_a_delivery_for_a_cancelled_order(app, db):
    """Creating a delivery for a cancelled order was never right."""
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None, status=OrderStatus.CANCELLED)
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            assert OrderScheduleService.ensure_delivery_if_due(order) is None
            create.assert_not_called()


def test_gate_still_creates_the_delivery_for_a_preparing_order(app, db):
    """PREPARING is the other releasable status -- the status precondition must
    not narrow the gate to CONFIRMED alone."""
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None, status=OrderStatus.PREPARING)
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            create.return_value = "DELIVERY"
            assert OrderScheduleService.ensure_delivery_if_due(order) == "DELIVERY"
            create.assert_called_once_with(order.id)


def test_gate_returns_an_existing_delivery_even_for_a_cancelled_order(app, db):
    """The status precondition must sit BEHIND the existing-delivery
    short-circuit: an order that was released and then cancelled still has a
    real `Delivery` row, and every caller expects the gate to hand it back
    rather than pretend it does not exist."""
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None, status=OrderStatus.CANCELLED)
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.commit()
        with patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:
            assert OrderScheduleService.ensure_delivery_if_due(order) is delivery
            create.assert_not_called()


def test_gate_declines_for_a_stale_in_memory_order_after_a_concurrent_reschedule(app, db):
    """Closes the release-sweep / reschedule race (Task 8 review finding #1):
    `release_due_scheduled_orders` selects candidates with a plain UNLOCKED
    query (order_tasks.py) and hands each `Order` straight to this gate. If a
    concurrent writer commits a new future `delivery_date` in the gap between
    the candidate-select and this call, the gate must not trust the stale
    Python object it was handed -- it must genuinely RE-READ committed state
    before ever calling `create_delivery`.

    The concurrent write is issued through `db.session.connection()` so it
    bypasses the ORM identity map entirely: `stale_order` stays mapped, stays
    in the identity map, and keeps its pre-write `delivery_date` and its
    already-lazy-loaded (None) `delivery`. That is the shape the sweep really
    has in hand, and it is the ONLY shape that distinguishes "the gate
    re-queries by id" from "the gate sees committed state".

    Deliberately NOT expunged. The previous version of this test called
    `db.session.expunge(stale_order)`, which removes the instance from the
    identity map -- with the map empty, even a re-read that cannot refresh an
    existing instance is forced to build a fresh one from the row, so the test
    passed against a gate that never refreshed anything (`Order.query
    .with_for_update().get(id)` sets `FOR UPDATE` and skips the identity-map
    shortcut, but NOT `_populate_existing`, so an instance already in the
    session keeps its stale attributes -- verified against SQLAlchemy 2.0.43).
    """
    from sqlalchemy import bindparam, text
    from sqlalchemy.types import Date as SADate

    with app.app_context():
        _driver(db, shift_start="08:00", suffix="race")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        order_id = order.id

        # 08:01 local on the 20th: release has just passed for the order's
        # CURRENT (soon-to-be-stale) date, so the gate's fast, unlocked
        # checks alone would say "due". It is well before release for the
        # date the concurrent write below moves it to.
        frozen = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=frozen), \
             patch("business_app.services.delivery_service.DeliveryService.create_delivery") as create:

            # The sweep's own read, before the concurrent write.
            stale_order = Order.query.get(order_id)
            _ = stale_order.delivery  # lazy-load the one-to-one, exactly as
            #                           `is_awaiting_release` does moments later
            assert stale_order.delivery_date == date(2026, 8, 20)

            # A concurrent writer pushes the order 5 days out. Raw SQL on the
            # session's own connection: the row changes, the identity map does
            # not hear about it.
            db.session.connection().execute(
                text("UPDATE orders SET delivery_date = :new_date WHERE id = :order_id").bindparams(
                    bindparam("new_date", type_=SADate)
                ),
                {"new_date": date(2026, 8, 25), "order_id": order_id},
            )

            # Preconditions that make this test meaningful: the instance is
            # still THE identity-map instance, and it is still stale.
            assert stale_order is db.session.get(Order, order_id)
            assert stale_order.delivery_date == date(2026, 8, 20)

            # The sweep now calls the gate on the stale instance it already
            # had in hand.
            result = OrderScheduleService.ensure_delivery_if_due(stale_order)

        assert result is None
        create.assert_not_called()
        assert Delivery.query.filter_by(order_id=order_id).first() is None


# ---------------------------------------------------------------------------
# Task 9 fix-review: `earliest_shift_start` must be request-scoped-cached.
#
# `serialize_order_admin` calls `is_awaiting_release`/`release_at` -- both of
# which resolve through `earliest_shift_start` -- once per order, inside the
# per-order loop of the paginated admin order-list endpoint
# (business_app/api/admin.py::get_orders, right next to PaginationOptimizer/
# AggregationOptimizer, which exist specifically to prevent this class of
# bug). An uncached `earliest_shift_start` issues a fresh `DeliveryPerson`
# roster query on every call: N orders on a page => N (or more, since a
# release-gated order calls through it twice: once inside
# `is_awaiting_release`, once via the explicit `release_at` call) roster
# queries for data that cannot meaningfully change within one response.
# ---------------------------------------------------------------------------


def test_earliest_shift_start_resolves_the_roster_once_across_a_multi_order_serialization(
    app, db, count_queries
):
    """Serializing several release-gated orders back to back (exactly what
    `business_app/api/admin.py::get_orders` does in its per-order loop) must
    issue exactly ONE `delivery_persons` roster query, not one per order."""
    from business_app.serializers.admin_serializers import serialize_order_admin

    with app.app_context():
        _driver(db, shift_start="08:00", suffix="cache")
        # Five days out: unambiguously in the future regardless of which side
        # of the UTC-vs-Tashkent day boundary "now" happens to fall on, so
        # `is_awaiting_release` is True for every order (both the
        # `is_awaiting_release` internal call AND serialize_order_admin's own
        # explicit `release_at` call fire `earliest_shift_start` per order --
        # the strictest exercise of the cache).
        far_future = date.today() + timedelta(days=5)
        orders = [_confirmed_order(db, delivery_date=far_future) for _ in range(8)]

        with count_queries() as counter:
            for order in orders:
                data = serialize_order_admin(order)
                assert data["awaiting_release"] is True

        roster_queries = [s for s in counter.statements if "delivery_persons" in s.lower()]
        assert len(roster_queries) == 1, (
            f"expected exactly 1 roster query across {len(orders)} orders, "
            f"got {len(roster_queries)}:\n" + "\n".join(roster_queries)
        )


def test_earliest_shift_start_cache_does_not_leak_across_app_contexts(app, db):
    """The cache must be scoped to ONE application context, not process-wide
    -- otherwise a roster change would never be observed again after the
    first read in a long-lived process (e.g. a Celery worker)."""
    with app.app_context():
        _driver(db, shift_start="10:00", suffix="ctx1")
        assert OrderScheduleService.earliest_shift_start() == time(10, 0)

    with app.app_context():
        _driver(db, shift_start="06:00", suffix="ctx2")
        assert OrderScheduleService.earliest_shift_start() == time(6, 0)


def test_earliest_shift_start_works_uncached_outside_an_app_context(monkeypatch):
    """Any caller without a live app/request context (or any future one) must
    still get a correct, uncached answer rather than crash touching
    `flask.g`. Stands in for `DeliveryPerson`/`current_app` entirely with
    plain fakes -- no `app.app_context()` is pushed anywhere in this test --
    so this exercises the real `has_app_context() is False` branch rather
    than mocking around Flask-SQLAlchemy's app-context-bound `query`
    descriptor."""
    import business_app.services.order_schedule_service as oss_module

    class _FakeQuery:
        def join(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def with_entities(self, *_a, **_k):
            return self

        def all(self):
            return []

    class _FakeDeliveryPerson:
        query = _FakeQuery()
        # Referenced when building the join/filter/entities clauses --
        # `_FakeQuery` ignores its args entirely, but the attribute access
        # (`DeliveryPerson.user_id`, `.is_active.is_(True)`,
        # `DeliveryPerson.working_hours_start`) still has to resolve as a
        # plain Python expression before it ever reaches `_FakeQuery`.
        user_id = MagicMock()
        is_active = MagicMock()
        working_hours_start = MagicMock()

    class _FakeCurrentApp:
        config = {"DEFAULT_DISPATCH_OPEN_TIME": "09:00"}

    monkeypatch.setattr(oss_module, "DeliveryPerson", _FakeDeliveryPerson)
    monkeypatch.setattr(oss_module, "current_app", _FakeCurrentApp())

    assert OrderScheduleService.earliest_shift_start() == time(9, 0)


# ---------------------------------------------------------------------------
# Reschedule eligibility (docs/superpowers/specs/2026-09-23-admin-order-
# reschedule-design.md, R1/R2). The statuses are written out here AS THE SPEC
# STATES THEM and never imported from the service: a test that read the
# service's own set would agree with any set it was pointed at.
# ---------------------------------------------------------------------------

_R1_LIVE_ORDER_STATUSES = {
    OrderStatus.PENDING,
    OrderStatus.CONFIRMED,
    OrderStatus.PREPARING,
    OrderStatus.OUT_FOR_DELIVERY,
}
_R2_MOVABLE_DELIVERY_STATUSES = {
    DeliveryStatus.SCHEDULED,
    DeliveryStatus.PENDING,
    DeliveryStatus.ASSIGNED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT,
    DeliveryStatus.ARRIVED,
    DeliveryStatus.FAILED,
    DeliveryStatus.RESCHEDULED,
}


@pytest.mark.parametrize("order_status", list(OrderStatus), ids=lambda s: s.value)
def test_only_a_live_order_can_be_rescheduled(app, order_status):
    """R1. No delivery row, so the order's own status is the whole answer.
    Built in memory: the rule reads two attributes and needs no table."""
    order = Order(order_number=f"R1-{order_status.value}", user_id=1, status=order_status, total_amount=1000)

    expected = None if order_status in _R1_LIVE_ORDER_STATUSES else "ORDER_NOT_RESCHEDULABLE"
    assert OrderScheduleService.reschedule_block_code(order) == expected


@pytest.mark.parametrize("delivery_status", list(DeliveryStatus), ids=lambda s: s.value)
def test_every_delivery_status_has_a_reschedule_answer(app, delivery_status):
    """R2, over the whole enum rather than a sample, on a live (CONFIRMED)
    order so only the delivery can refuse."""
    order = Order(
        order_number=f"R2-{delivery_status.value}", user_id=1, status=OrderStatus.CONFIRMED, total_amount=1000
    )
    order.delivery = Delivery(
        status=delivery_status,
        scheduled_date=datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc),
        scheduled_time_slot="anytime",
    )

    expected = None if delivery_status in _R2_MOVABLE_DELIVERY_STATUSES else "DELIVERY_NOT_RESCHEDULABLE"
    assert OrderScheduleService.reschedule_block_code(order) == expected


def test_a_dead_order_is_refused_as_an_order_even_when_its_delivery_is_dead_too(app):
    """The order is checked first: "this order is delivered" is the reason an
    admin can act on, not "this delivery is delivered"."""
    order = Order(order_number="R1-first", user_id=1, status=OrderStatus.DELIVERED, total_amount=1000)
    order.delivery = Delivery(
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc),
        scheduled_time_slot="anytime",
    )

    assert OrderScheduleService.reschedule_block_code(order) == "ORDER_NOT_RESCHEDULABLE"


@pytest.mark.parametrize(
    "delivery_status,expected",
    [
        (None, {"can_reschedule": True, "reschedule_block_code": None}),
        (DeliveryStatus.ASSIGNED, {"can_reschedule": True, "reschedule_block_code": None}),
        (DeliveryStatus.DELIVERED, {"can_reschedule": False, "reschedule_block_code": "DELIVERY_NOT_RESCHEDULABLE"}),
    ],
    ids=["no-row", "assigned", "delivered"],
)
def test_the_list_row_answer_costs_no_query(app, db, count_queries, delivery_driver, delivery_status, expected):
    """`serialize_order_admin` asks this for every row of a page of up to 100.
    Loaded the way `GET /admin/orders` loads it (`get_orders_with_details`,
    the list's own eager-load), the answer is read off the row: not one
    statement, and no detail-only key."""
    from business_app.utils.query_optimization import get_orders_with_details

    driver_id = delivery_driver.id
    with app.app_context():
        order = _confirmed_order(db, delivery_date=None)
        if delivery_status is not None:
            db.session.add(
                Delivery(
                    order_id=order.id,
                    status=delivery_status,
                    delivery_person_id=driver_id,
                    scheduled_date=datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc),
                    scheduled_time_slot="anytime",
                )
            )
            db.session.commit()
        loaded = get_orders_with_details(Order.query.filter(Order.id == order.id)).one()

        with count_queries() as counter:
            metadata = OrderScheduleService.get_reschedule_metadata(loaded, detail=False)

    assert metadata == expected
    assert counter.count == 0, counter.statements


def test_two_active_amount_contracts_leave_the_horizon_uncapped_and_say_so(app, db, caplog):
    """The contract lookup refuses to choose between two active AMOUNT
    contracts. The delivery charge fails on the same ambiguity, so no date
    cap could protect it: the bounds fall back to the booking horizon and a
    warning names the order and the store, instead of the admin order page
    failing. Real rows, so it is the real lookup that refuses."""
    import logging

    from business_app.models.corporate import CorporateContract, CorporateContractStatus
    from business_app.services import order_schedule_service as oss_module
    from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
    from shared.enums import CorporateContractTrackingMode, EntitySubtype, UserType

    now_local = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
    today = now_local.date()
    with app.app_context():
        store = User(
            email="two-contracts@example.com",
            phone="+998909990012",
            password_hash=hash_password("StorePassword123!"),
            first_name="Ikki",
            last_name="Market",
            user_type=UserType.ENTITY,
            entity_subtype=EntitySubtype.GROCERY_STORE,
            company_name="Ikki Market",
            role=UserRole.CUSTOMER,
        )
        db.session.add(store)
        db.session.flush()
        for number, ends_in_days in (("GS-TWO-001", 3), ("GS-TWO-002", 5)):
            db.session.add(
                CorporateContract(
                    user_id=store.id,
                    contract_number=number,
                    name=f"Ikki Market {number}",
                    status=CorporateContractStatus.ACTIVE,
                    start_date=datetime.now(timezone.utc) - timedelta(days=30),
                    end_date=datetime.now(timezone.utc) + timedelta(days=ends_in_days),
                    currency="UZS",
                    is_active=True,
                    tracking_mode=CorporateContractTrackingMode.AMOUNT,
                )
            )
        order = Order(user_id=store.id, status=OrderStatus.CONFIRMED, total_amount=1000, order_source="admin")
        db.session.add(order)
        db.session.commit()

        # The module logger does not propagate to caplog's root handler.
        oss_module.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger=oss_module.logger.name), patch(
                "business_app.utils.delivery_window.local_now", return_value=now_local
            ):
                bounds = OrderScheduleService.reschedule_date_bounds(order)
        finally:
            oss_module.logger.removeHandler(caplog.handler)

        order_id, store_id = order.id, store.id

    assert bounds == (today, today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(f"order {order_id}" in m and f"customer {store_id}" in m for m in warnings), warnings


# ---------------------------------------------------------------------------
# A held (`rescheduled`) row (docs/superpowers/specs/2026-09-23-admin-order-
# reschedule-design.md §3.5): released to drivers once, then moved to a later
# day. The gate is its one way back into the pool, decided on locked,
# re-read rows.
# ---------------------------------------------------------------------------


def _held_row(db, order):
    """The shape a reschedule to a later day leaves: driverless, no ETA, and
    the distance `create_delivery` measured when the row was first released."""
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.RESCHEDULED,
        delivery_person_id=None,
        distance_km=3.0,
        estimated_delivery_time=None,
        scheduled_date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
        scheduled_time_slot="anytime",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def test_a_held_row_is_awaiting_release_until_its_morning(app, db):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held1")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        _held_row(db, order)
        # Prove the relationship resolved before trusting the predicate.
        assert order.delivery is not None

        evening_before = datetime(2026, 8, 19, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=evening_before):
            assert OrderScheduleService.is_awaiting_release(order) is True
        opened = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=opened):
            assert OrderScheduleService.is_awaiting_release(order) is False


@pytest.mark.parametrize(
    "order_status, row_status, expected",
    [
        # Held until 08:00 Tashkent, which is 03:00 UTC.
        (OrderStatus.CONFIRMED, DeliveryStatus.RESCHEDULED, datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)),
        # R23 corollary: held until it is confirmed, which has no instant to quote.
        (OrderStatus.PENDING, DeliveryStatus.RESCHEDULED, None),
        # Already in the pool: drivers see it now.
        (OrderStatus.CONFIRMED, DeliveryStatus.SCHEDULED, None),
    ],
    ids=["held-confirmed", "held-pending", "released"],
)
def test_published_release_at_is_the_instant_only_while_the_order_is_held(
    app, db, order_status, row_status, expected
):
    """The one answer behind every published `release_at` (the admin order payloads and
    both re-dispatch responses). The clock is the evening before, so the release instant
    is still ahead in every case, and only the order and its row decide."""
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="pub")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20), status=order_status)
        db.session.add(
            Delivery(
                order_id=order.id,
                status=row_status,
                scheduled_date=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
                scheduled_time_slot="anytime",
            )
        )
        db.session.commit()
        # Prove the relationship resolved before trusting the answer.
        assert order.delivery is not None

        evening_before = datetime(2026, 8, 19, 14, 0, tzinfo=TZ).astimezone(timezone.utc)
        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=evening_before):
            assert OrderScheduleService.published_release_at(order) == expected


def _release_spies(monkeypatch):
    """The two Celery publishes a release makes through `offer_to_drivers`: the
    auto-assign timer and the diversion evaluator (which itself broadcasts).

    Only the broker is replaced. `_task_spy` binds each publish against the
    task's own signature and records its exact payload, so `offer_to_drivers`
    and `create_delivery` run for real and a gate that offered too early, too
    often or with the wrong id shows up here.
    """
    from business_app.tasks import delivery_tasks
    from tests.unit.test_delivery_service_business_rules import _task_spy

    auto_assign = _task_spy(monkeypatch, delivery_tasks.auto_assign_delivery_task, "apply_async")
    evaluator = _task_spy(monkeypatch, delivery_tasks.evaluate_pool_insertion_suggestions_task, "delay")
    return auto_assign, evaluator


def test_gate_leaves_a_held_row_alone_before_its_morning(app, db, monkeypatch):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held2")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        held = _held_row(db, order)
        auto_assign, evaluator = _release_spies(monkeypatch)
        evening_before = datetime(2026, 8, 19, 14, 0, tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=evening_before):
            assert OrderScheduleService.ensure_delivery_if_due(order) is None

        assert auto_assign == []
        assert evaluator == []
        assert Delivery.query.filter_by(order_id=order.id).count() == 1
        assert db.session.get(Delivery, held.id).status == DeliveryStatus.RESCHEDULED
        assert DeliveryStatusHistory.query.filter_by(delivery_id=held.id).count() == 0


def test_gate_releases_a_held_row_once_its_morning_arrives(app, db, monkeypatch):
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held3")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        held = _held_row(db, order)
        auto_assign, evaluator = _release_spies(monkeypatch)
        opened = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=opened):
            released = OrderScheduleService.ensure_delivery_if_due(order)

        assert released is not None and released.id == held.id
        assert released.status == DeliveryStatus.SCHEDULED
        assert released.delivery_person_id is None
        assert released.estimated_delivery_time is not None
        # The row already exists: release flips it, it never makes a second one.
        assert Delivery.query.filter_by(order_id=order.id).count() == 1
        # Offered once, exactly as a fresh pool row is.
        assert auto_assign == [((held.id,), {}, {"countdown": 300})]
        assert evaluator == [((held.id,), {})]
        history = DeliveryStatusHistory.query.filter_by(delivery_id=held.id).all()
        assert [(h.old_status, h.new_status, h.changed_by, h.automatic) for h in history] == [
            (DeliveryStatus.RESCHEDULED, DeliveryStatus.SCHEDULED, None, True)
        ]


def test_gate_keeps_a_held_row_whose_order_is_not_releasable(app, db, monkeypatch):
    """PENDING is outside RELEASABLE_ORDER_STATUSES. The held row waits for its
    order to be confirmed, exactly as an order with no row does. The shape is
    real: a reschedule never confirms an order (R23), so an unpaid order that
    owned a row keeps PENDING when that row is held."""
    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held4")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20), status=OrderStatus.PENDING)
        held = _held_row(db, order)
        auto_assign, evaluator = _release_spies(monkeypatch)
        opened = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=opened):
            assert OrderScheduleService.ensure_delivery_if_due(order) is None

        assert auto_assign == []
        assert evaluator == []
        assert Delivery.query.filter_by(order_id=order.id).count() == 1
        assert db.session.get(Delivery, held.id).status == DeliveryStatus.RESCHEDULED


def test_gate_hands_back_a_held_row_that_was_cancelled_while_it_looked(app, db, monkeypatch):
    """The sweep selects its candidates with an UNLOCKED read. If the
    order-cancel cascade ends the held row in that gap, the locked re-read
    must see CANCELLED. Flipping the stale RESCHEDULED instance would put a
    cancelled order's delivery back in the pool.

    The concurrent write goes through `db.session.connection()` so the
    identity map never hears of it (the same technique as
    `test_gate_declines_for_a_stale_in_memory_order_after_a_concurrent_reschedule`):
    only a `populate_existing` re-read can see it."""
    from sqlalchemy import text

    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held5")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        held = _held_row(db, order)
        auto_assign, evaluator = _release_spies(monkeypatch)
        opened = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=opened):
            stale_order = Order.query.get(order.id)
            assert stale_order.delivery.status == DeliveryStatus.RESCHEDULED
            db.session.connection().execute(
                text("UPDATE deliveries SET status = 'cancelled' WHERE id = :delivery_id"),
                {"delivery_id": held.id},
            )
            # Still stale in memory: the precondition that makes this test mean something.
            assert stale_order.delivery.status == DeliveryStatus.RESCHEDULED

            result = OrderScheduleService.ensure_delivery_if_due(stale_order)

        assert result.id == held.id
        assert result.status == DeliveryStatus.CANCELLED
        assert auto_assign == []
        assert evaluator == []
        assert Delivery.query.filter_by(order_id=order.id).count() == 1
        assert DeliveryStatusHistory.query.filter_by(delivery_id=held.id).count() == 0


def test_gate_rereads_the_order_date_before_releasing_a_held_row(app, db, monkeypatch):
    """The same race from the order's side: a reschedule to a later day
    commits between the sweep's read and the gate. The locked Order re-read
    must see the new date and keep the row held."""
    from sqlalchemy import bindparam, text
    from sqlalchemy.types import Date as SADate

    with app.app_context():
        _driver(db, shift_start="08:00", suffix="held6")
        order = _confirmed_order(db, delivery_date=date(2026, 8, 20))
        held = _held_row(db, order)
        auto_assign, evaluator = _release_spies(monkeypatch)
        opened = datetime(2026, 8, 20, 8, 1, tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=opened):
            stale_order = Order.query.get(order.id)
            _ = stale_order.delivery
            db.session.connection().execute(
                text("UPDATE orders SET delivery_date = :new_date WHERE id = :order_id").bindparams(
                    bindparam("new_date", type_=SADate)
                ),
                {"new_date": date(2026, 8, 25), "order_id": order.id},
            )
            assert stale_order.delivery_date == date(2026, 8, 20)

            result = OrderScheduleService.ensure_delivery_if_due(stale_order)

        assert result is None
        assert auto_assign == []
        assert evaluator == []
        assert Delivery.query.filter_by(order_id=order.id).count() == 1
        assert db.session.get(Delivery, held.id).status == DeliveryStatus.RESCHEDULED


def test_a_due_reschedule_lands_in_the_pool_only_for_a_status_the_release_gate_offers(app, db, admin_user, monkeypatch):
    """The reschedule's landing status asks the release gate's own constant, not a
    restatement of it. Narrow `RELEASABLE_ORDER_STATUSES` to CONFIRMED and a PREPARING
    order's row, moved to a date whose morning has passed, must be held, not put in
    today's pool and offered: a status the gate would not offer never reaches drivers
    through a reschedule either."""
    from business_app.services import order_schedule_service as oss_module

    with app.app_context():
        _driver(db, shift_start="08:00", suffix="gate")
        today = datetime.now(TZ).date()
        order = _confirmed_order(db, delivery_date=today, status=OrderStatus.PREPARING)
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            delivery_person_id=None,
            distance_km=3.0,
            scheduled_date=datetime.combine(today, time.min, tzinfo=timezone.utc),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.commit()
        auto_assign, evaluator = _release_spies(monkeypatch)
        monkeypatch.setattr(oss_module, "RELEASABLE_ORDER_STATUSES", (OrderStatus.CONFIRMED,))
        noon = datetime.combine(today, time(12, 0), tzinfo=TZ).astimezone(timezone.utc)

        with patch("business_app.services.order_schedule_service.get_utc_now", return_value=noon):
            # Past the 08:00 roster start: only the order's status can hold the row.
            assert OrderScheduleService.release_at(order) <= noon
            OrderScheduleService.reschedule(order.id, delivery_date=today, actor_user_id=admin_user.id)

        assert db.session.get(Delivery, delivery.id).status == DeliveryStatus.RESCHEDULED
        assert db.session.get(Order, order.id).status == OrderStatus.PREPARING
        assert auto_assign == []
        assert evaluator == []
