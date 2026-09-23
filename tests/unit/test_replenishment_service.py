"""ReplenishmentService: the D7 due-date rule and the D15 suggested-order rule.

The numbers are the spec's worked example (`docs/superpowers/specs/2026-09-07-sales-agent-role-design.md`
§ D7 / D15 and *Testing → Visits and orders*): two stock checks 7 days apart,
10 -> 3 on hand with 6 delivered between them, gives rate_per_day = 13/7; a
class-A outlet (cadence 7) with lead 1 and safety factor 1.5 then suggests
ceil(13/7 x 8 x 1.5 - 3) = 20.

Time is frozen with a `_FrozenDatetime` subclass patched into the service module
(the `tests/unit/test_dispatch_service.py:462-470` pattern) so the due-date cases
assert a calendar instant instead of "roughly a week after whenever the suite
ran", and so `local_day_end_utc` can be probed at 03:00 Tashkent — inside the
00:00-05:00 window where the local day and the UTC day disagree.

Nothing here re-implements a rule: every expectation is either a literal from
the spec or the value another published `ReplenishmentService` field returns.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.product import Product
from business_app.models.sales import Outlet
from business_app.models.sales_visits import Visit, VisitStockCheck
from business_app.models.user import User, UserAddress
from business_app.services.sales import replenishment_service as replenishment_module
from business_app.services.sales.replenishment_service import INCOMING_ORDER_STATUSES, ReplenishmentService
from business_app.services.sales.visit_service import VisitService
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserType
from shared.status_transitions import ORDER_STATUS_TRANSITIONS
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

# 2026-09-08 06:00 UTC = 11:00 Tashkent (UTC+5).
FROZEN_UTC = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
FIRST_VISIT_AT = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
DELIVERY_AT = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
SECOND_VISIT_AT = FROZEN_UTC

# The D7-floor scenario (ruling 33): the same 7-day 10 -> 3 example, but three weeks
# stale, so the stock-out it projects already lies in the past.
STALE_FIRST_VISIT_AT = FROZEN_UTC - timedelta(days=28)
STALE_DELIVERY_AT = FROZEN_UTC - timedelta(days=25)
STALE_SECOND_VISIT_AT = FROZEN_UTC - timedelta(days=21)


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze `datetime.now()` inside the service module only (dispatch precedent)."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_UTC.astimezone(tz) if tz else FROZEN_UTC.replace(tzinfo=None)

    monkeypatch.setattr(replenishment_module, "datetime", _FrozenDatetime)
    return FROZEN_UTC


def _owner(db, phone="+998901234591"):
    """The store's customer account — an outlet only has consumption history once linked."""
    user = User(
        phone=phone,
        password_hash=hash_password("Pw123456!"),
        first_name="Bahor",
        last_name="Owner",
        user_type=UserType.ENTITY,
        role=UserRole.CUSTOMER,
        company_name="Bahor market",
        entity_subtype=EntitySubtype.GROCERY_STORE,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _outlet(db, owner=None, *, stage="active", outlet_class="A", **columns):
    outlet = Outlet(
        name="Bahor market",
        outlet_type="grocery_store",
        stage=stage,
        outlet_class=outlet_class,
        user_id=owner.id if owner else None,
        **columns,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _address(db, owner, *, full_address, is_default=False):
    """One branch's own delivery address -- the column every branch-scoped query joins on.

    Deliberately coordinate-less: `register_delivery_zone_listeners` skips a row with no
    pin, so these fixtures never have to carry an in-polygon coordinate to say where a
    branch is.
    """
    address = UserAddress(user_id=owner.id, full_address=full_address, is_default=is_default)
    db.session.add(address)
    db.session.commit()
    return address


def _chain(db, product):
    """One grocery ACCOUNT with two branches, each on its own address (D25).

    Returns (owner, busy_branch, quiet_branch, busy_address, quiet_address). Two outlets on
    one `user_id` is what `OutletService.is_branch` answers True to.
    """
    owner = _owner(db, phone="+998901234593")
    busy_address = _address(db, owner, full_address="Chilonzor 5", is_default=True)
    quiet_address = _address(db, owner, full_address="Yunusobod 12")
    busy = _outlet(db, owner, address_id=busy_address.id)
    quiet = _outlet(db, owner, address_id=quiet_address.id)
    _flag_for_stock_check(db, product)
    return owner, busy, quiet, busy_address, quiet_address


def _visit(db, outlet, agent, *, started_at):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=agent.id,
        status="completed",
        planned=True,
        current_step="close",
        started_at=started_at,
        checkin_skipped=False,
    )
    db.session.add(visit)
    db.session.commit()
    return visit


def _stock_check(db, visit, product, *, on_hand):
    row = VisitStockCheck(
        visit_id=visit.id,
        product_id=product.id,
        on_hand_qty=on_hand,
        is_sold_out=False,
        is_low=False,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _delivered_order(db, owner, product, *, quantity, delivered_at, number, delivery_address_id=None):
    """A DELIVERED order whose delivery instant lives on the status-history row.

    `orders` has no `delivered_at` column — the DELIVERED history row is the only
    record of when it landed, which is exactly what the rate window reads.
    `delivery_address_id` is the branch key: nullable on `orders`, so leaving it None
    is what a legacy order really looks like.
    """
    order = Order(
        user_id=owner.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=delivery_address_id,
        created_at=delivered_at,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * quantity,
        )
    )
    db.session.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=OrderStatus.OUT_FOR_DELIVERY,
            new_status=OrderStatus.DELIVERED,
            changed_at=delivered_at,
            created_at=delivered_at,
        )
    )
    db.session.commit()
    return order


def _pending_order(db, owner, product, *, quantity, number, status=OrderStatus.PENDING, created_at=None,
                   delivery_address_id=None):
    order = Order(
        user_id=owner.id,
        order_number=number,
        status=status,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=delivery_address_id,
    )
    db.session.add(order)
    db.session.flush()
    if created_at is not None:
        order.created_at = created_at
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00"),
        )
    )
    db.session.commit()
    return order


def _flag_for_stock_check(db, product, *, per_unit="1.00", tracks=True, min_order_quantity=None):
    product.in_sales_stock_check = True
    product.tracks_returnable_bottles = tracks
    product.returnable_bottles_per_unit = Decimal(per_unit)
    if min_order_quantity is not None:
        product.min_order_quantity = min_order_quantity
    db.session.commit()
    return product


def _extra_product(db, category, name, size, *, flagged=True, tracks=True, per_unit="1.00", is_active=True):
    product = Product(
        name=name,
        category_id=category.id,
        size=size,
        base_price=Decimal("12000.00"),
        stock_quantity=50,
        is_active=is_active,
        in_sales_stock_check=flagged,
        tracks_returnable_bottles=tracks,
        returnable_bottles_per_unit=Decimal(per_unit),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _seven_day_scenario(db, agent, owner, outlet, product):
    """The spec's example: on hand 10 -> 3 across 7 days with 6 delivered between."""
    first = _visit(db, outlet, agent, started_at=FIRST_VISIT_AT)
    _stock_check(db, first, product, on_hand=10)
    _delivered_order(db, owner, product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")
    second = _visit(db, outlet, agent, started_at=SECOND_VISIT_AT)
    _stock_check(db, second, product, on_hand=3)
    return first, second


class TestCatalogueAndHistory:
    def test_stock_check_products_lists_flagged_active_products_by_id(self, db, sample_category, sample_product):
        _flag_for_stock_check(db, sample_product)
        ten_litre = _extra_product(db, sample_category, "Pure Water 10L", "10L")
        _extra_product(db, sample_category, "Snack bar", "5L", flagged=False)
        _extra_product(db, sample_category, "Retired 5L", "5L", is_active=False)

        assert [p.id for p in ReplenishmentService.stock_check_products()] == [sample_product.id, ten_litre.id]

    def test_primary_returnable_product_prefers_the_biggest_returnable_sku(self, db, sample_category, sample_product):
        """D22 + the returnable-bottle SSOT: `tracks` alone never makes a product returnable."""
        _flag_for_stock_check(db, sample_product, per_unit="1.00")
        winner = _extra_product(db, sample_category, "Pure Water 10L", "10L", per_unit="2.00")
        _extra_product(db, sample_category, "Cooler rental", "5L", tracks=False, per_unit="9.00")
        _extra_product(db, sample_category, "Pure Water 10L twin", "10L", per_unit="2.00")

        assert ReplenishmentService.primary_returnable_product().id == winner.id

    def test_cadence_days_uses_override_then_class_then_c(self, db):
        outlet = _outlet(db, None, stage="prospect", outlet_class="A")
        assert ReplenishmentService.cadence_days(outlet) == 7

        outlet.outlet_class = "B"
        assert ReplenishmentService.cadence_days(outlet) == 14

        outlet.outlet_class = "C"
        assert ReplenishmentService.cadence_days(outlet) == 30

        outlet.outlet_class = None
        assert ReplenishmentService.cadence_days(outlet) == 30

        outlet.cadence_days_override = 3
        assert ReplenishmentService.cadence_days(outlet) == 3

    def test_delivered_qty_counts_only_deliveries_inside_the_window(self, db, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        _flag_for_stock_check(db, sample_product)
        since = FIRST_VISIT_AT
        until = SECOND_VISIT_AT
        _delivered_order(db, owner, sample_product, quantity=100, delivered_at=since, number="SA_000001_26")
        _delivered_order(db, owner, sample_product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000002_26")
        _delivered_order(db, owner, sample_product, quantity=4, delivered_at=until, number="SA_000003_26")
        _delivered_order(
            db,
            owner,
            sample_product,
            quantity=200,
            delivered_at=until + timedelta(minutes=1),
            number="SA_000004_26",
        )
        _pending_order(db, owner, sample_product, quantity=500, number="SA_000005_26")

        # (since, until]: the delivery exactly AT `since` belongs to the previous window.
        assert ReplenishmentService.delivered_qty(outlet, sample_product, since=since, until=until) == 10
        assert ReplenishmentService.delivered_qty(outlet, sample_product, since=since) == 210

        unlinked = _outlet(db, None, stage="prospect")
        assert ReplenishmentService.delivered_qty(unlinked, sample_product, since=since) == 0

    def test_the_delivered_window_is_read_once_and_only_for_this_customer(
        self, db, sample_product, count_queries
    ):
        """M04: the DELIVERED-history gather carries the outlet's own customer.

        `orders` has no `delivered_at` column, so this table is where the D7 rate
        window (twice per product — the pairing leg and the 60-day history leg) and
        the stock-out projection all look; `stock_check` runs the lot once per product
        per visit. Gathering order ids with no user predicate reads every customer's
        delivery history to answer a question about one shop, and the outer
        `Order.user_id` filter then throws the rest away.

        The number is asserted on both sides of the narrowing: `delivered_qty` must
        keep returning 6 with a 999-unit delivery to a different customer sitting in
        the same window, so the predicate provably narrows the SCAN and not the ANSWER.
        Statement capture follows the ARCH-009 precedent
        (`tests/unit/test_cod_cap_scope_totals.py:114-127`).
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        _flag_for_stock_check(db, sample_product)
        other_owner = _owner(db, phone="+998901234592")
        _delivered_order(db, owner, sample_product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")
        _delivered_order(
            db,
            other_owner,
            sample_product,
            quantity=999,
            delivered_at=DELIVERY_AT,
            number="SA_000002_26",
        )
        # Touch both rows before counting: `delivered_qty` reads `outlet.user_id` and
        # `product.id`, and the commits above expired every instance, so an untouched
        # attribute would re-SELECT inside the measured window.
        assert outlet.user_id == owner.id
        assert sample_product.id is not None

        with count_queries() as counter:
            delivered = ReplenishmentService.delivered_qty(
                outlet, sample_product, since=FIRST_VISIT_AT, until=SECOND_VISIT_AT
            )

        assert delivered == 6
        history_reads = [statement for statement in counter.statements if "order_status_history" in statement]
        assert len(history_reads) == 1, history_reads
        assert "delivered_order.user_id" in history_reads[0], history_reads[0]

    def test_the_delivered_history_index_is_declared_on_the_model(self, db):
        """M04: `(new_status, changed_at)` is indexed, and the model says so too.

        Migration f1a2b3c4d5e6 creates the index on Postgres. The SQLite suite builds
        its schema from the models with `db.create_all()`, so an index that lived only
        in the migration would be invisible here and `flask db migrate` would propose
        dropping it on the next autogenerate. One index, two expressions, pinned to
        each other here and — against the real migrated DDL — by
        `tests/integration/test_sales_agent_pg_constraints.py::test_the_delivered_history_window_is_indexed_by_status_and_instant`.
        """
        declared = {
            index.name: [column.name for column in index.columns]
            for index in OrderStatusHistory.__table__.indexes
        }
        assert declared.get("idx_order_status_history_new_status_changed_at") == ["new_status", "changed_at"]

    def test_last_delivered_qty_returns_the_most_recent_delivered_quantity(self, db, sample_category, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        _flag_for_stock_check(db, sample_product)
        _delivered_order(
            db,
            owner,
            sample_product,
            quantity=9,
            delivered_at=datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc),
            number="SA_000001_26",
        )
        _delivered_order(
            db,
            owner,
            sample_product,
            quantity=4,
            delivered_at=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc),
            number="SA_000002_26",
        )
        _pending_order(db, owner, sample_product, quantity=99, number="SA_000003_26")

        assert ReplenishmentService.last_delivered_qty(outlet, sample_product) == 4

        never_ordered = _extra_product(db, sample_category, "Pure Water 10L", "10L")
        assert ReplenishmentService.last_delivered_qty(outlet, never_ordered) is None

    def test_latest_stock_check_is_the_newest_visits_row(self, db, sales_agent_user, sample_category, sample_product):
        """One accessor answers "which check is newest" — the card and the stock-out
        projection both read it, so the ordering exists exactly once."""
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        first, second = _seven_day_scenario(db, sales_agent_user, owner, outlet, product)

        newest = ReplenishmentService.latest_stock_check(outlet, product)

        assert newest is not None
        assert newest.visit_id == second.id
        assert newest.on_hand_qty == 3
        # The `visit` backref is what `predicted_stockout_at` reads for the instant, so it must
        # resolve to the SAME visit the ordering picked. (No datetime equality here: SQLite hands
        # `started_at` back naive, which is why the service normalises with `_aware`.)
        assert newest.visit.id == second.id
        assert newest.visit_id != first.id

        never_counted = _extra_product(db, sample_category, "Pure Water 10L", "10L")
        assert ReplenishmentService.latest_stock_check(outlet, never_counted) is None

    def test_rate_per_day_from_two_stock_checks_is_thirteen_over_seven(self, db, sales_agent_user, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _seven_day_scenario(db, sales_agent_user, owner, outlet, product)

        rate, source = ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC)

        assert source == "stock_checks"
        assert rate == Decimal("1.857")  # (10 + 6 - 3) / 7 at visit_stock_checks.rate_per_day's numeric(8,3)
        assert abs(rate - Decimal(13) / Decimal(7)) < Decimal("0.001")

    def test_an_abandoned_visits_count_is_not_a_stock_check(self, db, sales_agent_user, sample_product):
        """C04: `previous_stock_rows` counts completed visits only; the rate rule counted EVERY
        visit -- so the hourly abandon sweep manufactured the "previous check" that the money
        numbers (rate, suggestion, stock-out projection) were then measured against.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _first, second = _seven_day_scenario(db, sales_agent_user, owner, outlet, product)
        # The agent re-opened a visit the same moment the good one closed, counted the back-store
        # crates too, and the phone died: the sweep abandoned it. Same instant, higher id, so it
        # wins the `started_at desc, id desc` ordering the two consumers share.
        dead = _visit(db, outlet, sales_agent_user, started_at=SECOND_VISIT_AT)
        dead.status = "abandoned"
        db.session.commit()
        _stock_check(db, dead, product, on_hand=99)

        assert ReplenishmentService.latest_stock_check(outlet, product).visit_id == second.id
        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (
            Decimal("1.857"),
            "stock_checks",
        )

    def test_only_the_callers_own_open_visit_counts_beside_the_completed_ones(
        self, db, sales_agent_user, sample_product
    ):
        """C28: `uq_visits_one_open_per_agent` is per AGENT, so two agents can hold open visits at
        one outlet -- and `get_for_agent` admits both the assigned and the onboarding agent. With
        no notion of WHOSE check a row is, this agent's count was paired with the other's.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        other_agent = make_sales_agent_user(db, phone="+998901234593")

        done = _visit(db, outlet, sales_agent_user, started_at=FIRST_VISIT_AT)
        _stock_check(db, done, product, on_hand=10)
        _delivered_order(db, owner, product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")
        theirs = _visit(db, outlet, other_agent, started_at=SECOND_VISIT_AT)
        theirs.status = "in_progress"
        mine = _visit(db, outlet, sales_agent_user, started_at=SECOND_VISIT_AT)
        mine.status = "in_progress"
        db.session.commit()
        _stock_check(db, theirs, product, on_hand=40)
        _stock_check(db, mine, product, on_hand=3)

        # Computing MY rate: my own open visit is the latest check, the other agent's is invisible.
        assert ReplenishmentService.latest_stock_check(outlet, product, current_visit_id=mine.id).visit_id == mine.id
        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC, current_visit_id=mine.id) == (
            Decimal("1.857"),
            "stock_checks",
        )
        # The card and the nightly recompute name no visit: only finished ones count for them.
        assert ReplenishmentService.latest_stock_check(outlet, product).visit_id == done.id

    def test_two_counts_on_one_day_never_fabricate_a_zero_rate(self, db, sales_agent_user, sample_product):
        """#14: `days = max(0, 1)` read two counts hours apart as "consumed nothing in a day".

        A rate of 0 is not "unknown": `suggested_qty` returns 0 from it and
        `predicted_stockout_at` refuses to project at all, so the D7 rule goes silent on the
        outlet the agent just stood in.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _delivered_order(
            db,
            owner,
            product,
            quantity=30,
            delivered_at=FROZEN_UTC - timedelta(days=10),
            number="SA_000001_26",
        )
        morning = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC - timedelta(hours=3))
        _stock_check(db, morning, product, on_hand=12)
        noon = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC)
        _stock_check(db, noon, product, on_hand=12)

        # No pair spans a day, so the measurement falls through to delivered history: 30 / 60.
        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (Decimal("0.500"), "orders")
        assert noon.id != morning.id

    def test_the_previous_count_is_the_newest_one_a_full_day_older(self, db, sales_agent_user, sample_product):
        """The same-day count is SKIPPED, not fallen back from: the visit before it still measures."""
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        week_ago = _visit(db, outlet, sales_agent_user, started_at=FIRST_VISIT_AT)
        _stock_check(db, week_ago, product, on_hand=10)
        _delivered_order(db, owner, product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")
        morning = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC - timedelta(hours=3))
        _stock_check(db, morning, product, on_hand=5)
        noon = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC)
        _stock_check(db, noon, product, on_hand=3)

        # (10 + 6 - 3) / 7 — measured against the week-old count, exactly as if the extra
        # same-day check had never been taken.
        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (
            Decimal("1.857"),
            "stock_checks",
        )

    def test_a_day_spanning_count_is_found_behind_any_number_of_same_day_re_visits(
        self, db, sales_agent_user, sample_product
    ):
        """The previous count is the newest one a full day older -- however many re-visits sit on top.

        With a fixed window of three rows, a THIRD same-day call pushed the day-spanning count to
        position four and the measurement fell through to 60-day delivered history: the outlet an
        agent had just counted three times scored an average instead of its own consumption.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        week_ago = _visit(db, outlet, sales_agent_user, started_at=FIRST_VISIT_AT)
        _stock_check(db, week_ago, product, on_hand=10)
        _delivered_order(db, owner, product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")
        for hours in (5, 3, 1):
            same_day = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC - timedelta(hours=hours))
            _stock_check(db, same_day, product, on_hand=5)
        noon = _visit(db, outlet, sales_agent_user, started_at=FROZEN_UTC)
        _stock_check(db, noon, product, on_hand=3)

        # (10 + 6 - 3) / 7 -- the week-old count still measures, exactly as with one re-visit.
        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (
            Decimal("1.857"),
            "stock_checks",
        )

    def test_incoming_qty_counts_only_what_can_still_reach_the_shelf(self, db, sample_product):
        """C26: the water the store has already bought but not yet received.

        `delivered_qty` answers "what landed"; this answers "what is on its way". The two must
        not overlap (a DELIVERED order would be counted twice) and an order that will never
        arrive must not pad the shelf at all.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        after = SECOND_VISIT_AT + timedelta(minutes=5)

        _pending_order(db, owner, product, quantity=20, number="SA_000001_26", created_at=after)
        _pending_order(
            db, owner, product, quantity=7, number="SA_000002_26", status=OrderStatus.CONFIRMED, created_at=after
        )
        _pending_order(
            db, owner, product, quantity=99, number="SA_000003_26", status=OrderStatus.CANCELLED, created_at=after
        )
        # Ruling 53: the water came back to the depot, so it is not on its way to this shelf.
        _pending_order(
            db, owner, product, quantity=40, number="SA_000006_26", status=OrderStatus.RETURNED, created_at=after
        )
        # Already counted by `delivered_qty` — counting it here too would double the shelf.
        _delivered_order(db, owner, product, quantity=6, delivered_at=after, number="SA_000004_26")
        # Placed BEFORE the count and still undelivered: it is not on the shelf the agent counted
        # (nothing undelivered can be) and `delivered_qty` starts AT the count, so this water used
        # to be invisible to both legs of the projection.
        _pending_order(
            db,
            owner,
            product,
            quantity=50,
            number="SA_000005_26",
            created_at=SECOND_VISIT_AT - timedelta(days=1),
        )

        assert ReplenishmentService.incoming_qty(outlet, product, now=FROZEN_UTC) == 77

    def test_incoming_qty_ignores_an_order_that_never_landed(self, db, app, sample_product):
        """Ruling 72: what is "on its way" has a horizon — SALES_RATE_HISTORY_DAYS.

        An order that has sat un-DELIVERED for two months is not water on its way to
        this shelf; it is a stuck row nobody will ever unstick. Without a horizon it
        padded `predicted_stockout_at` forever — the projection pushed further out
        every day, so the outlet quietly fell off the due list and the agent stopped
        being sent to a shop that was actually running dry.

        The window is the SAME config the rate's history leg reads, derived here rather
        than hard-coded, so one number governs "how far back does an order still mean
        anything" on both legs of the projection.
        """
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        history_days = int(app.config["SALES_RATE_HISTORY_DAYS"])

        _pending_order(
            db,
            owner,
            product,
            quantity=20,
            number="SA_000001_26",
            created_at=FROZEN_UTC - timedelta(days=history_days - 1),
        )
        _pending_order(
            db,
            owner,
            product,
            quantity=500,
            number="SA_000002_26",
            created_at=FROZEN_UTC - timedelta(days=history_days + 1),
        )

        assert ReplenishmentService.incoming_qty(outlet, product, now=FROZEN_UTC) == 20

    def test_the_incoming_statuses_are_derived_from_the_shared_transition_map(self):
        """The excluded set, pinned (ruling 53).

        Three exclusions for three different reasons: DELIVERED is already counted by
        `delivered_qty`; CANCELLED is a dead end in the shared transition map, so it will never
        arrive; and a RETURNED order's water is physically back at the depot — it is not on its
        way to this shelf. If a returned order re-enters PENDING it counts again from then.
        """
        assert set(INCOMING_ORDER_STATUSES) == {
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.OUT_FOR_DELIVERY,
        }
        excluded = set(OrderStatus) - set(INCOMING_ORDER_STATUSES)
        assert excluded == {OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED}
        # CANCELLED is excluded BY the derivation (no onward transition); the other two are named
        # exclusions, and RETURNED proves the naming is load-bearing -- it has somewhere to go.
        assert not ORDER_STATUS_TRANSITIONS[OrderStatus.CANCELLED]
        assert ORDER_STATUS_TRANSITIONS[OrderStatus.RETURNED] == [OrderStatus.PENDING]

    def test_rate_per_day_falls_back_to_sixty_day_order_history(self, db, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _delivered_order(
            db,
            owner,
            product,
            quantity=30,
            delivered_at=datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc),
            number="SA_000001_26",
        )
        _delivered_order(  # older than SALES_RATE_HISTORY_DAYS — must not inflate the rate
            db,
            owner,
            product,
            quantity=600,
            delivered_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            number="SA_000002_26",
        )

        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (Decimal("0.500"), "orders")

    def test_rate_per_day_is_none_when_there_is_no_signal(self, db, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _delivered_order(
            db,
            owner,
            product,
            quantity=600,
            delivered_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            number="SA_000001_26",
        )

        assert ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC) == (None, "none")


class TestSuggestionAndDueDate:
    def test_suggested_qty_is_twenty_for_the_spec_scenario(self, db, sales_agent_user, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner, outlet_class="A")
        product = _flag_for_stock_check(db, sample_product)
        _seven_day_scenario(db, sales_agent_user, owner, outlet, product)

        rate, source = ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC)
        cadence = ReplenishmentService.cadence_days(outlet)
        assert (source, cadence) == ("stock_checks", 7)

        # ceil(13/7 x (7 + 1) x 1.5 - 3) = ceil(19.284) = 20
        assert ReplenishmentService.suggested_qty(product, rate, 3, cadence, last_qty=None) == 20

    def test_suggested_qty_applies_the_min_order_quantity_floor(self, db, sample_product):
        product = _flag_for_stock_check(db, sample_product, min_order_quantity=24)

        # ceil(0.1 x 8 x 1.5 - 0) = 2, floored to the product's own minimum.
        assert ReplenishmentService.suggested_qty(product, Decimal("0.100"), 0, 7, last_qty=None) == 24

    def test_suggested_qty_stays_zero_when_stock_covers_the_cover_period(self, db, sample_product):
        product = _flag_for_stock_check(db, sample_product, min_order_quantity=24)

        # 1.2 - 5 is negative: the floor must not resurrect an order nobody needs.
        assert ReplenishmentService.suggested_qty(product, Decimal("0.100"), 5, 7, last_qty=None) == 0

    def test_suggested_qty_falls_back_to_the_last_delivered_quantity(self, db, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner)
        product = _flag_for_stock_check(db, sample_product)
        _delivered_order(
            db,
            owner,
            product,
            quantity=7,
            delivered_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            number="SA_000001_26",
        )

        rate, source = ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC)
        last_qty = ReplenishmentService.last_delivered_qty(outlet, product)
        assert (rate, source, last_qty) == (None, "none", 7)

        assert ReplenishmentService.suggested_qty(product, rate, 2, 7, last_qty=last_qty) == 7
        assert ReplenishmentService.suggested_qty(product, None, 2, 7, last_qty=None) is None

    def test_suggested_qty_never_exceeds_the_counted_quantity_ceiling(
        self, db, app, monkeypatch, sample_product
    ):
        """M30: nothing bounded a suggestion, and a suggestion is one tap from a real order.

        `SALES_STOCK_QTY_MAX` already bounds what an agent may COUNT, and the rate is derived
        from those counts -- so a mistyped or fabricated rate printed a suggestion of thousands
        that the order door (also bounded now) would refuse. The fallback is bounded by the same
        number, because a pre-fill the order door refuses is a dead end on the confirm screen.
        """
        monkeypatch.setitem(app.config, "SALES_STOCK_QTY_MAX", 5)
        product = _flag_for_stock_check(db, sample_product, min_order_quantity=1)

        # ceil(40 x (7 + 1) x 1.5 - 0) = 480, capped at the one ceiling.
        assert ReplenishmentService.suggested_qty(product, Decimal("40.000"), 0, 7, last_qty=None) == 5
        assert ReplenishmentService.suggested_qty(product, None, 0, 7, last_qty=60) == 5
        # ...and a suggestion under the ceiling is untouched: ceil(0.25 x 8 x 1.5 - 0) = 3.
        assert ReplenishmentService.suggested_qty(product, Decimal("0.250"), 0, 7, last_qty=None) == 3
        assert ReplenishmentService.suggested_qty(product, None, 0, 7, last_qty=None) is None

    def test_suggested_qty_never_exceeds_what_the_order_door_will_accept(self, db, app, sample_product):
        """Ruling 71: the suggestion is bounded by the SMALLER of the two ceilings.

        `SALES_STOCK_QTY_MAX` (500) bounds what an agent may COUNT; `MAX_QUANTITY_PER_ITEM`
        (100) is what `OrderService._process_order_items` enforces on every line of every
        channel. A suggestion between the two is one tap from a refusal, so the pre-fill is a
        dead end on the confirm screen. NOTHING is patched here: 480 is under 500 and over 100,
        which is exactly the window in which the two ceilings differ.
        """
        assert (app.config["SALES_STOCK_QTY_MAX"], app.config["MAX_QUANTITY_PER_ITEM"]) == (500, 100)
        product = _flag_for_stock_check(db, sample_product, min_order_quantity=1)

        # ceil(40 x (7 + 1) x 1.5 - 0) = 480 -- under SALES_STOCK_QTY_MAX, over the real bound.
        assert ReplenishmentService.suggested_qty(product, Decimal("40.000"), 0, 7, last_qty=None) == 100
        assert ReplenishmentService.suggested_qty(product, None, 0, 7, last_qty=480) == 100

    def test_a_purchase_minimum_above_the_ceiling_suggests_nothing_at_all(self, db, app, sample_product):
        """M02: the only pre-fill that cleared the ceiling was one the door refuses.

        `min_order_quantity` has no upper bound anywhere — the admin write only enforces
        `>= 1` — while every line is capped at `effective_line_qty_max()`. Set the floor
        above the ceiling and the two guards are unsatisfiable together: the clamp hands
        back `qty_max`, and the SAME cycle's `VisitService._assert_line_quantities` (and
        `create_order`'s own backstop behind it) refuse it for being under the minimum. The
        agent reads a suggestion, taps *Take suggestion*, reads a total out to the
        shopkeeper and only then learns the basket cannot be sent.

        There is no quantity that door would accept, so there is no suggestion to make:
        answered None on BOTH paths, the rate one and the last-order fallback. The floor is
        not weakened to reach an orderable number — `create_order` enforces it for every
        channel, so bending it here would only move the refusal one screen later.
        """
        qty_max = app.config["MAX_QUANTITY_PER_ITEM"]
        assert min(app.config["SALES_STOCK_QTY_MAX"], qty_max) == 100
        product = _flag_for_stock_check(db, sample_product, min_order_quantity=qty_max + 50)

        assert ReplenishmentService.suggested_qty(product, Decimal("40.000"), 0, 7, last_qty=None) is None
        assert ReplenishmentService.suggested_qty(product, None, 0, 7, last_qty=480) is None
        # ...and a floor exactly ON the ceiling is still orderable, so it still suggests.
        product.min_order_quantity = qty_max
        db.session.commit()
        assert ReplenishmentService.suggested_qty(product, Decimal("40.000"), 0, 7, last_qty=None) == qty_max

    def test_next_visit_due_at_agent_date_wins(self, db, sample_product):
        """A date the agent picked at close beats the class cadence, at local 09:00."""
        owner = _owner(db)
        _flag_for_stock_check(db, sample_product)
        outlet = _outlet(
            db,
            owner,
            outlet_class="A",
            agent_next_visit_at=date(2026, 9, 9),
            last_visit_at=SECOND_VISIT_AT,
        )

        due = ReplenishmentService.next_visit_due_at(outlet, now=FROZEN_UTC)

        # 09:00 Tashkent on 2026-09-09 == 04:00 UTC; cadence would have said 2026-09-15 06:00 UTC.
        assert due == datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)

    def test_next_visit_due_at_predicted_stockout_wins(self, db, sales_agent_user, sample_product):
        owner = _owner(db)
        outlet = _outlet(db, owner, outlet_class="A", last_visit_at=SECOND_VISIT_AT)
        product = _flag_for_stock_check(db, sample_product)
        _seven_day_scenario(db, sales_agent_user, owner, outlet, product)

        stockout = ReplenishmentService.predicted_stockout_at(outlet, product, now=FROZEN_UTC)
        due = ReplenishmentService.next_visit_due_at(outlet, now=FROZEN_UTC)

        # 3 on hand at 1.857/day is ~1.62 days of cover.
        assert timedelta(days=1.6) < stockout - SECOND_VISIT_AT < timedelta(days=1.7)
        assert due == stockout - timedelta(days=1)  # SALES_DELIVERY_LEAD_DAYS
        assert due.date() == date(2026, 9, 8)
        assert due < SECOND_VISIT_AT + timedelta(days=7)  # the cadence candidate lost

    def test_a_productive_visit_does_not_put_the_outlet_back_on_the_due_list(
        self, db, sales_agent_user, sample_product
    ):
        """C26 end to end: the same outlet as `..._predicted_stockout_wins`, plus the order.

        Without the 20 bottles in the numerator the projection says the shelf empties in 1.6
        days, `stockout - lead` lands on the day of the visit itself, survives the ruling-33
        floor (it is not EARLIER than `last_visit_at`) and wins `min()` — so the agent closes a
        visit having sold 20 bottles and the shop is back at the top of *Due today* the same
        afternoon. With it, the date the agent and the shopkeeper agreed on stands.
        """
        owner = _owner(db)
        outlet = _outlet(
            db,
            owner,
            outlet_class="A",
            last_visit_at=SECOND_VISIT_AT,
            agent_next_visit_at=date(2026, 9, 15),  # the frozen day + 7, what the agent picked
        )
        product = _flag_for_stock_check(db, sample_product)
        _seven_day_scenario(db, sales_agent_user, owner, outlet, product)
        _pending_order(
            db,
            owner,
            product,
            quantity=20,
            number="SA_000002_26",
            created_at=SECOND_VISIT_AT + timedelta(minutes=5),
        )

        stockout = ReplenishmentService.predicted_stockout_at(outlet, product, now=FROZEN_UTC)
        due = ReplenishmentService.next_visit_due_at(outlet, now=FROZEN_UTC)

        # 3 on the shelf + 20 on the way at 1.857/day is ~12.4 days of cover, not 1.6.
        assert timedelta(days=12) < stockout - SECOND_VISIT_AT < timedelta(days=13)
        # 09:00 Tashkent on 2026-09-15 == 04:00 UTC — the agent's own date, which now wins.
        assert due == datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)

    def test_closing_a_visit_that_sold_water_honours_the_date_the_agent_picked(
        self, db, monkeypatch, frozen_now, sales_agent_user, sample_product
    ):
        """C26 through the REAL close path, which is where it bites (ruling 54).

        The sibling above presets `agent_next_visit_at`; this one drives
        `VisitService.close(next_visit_at=+7d)` on a visit carrying an undelivered order, which
        is the sequence a productive call actually takes: `close` stamps `last_visit_at`, writes
        the agreed date and then recomputes. Before the fix the projection ran off the 3 bottles
        left on the shelf, landed the same evening, survived the ruling-33 floor (it is not
        EARLIER than the visit) and won `min()` -- so the shop the agent had just served was
        back at the top of *Due today* before the agent reached the next street.
        """
        from business_app.services.sales import visit_service as visit_module

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return FROZEN_UTC.astimezone(tz) if tz else FROZEN_UTC.replace(tzinfo=None)

        monkeypatch.setattr(visit_module, "datetime", _FrozenDatetime)

        owner = _owner(db)
        outlet = _outlet(db, owner, outlet_class="A", assigned_agent_user_id=sales_agent_user.id)
        product = _flag_for_stock_check(db, sample_product)

        # The previous call: 10 on the shelf, 6 delivered since, so the rate is the spec's 13/7.
        previous = _visit(db, outlet, sales_agent_user, started_at=FIRST_VISIT_AT)
        _stock_check(db, previous, product, on_hand=10)
        _delivered_order(db, owner, product, quantity=6, delivered_at=DELIVERY_AT, number="SA_000001_26")

        # Today's call, still open: 3 counted on the shelf and 20 booked for delivery.
        today = _visit(db, outlet, sales_agent_user, started_at=SECOND_VISIT_AT)
        today.status = "in_progress"
        today.current_step = "close"
        db.session.commit()
        _stock_check(db, today, product, on_hand=3)
        order = _pending_order(
            db, owner, product, quantity=20, number="SA_000002_26", created_at=SECOND_VISIT_AT + timedelta(minutes=1)
        )
        order.visit_id = today.id
        db.session.commit()

        chosen = date(2026, 9, 15)  # the frozen day + 7, what the agent and the shopkeeper agreed
        VisitService.close(
            today, outcome=None, no_order_reason=None, notes=None, next_visit_at=chosen, dm_present=None
        )

        reloaded = Outlet.query.get(outlet.id)
        assert Visit.query.get(today.id).outcome == "order_placed"
        assert reloaded.agent_next_visit_at == chosen
        # 09:00 Tashkent on the chosen date == 04:00 UTC. Not one minute earlier: an order the
        # shop has not received yet still counts as cover.
        assert ensure_utc(reloaded.next_visit_due_at) == datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
        assert ensure_utc(reloaded.next_visit_due_at) >= ReplenishmentService._agent_date_utc(chosen)

    def test_next_visit_due_at_cadence_wins(self, db, sample_product):
        owner = _owner(db)
        _flag_for_stock_check(db, sample_product)
        outlet = _outlet(
            db,
            owner,
            outlet_class="A",
            agent_next_visit_at=date(2026, 9, 20),
            last_visit_at=datetime(2026, 9, 6, 6, 0, tzinfo=timezone.utc),
        )

        due = ReplenishmentService.next_visit_due_at(outlet, now=FROZEN_UTC)

        assert due == datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)

    def test_next_visit_due_at_never_lands_before_the_visit_that_just_closed(
        self, db, sales_agent_user, sample_product
    ):
        """D7 floor (ruling 33): a stock-out projected off a three-week-old count is
        history, not a plan.

        The last count was 21 days ago — 3 on hand at 1.857/day — so the shelf ran dry
        around 2026-08-19, three weeks BEFORE the visit the agent closed today. Left
        unfloored, `stockout - lead` would put `next_visit_due_at` in the past and make
        Task 6's `overdue_days` read "+21d" the instant a visit ends. The visit answers
        everything the outlet knew before it, so a candidate earlier than `last_visit_at`
        is dropped and the date the agent actually picked wins.
        """
        owner = _owner(db)
        outlet = _outlet(
            db,
            owner,
            outlet_class="A",
            last_visit_at=FROZEN_UTC,
            agent_next_visit_at=date(2026, 9, 11),  # the frozen day + 3
        )
        product = _flag_for_stock_check(db, sample_product)
        first = _visit(db, outlet, sales_agent_user, started_at=STALE_FIRST_VISIT_AT)
        _stock_check(db, first, product, on_hand=10)
        _delivered_order(db, owner, product, quantity=6, delivered_at=STALE_DELIVERY_AT, number="SA_000001_26")
        second = _visit(db, outlet, sales_agent_user, started_at=STALE_SECOND_VISIT_AT)
        _stock_check(db, second, product, on_hand=3)

        rate, source = ReplenishmentService.rate_per_day(outlet, product, now=FROZEN_UTC)
        stockout = ReplenishmentService.predicted_stockout_at(outlet, product, now=FROZEN_UTC)
        due = ReplenishmentService.next_visit_due_at(outlet, now=FROZEN_UTC)

        # The spec's own 13/7 — only the calendar moved.
        assert (rate, source) == (Decimal("1.857"), "stock_checks")
        # The candidate the floor has to reject: three weeks before the visit just closed.
        assert stockout - timedelta(days=1) < FROZEN_UTC - timedelta(days=20)

        assert due == datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)  # agent date at 09:00 Tashkent
        assert due > FROZEN_UTC  # never a due date in the past

    def test_next_visit_due_at_is_now_for_a_never_visited_active_outlet(self, db):
        owner = _owner(db)
        active = _outlet(db, owner, stage="active")
        dormant = _outlet(db, None, stage="dormant")

        assert ReplenishmentService.next_visit_due_at(active, now=FROZEN_UTC) == FROZEN_UTC
        assert ReplenishmentService.next_visit_due_at(dormant, now=FROZEN_UTC) == FROZEN_UTC

    def test_next_visit_due_at_is_none_for_a_prospect_without_an_agent_date(self, db):
        prospect = _outlet(db, None, stage="prospect", outlet_class=None)
        lost = _outlet(db, None, stage="lost", outlet_class=None)

        assert ReplenishmentService.next_visit_due_at(prospect, now=FROZEN_UTC) is None
        assert ReplenishmentService.next_visit_due_at(lost, now=FROZEN_UTC) is None

        prospect.agent_next_visit_at = date(2026, 9, 11)
        assert ReplenishmentService.next_visit_due_at(prospect, now=FROZEN_UTC) == datetime(
            2026, 9, 11, 4, 0, tzinfo=timezone.utc
        )

    def test_recompute_outlet_writes_the_column_without_committing(self, db, frozen_now):
        owner = _owner(db)
        outlet = _outlet(db, owner, stage="active")
        outlet_id = outlet.id

        ReplenishmentService.recompute_outlet(outlet)

        assert outlet.next_visit_due_at == FROZEN_UTC  # `now` came from the frozen module clock
        assert outlet in db.session.dirty  # still pending: the caller owns the transaction

        db.session.rollback()
        assert Outlet.query.get(outlet_id).next_visit_due_at is None

    def test_local_day_end_utc_is_the_end_of_the_local_day(self, db):
        # 2026-09-08 22:00 UTC is 03:00 Tashkent on the 9th: the UTC day is still
        # the 8th, the business day has already rolled over.
        assert ReplenishmentService.local_day_end_utc(datetime(2026, 9, 8, 22, 0, tzinfo=timezone.utc)) == datetime(
            2026, 9, 9, 19, 0, tzinfo=timezone.utc
        )

        assert ReplenishmentService.local_day_end_utc(FROZEN_UTC) == datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)

    def test_local_day_end_utc_agrees_with_the_route_day_boundary(self, db, monkeypatch):
        """One definition of the local day: the due list and the driver's day cannot drift.

        Both sides were read from the WALL CLOCK, a few microseconds apart. That is a test
        that fails once a year at exactly 00:00 Tashkent -- when the two reads straddle local
        midnight and the right-hand side jumps a day -- and is otherwise unfalsifiable. One
        frozen instant feeds both, and the calendar literal is the same one the sibling test
        above asserts for the explicit-argument call, so the no-argument path is pinned to
        read the same clock rather than merely to agree with itself.

        Two modules are frozen, not one: `local_day_end_utc` now derives its answer from
        `local_windows` (whose clock is `delivery_window.local_now`), while the driver's day
        still comes from `route_optimization_service`. Freezing both is what makes the
        equality a statement ABOUT the two definitions instead of about two wall-clock reads.
        """
        from business_app.services import route_optimization_service as ros
        from business_app.utils import delivery_window

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return FROZEN_UTC.astimezone(tz) if tz else FROZEN_UTC.replace(tzinfo=None)

        monkeypatch.setattr(ros, "datetime", _FrozenDatetime)
        monkeypatch.setattr(delivery_window, "datetime", _FrozenDatetime)

        assert ReplenishmentService.local_day_end_utc() == ros._driver_day_start_utc() + timedelta(days=1)
        assert ReplenishmentService.local_day_end_utc() == datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)


class TestBranchScopedHistory:
    """D25 rule 5: on a chain account every branch's numbers come from ITS OWN address.

    Before branch outlets, `outlets.user_id` was unique, so "this outlet's history" and
    "this account's history" were the same sentence. With two shops on one account they are
    not: unscoped, a busy branch's deliveries hide a dead one's silence, and every number the
    agent is shown for the dead branch -- rate, suggested quantity, stage -- is the chain's.
    """

    def test_a_branch_counts_only_what_was_delivered_to_its_own_address(self, db, sample_product):
        owner, busy, quiet, busy_address, quiet_address = _chain(db, sample_product)
        _delivered_order(db, owner, sample_product, quantity=6, delivered_at=DELIVERY_AT,
                         number="SA_000401_26", delivery_address_id=busy_address.id)
        _delivered_order(db, owner, sample_product, quantity=11, delivered_at=SECOND_VISIT_AT,
                         number="SA_000402_26", delivery_address_id=quiet_address.id)

        assert ReplenishmentService.delivered_qty(busy, sample_product, since=FIRST_VISIT_AT) == 6
        assert ReplenishmentService.delivered_qty(quiet, sample_product, since=FIRST_VISIT_AT) == 11
        # The D15 fallback quantity: "order what they took last time" must mean last time HERE.
        assert ReplenishmentService.last_delivered_qty(busy, sample_product) == 6
        assert ReplenishmentService.last_delivered_qty(quiet, sample_product) == 11
        # The stage sweep's one input (`update_stages` -> `delivered_instants`).
        assert [ensure_utc(i) for i in ReplenishmentService.delivered_instants(busy, limit=5)] == [DELIVERY_AT]
        assert [ensure_utc(i) for i in ReplenishmentService.delivered_instants(quiet, limit=5)] == [SECOND_VISIT_AT]

    def test_a_siblings_undelivered_order_is_not_on_its_way_to_this_branch(self, db, sample_product):
        """`incoming_qty` pushes the stock-out projection out. A pallet booked for the OTHER
        branch used to push this one's, so the shop that was actually running dry fell off the
        due list because its sibling had just ordered."""
        owner, busy, quiet, busy_address, quiet_address = _chain(db, sample_product)
        _pending_order(db, owner, sample_product, quantity=4, number="SA_000403_26",
                       created_at=DELIVERY_AT, delivery_address_id=busy_address.id)
        _pending_order(db, owner, sample_product, quantity=7, number="SA_000404_26",
                       created_at=DELIVERY_AT, delivery_address_id=quiet_address.id)

        assert ReplenishmentService.incoming_qty(busy, sample_product, now=FROZEN_UTC) == 4
        assert ReplenishmentService.incoming_qty(quiet, sample_product, now=FROZEN_UTC) == 7

    def test_a_single_outlet_account_still_counts_its_address_less_history(self, db, sample_product):
        """R2. `orders.delivery_address_id` is nullable and legacy rows carry NULL, so the
        address conjunct is added ONLY when the account really has siblings. A one-shop
        account -- every account on the estate until an import creates a second outlet --
        answers exactly as it did before this change."""
        owner = _owner(db, phone="+998901234594")
        address = _address(db, owner, full_address="Chilonzor 5", is_default=True)
        only = _outlet(db, owner, address_id=address.id)
        _flag_for_stock_check(db, sample_product)
        _delivered_order(db, owner, sample_product, quantity=8, delivered_at=DELIVERY_AT,
                         number="SA_000405_26", delivery_address_id=None)

        assert ReplenishmentService.delivered_qty(only, sample_product, since=FIRST_VISIT_AT) == 8
        assert ReplenishmentService.last_delivered_qty(only, sample_product) == 8

    def test_a_branchs_address_less_legacy_orders_are_the_documented_cost(self, db, sample_product):
        """R3's stated price, pinned so nobody 'fixes' it into a fallback by accident.

        Once an account has two outlets, an order with no `delivery_address_id` belongs to
        neither branch and is counted for neither. Falling back to the account when a branch
        has no address-stamped history would hand a dead branch its sibling's rhythm -- the
        exact bug this task exists to remove -- so the trade is deliberate: `Mark lost` and a
        fresh visit rebuild the number, an implicit fallback never would.
        """
        owner, busy, quiet, busy_address, _quiet_address = _chain(db, sample_product)
        _delivered_order(db, owner, sample_product, quantity=9, delivered_at=DELIVERY_AT,
                         number="SA_000406_26", delivery_address_id=None)
        _delivered_order(db, owner, sample_product, quantity=2, delivered_at=SECOND_VISIT_AT,
                         number="SA_000407_26", delivery_address_id=busy_address.id)

        assert ReplenishmentService.delivered_qty(busy, sample_product, since=FIRST_VISIT_AT) == 2
        assert ReplenishmentService.delivered_qty(quiet, sample_product, since=FIRST_VISIT_AT) == 0
        assert ReplenishmentService.last_delivered_qty(quiet, sample_product) is None
        assert ReplenishmentService.delivered_instants(quiet, limit=5) == []
