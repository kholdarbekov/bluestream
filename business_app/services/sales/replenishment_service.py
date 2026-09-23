"""D7/D15 replenishment rules: consumption rate, suggested order, next-visit-due.

Every number the agent sees is computed here and published as a field (D15):
the staff bot and the admin UI never re-derive a rate, a suggestion or a due
date. Two facts shape the queries:

  * `orders` has no `delivered_at` column, so "when did this land" is the
    DELIVERED row in `order_status_history`;
  * an outlet only has consumption history once it is linked to a customer
    account (`outlets.user_id`), so an unlinked prospect scores zero rather
    than raising;
  * an account may own several BRANCH outlets, one per address (D25), so
    "whose history is this" is asked once, of `OutletService.order_scope`,
    which adds the `delivery_address_id` conjunct only in branch mode. Every
    query below filters through it and none of them restates the rule.

Only `recompute_all` — the nightly job's own entry point — commits. `recompute_outlet`
writes the column and leaves the transaction to its caller (`VisitService.close`,
`OutletService._record_stage`).
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from business_app import db
from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.product import Product
from business_app.models.sales import Outlet
from business_app.models.sales_visits import Visit, VisitStockCheck
from business_app.utils.local_windows import local_date, local_day_bounds
from business_app.utils.timezone_utils import ensure_utc
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus
from shared.status_transitions import ORDER_STATUS_TRANSITIONS

# `visit_stock_checks.rate_per_day` is numeric(8,3): quantize where the rate is
# computed so the published field and the stored field are the same number.
RATE_QUANTUM = Decimal("0.001")

# "The caller did not look the catalogue up for me." `None` cannot say this: it is the honest
# answer when no returnable SKU is flagged at all, and treating it as "unset" would make the
# nightly job re-query the whole product table for every outlet — the cost L30 reports.
_CATALOGUE_UNSET = object()

# The agent picks a DATE at visit close; the due list needs an instant. 09:00
# local is the start of the trading day the agent meant.
AGENT_VISIT_LOCAL_HOUR = 9


def effective_line_qty_max() -> int:
    """The largest quantity one ORDER LINE may carry — read by every sales surface.

    Two ceilings already exist and the smaller one is the only honest answer (ruling 71):

      * `SALES_STOCK_QTY_MAX` bounds a figure an agent may COUNT on a shelf
        (`VisitService._qty`), and by extension the rate derived from those counts;
      * `MAX_QUANTITY_PER_ITEM` is what `OrderService._process_order_items` enforces on
        every line of every channel — as a code-less 400 raised INSIDE `create_order`,
        which is the exact generic refusal the sales guard exists to replace.

    Suggesting, quoting and ordering therefore all read `min()` of the two rather than each
    picking a ceiling: a suggestion or a quote above the number `create_order` will really
    enforce is a dead end on the confirm screen, reached only after the agent has read a
    total out to the shopkeeper. One function, so the two call sites cannot drift.
    """
    return min(
        int(current_app.config["SALES_STOCK_QTY_MAX"]),
        int(current_app.config["MAX_QUANTITY_PER_ITEM"]),
    )


# A serving outlet that has never been visited is due immediately; a prospect is
# only due when the agent said so (D7).
DUE_WHEN_NEVER_VISITED_STAGES = ("active", "at_risk", "dormant")

# The stages whose due date nobody republishes: `recompute_all` skips them and so does an admin's
# class edit (D29), which must publish exactly the date tonight's run would. Skipped rather than
# nulled -- the due scope filters on stage anyway, and a shop somebody may yet win back keeps the
# date it had.
DUE_DATE_FROZEN_STAGES = ("lost",)

# Never incoming, whatever the transition map says (ruling 53). DELIVERED because
# `delivered_qty` has already counted it — adding it here would double the shelf. RETURNED
# because the water is physically back at the depot: it is not on its way to this shop, and if
# the order re-enters PENDING it starts counting again from then, under its new timestamp.
NEVER_INCOMING_ORDER_STATUSES = (OrderStatus.DELIVERED, OrderStatus.RETURNED)

# "Can this order still land on the shelf?" — the dead-end half is DERIVED from
# `ORDER_STATUS_TRANSITIONS`, the one transition map every surface shares, rather than
# hand-listed here, so a new status cannot quietly default into the stock-out projection: a
# status with nowhere left to go (CANCELLED) will never arrive.
INCOMING_ORDER_STATUSES = tuple(
    status
    for status, next_statuses in ORDER_STATUS_TRANSITIONS.items()
    if status not in NEVER_INCOMING_ORDER_STATUSES and next_statuses
)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite (tests) drops tzinfo on round-trip; every comparison here is UTC."""
    return ensure_utc(value) if value is not None else None


class ReplenishmentService:
    """D7 (next-visit-due) and D15 (suggested order) as published fields."""

    # Membership of the agent's stock list, written ONCE: the columns that must all be
    # true. The query below derives its WHERE from this tuple and `in_stock_check_list`
    # derives its answer for a single row from the same tuple, so a third clause added
    # here reaches the list, the admin row and the admin UI's tag together. Spelling the
    # pair out twice — once in SQL, once in Python — is exactly the drift M13 reports.
    STOCK_CHECK_COLUMNS = ("in_sales_stock_check", "is_active")

    @staticmethod
    def stock_check_products() -> List[Product]:
        """The admin-chosen stock-check list (D22)."""
        return (
            Product.query.filter(
                *(getattr(Product, column).is_(True) for column in ReplenishmentService.STOCK_CHECK_COLUMNS)
            )
            .order_by(Product.id.asc())
            .all()
        )

    @staticmethod
    def in_stock_check_list(product: Product) -> bool:
        """Does `stock_check_products()` return THIS product? (D22, M13.)

        The serializer publishes this as `in_agent_stock_list` so no client re-derives
        "flagged AND active" — the admin UI used to, and a flagged-but-inactive SKU was
        therefore tagged as counted on a screen the agent is never sent it on.
        """
        return all(bool(getattr(product, column)) for column in ReplenishmentService.STOCK_CHECK_COLUMNS)

    @staticmethod
    def primary_returnable_product() -> Optional[Product]:
        """The SKU the D7 stock-out prediction runs on: the flagged returnable
        bottle with the highest `returnable_bottles_per_unit`, ties by lowest id.

        "Returnable" is decided by `Product.is_returnable_bottle` — the SSOT that
        requires BOTH `tracks_returnable_bottles` and a per-unit count above zero
        — rather than by a second copy of that rule in SQL.
        """
        returnable = [p for p in ReplenishmentService.stock_check_products() if p.is_returnable_bottle]
        if not returnable:
            return None
        returnable.sort(key=lambda p: (-(p.returnable_bottles_per_unit or Decimal(0)), p.id))
        return returnable[0]

    @staticmethod
    def cadence_days(outlet: Outlet) -> int:
        override = outlet.cadence_days_override
        if override and int(override) > 0:
            return int(override)
        default_days = int(current_app.config["SALES_CADENCE_DAYS_C"])
        outlet_class = (outlet.outlet_class or "C").upper()
        return int(current_app.config.get(f"SALES_CADENCE_DAYS_{outlet_class}", default_days))

    @staticmethod
    def delivered_qty(
        outlet: Outlet,
        product: Product,
        *,
        since: datetime,
        until: Optional[datetime] = None,
    ) -> int:
        """Units of `product` delivered to this outlet's customer in (since, until].

        The order ids are gathered from the DELIVERED status-history rows first so
        that an order with more than one DELIVERED row cannot double-count its items —
        and the gather carries the same customer as the outer query, because `orders`
        has no `delivered_at` column and so this table is read once per product per
        stock check (twice more per rate window). Unbounded, that subquery read the
        whole estate's delivery history to answer a question about one shop. The pair
        it filters on, `(new_status, changed_at)`, is indexed by migration
        f1a2b3c4d5e6; `last_delivered_qty` below reads the same index.

        `aliased` rather than a bare join: the enclosing query already selects FROM
        `orders`, and a second unaliased `orders` inside the subquery reads as a
        correlation bug and would become one under any later `exists()` refactor.
        """
        if not outlet.user_id:
            return 0

        from business_app.services.sales.outlet_service import OutletService

        delivered_order = aliased(Order, name="delivered_order")
        delivered_orders = (
            select(OrderStatusHistory.order_id)
            .join(delivered_order, delivered_order.id == OrderStatusHistory.order_id)
            .where(
                delivered_order.user_id == outlet.user_id,
                OrderStatusHistory.new_status == OrderStatus.DELIVERED,
                OrderStatusHistory.changed_at > _aware(since),
            )
        )
        if until is not None:
            delivered_orders = delivered_orders.where(OrderStatusHistory.changed_at <= _aware(until))

        total = (
            db.session.query(func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .filter(
                # `order_scope` is the branch rule; the inner `delivered_order.user_id`
                # predicate above stays ACCOUNT-wide on purpose. It narrows the SCAN, not the
                # answer (M04) -- the outer filter is what decides which branch these units
                # belong to -- and an account has two or three branches, so re-scoping the
                # gather would buy nothing and would force the rule to be spelled a second
                # time against the alias.
                OutletService.order_scope(outlet),
                Order.status == OrderStatus.DELIVERED,
                OrderItem.product_id == product.id,
                Order.id.in_(delivered_orders),
            )
            .scalar()
        )
        return int(total or 0)

    @staticmethod
    def incoming_qty(outlet: Outlet, product: Product, *, now: datetime) -> int:
        """Units of `product` this outlet has already BOUGHT and not yet received.

        The order the agent placed at this very visit is PENDING (a linked store) or CONFIRMED
        (an unlinked one) — never DELIVERED — so `delivered_qty` scores it zero and the
        stock-out projection ran as if the shelf were still about to empty at the old rate. That
        projection then won `min()` in `next_visit_due_at` (the ruling-33 floor only drops
        candidates EARLIER than the visit), so the most productive visit of the day put its
        outlet straight back at the top of the due list.

        The window reaches BACK from `now`, not forward from the count (ruling 72). An
        undelivered order placed BEFORE the count is counted by neither leg — `delivered_qty`
        starts at the count, and so did this one — and it is exactly the water that is on its
        way, so a `created_at` floor at the count would lose it. But an order that has sat
        un-DELIVERED for a whole `SALES_RATE_HISTORY_DAYS` is not on its way to anywhere: it is
        a stuck row, and counting it padded the projection further out every day until the
        outlet fell off the due list entirely. Same config as the rate's history leg, so one
        number decides how far back an order still means anything.
        """
        if not outlet.user_id:
            return 0

        from business_app.services.sales.outlet_service import OutletService

        horizon_start = _aware(now) - timedelta(days=int(current_app.config["SALES_RATE_HISTORY_DAYS"]))
        total = (
            db.session.query(func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .filter(
                OutletService.order_scope(outlet),
                Order.status.in_(INCOMING_ORDER_STATUSES),
                OrderItem.product_id == product.id,
                Order.created_at >= horizon_start,
            )
            .scalar()
        )
        return int(total or 0)

    @staticmethod
    def last_delivered_qty(outlet: Outlet, product: Product) -> Optional[int]:
        """The quantity on the most recent delivered order — the D15 fallback."""
        if not outlet.user_id:
            return None

        from business_app.services.sales.outlet_service import OutletService

        row = (
            db.session.query(OrderItem.quantity)
            .join(Order, Order.id == OrderItem.order_id)
            .join(OrderStatusHistory, OrderStatusHistory.order_id == Order.id)
            .filter(
                OutletService.order_scope(outlet),
                Order.status == OrderStatus.DELIVERED,
                OrderItem.product_id == product.id,
                OrderStatusHistory.new_status == OrderStatus.DELIVERED,
            )
            .order_by(OrderStatusHistory.changed_at.desc(), Order.id.desc())
            .first()
        )
        return int(row[0]) if row else None

    @staticmethod
    def delivered_instants(outlet: Outlet, *, limit: int) -> List[datetime]:
        """When this outlet's last `limit` orders actually landed, newest first.

        The stage job's one input, read where every other number here reads it: `orders` has
        no `delivered_at` column, so the DELIVERED row in `order_status_history` is the
        instant. One row per ORDER (`max(changed_at)` grouped by `order_id`) because a
        re-delivered order carries two DELIVERED rows, and the second would read as a second
        purchase and halve the shop's measured buying interval.

        Filtered on `Order.status == DELIVERED` as well, exactly like `delivered_qty` and
        `last_delivered_qty`: an order that came back is not a delivery this outlet still
        has, and three queries answering "did this land" must not answer it three ways.

        An outlet with no customer account has no history at all and answers with an empty
        list rather than raising — the contract `delivered_qty` already keeps.

        On a chain account the answer is this BRANCH's landings (`OutletService.order_scope`),
        which is what lets one shop of a chain go `at_risk` while its sibling keeps buying.
        """
        if not outlet.user_id:
            return []

        from business_app.services.sales.outlet_service import OutletService

        rows = (
            db.session.query(func.max(OrderStatusHistory.changed_at))
            .join(Order, Order.id == OrderStatusHistory.order_id)
            .filter(
                OutletService.order_scope(outlet),
                Order.status == OrderStatus.DELIVERED,
                OrderStatusHistory.new_status == OrderStatus.DELIVERED,
            )
            .group_by(OrderStatusHistory.order_id)
            .order_by(func.max(OrderStatusHistory.changed_at).desc())
            .limit(limit)
            .all()
        )
        return [_aware(row[0]) for row in rows]

    @staticmethod
    def _recent_checks(
        outlet: Outlet,
        product: Product,
        *,
        limit: int,
        current_visit_id: Optional[int] = None,
        started_before: Optional[datetime] = None,
    ):
        """(VisitStockCheck, Visit) pairs for this outlet x product, newest visit first.

        A row counts when its visit is COMPLETED — the same answer
        `VisitService.previous_stock_rows` gives to "which is the previous check here", so the
        pre-fill the agent sees and the rate the money reads cannot drift apart — or when it
        belongs to `current_visit_id`, the caller's own visit, which is still in progress
        precisely because the caller is inside it.

        Both exclusions are load-bearing. An ABANDONED visit's partial count is never deleted
        (`abandon` only flips the status) and the hourly sweep manufactures them, so it used to
        become "the latest check". And the one-open-visit unique is per AGENT, so two agents can
        stand in one shop at once: without a notion of whose row this is, agent A's count paired
        with agent B's half-finished one and published a fabricated rate.

        `started_before` narrows the same ordering to visits that began at or before an instant,
        which is how `rate_per_day` asks the DATABASE for "the newest count at least a day older
        than the latest one" instead of scanning a fixed number of rows for it. Compare it
        against the RAW column value the caller read back, never an `_aware()` normalisation:
        SQLite stores these naive, and binding an aware datetime against a naive column compares
        two different strings.

        Private, and used only by `latest_stock_check` and `rate_per_day` — one ordering, one
        answer.
        """
        visit_is_countable = Visit.status == "completed"
        if current_visit_id is not None:
            visit_is_countable = or_(visit_is_countable, Visit.id == current_visit_id)
        query = (
            db.session.query(VisitStockCheck, Visit)
            .join(Visit, Visit.id == VisitStockCheck.visit_id)
            .filter(
                Visit.outlet_id == outlet.id,
                VisitStockCheck.product_id == product.id,
                visit_is_countable,
            )
        )
        if started_before is not None:
            query = query.filter(Visit.started_at <= started_before)
        return query.order_by(Visit.started_at.desc(), Visit.id.desc()).limit(limit).all()

    @staticmethod
    def latest_stock_check(
        outlet: Outlet, product: Product, *, current_visit_id: Optional[int] = None
    ) -> Optional[VisitStockCheck]:
        """The newest stock-check row for this outlet x product, newest VISIT first.

        Published as one accessor because more than one rule needs it — the stock-out
        projection below and Task 6's outlet card (`suggested_now`) — and "which check
        is the newest" must not be answered twice. Ordered by `Visit.started_at` (the
        field event), not by row id, because a re-submitted stock check overwrites in
        place. `row.visit` is the `Visit.stock_checks` backref, so callers that need
        the instant do not re-join.
        """
        rows = ReplenishmentService._recent_checks(outlet, product, limit=1, current_visit_id=current_visit_id)
        return rows[0][0] if rows else None

    @staticmethod
    def rate_per_day(
        outlet: Outlet, product: Product, *, now: datetime, current_visit_id: Optional[int] = None
    ) -> Tuple[Optional[Decimal], str]:
        """Units consumed per day, and where the number came from.

        (1) two stock checks: (prev.on_hand + delivered_between - now.on_hand) / days,
        clamped at zero; (2) delivered history over SALES_RATE_HISTORY_DAYS;
        (3) nothing. This is how summer and winter differ per outlet without a
        calendar table.
        """
        # The "previous" check has to be at least a DAY older than the latest, and a re-visit the
        # same afternoon is not it: `days` was clamped up to 1, so two counts hours apart divided
        # a consumption of ~0 by a whole day and published a rate of 0 — which is not "unknown"
        # (`suggested_qty` turns it into 0 and `predicted_stockout_at` refuses to project), so the
        # D7 rule went silent on exactly the outlets an agent had just visited twice. That row is
        # now SELECTED by the predicate rather than looked for inside a fixed window of rows:
        # `started_at <= latest − 1 day` is the same test `days >= 1` performed, and with three
        # rows fetched a third same-day re-visit pushed the real previous count out of the window.
        latest = ReplenishmentService._recent_checks(outlet, product, limit=1, current_visit_id=current_visit_id)
        if latest:
            latest_check, latest_visit = latest[0]
            previous = ReplenishmentService._recent_checks(
                outlet,
                product,
                limit=1,
                current_visit_id=current_visit_id,
                started_before=latest_visit.started_at - timedelta(days=1),
            )
            if previous:
                previous_check, previous_visit = previous[0]
                days = (_aware(latest_visit.started_at) - _aware(previous_visit.started_at)).days
                delivered = ReplenishmentService.delivered_qty(
                    outlet,
                    product,
                    since=previous_visit.started_at,
                    until=latest_visit.started_at,
                )
                consumed = Decimal(previous_check.on_hand_qty + delivered - latest_check.on_hand_qty)
                if consumed < 0:
                    consumed = Decimal(0)
                rate = (consumed / Decimal(days)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
                return rate, "stock_checks"

        history_days = int(current_app.config["SALES_RATE_HISTORY_DAYS"])
        window_start = _aware(now) - timedelta(days=history_days)
        delivered = ReplenishmentService.delivered_qty(outlet, product, since=window_start, until=now)
        if delivered > 0:
            rate = (Decimal(delivered) / Decimal(history_days)).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
            return rate, "orders"

        return None, "none"

    @staticmethod
    def suggested_qty(
        product: Product,
        rate: Optional[Decimal],
        on_hand: int,
        cadence: int,
        *,
        last_qty: Optional[int],
    ) -> Optional[int]:
        """D15: cover the cadence plus the delivery lead, times the safety factor,
        minus what is already on the shelf. An unknown rate falls back to the last
        delivered quantity; a shelf that is already covered suggests nothing at all
        (the min-order floor applies to a real order, never resurrects a zero).

        Bounded by `effective_line_qty_max()` — the SAME number that bounds a line the order
        door will accept (`VisitService._assert_line_quantities`), which is the smaller of the
        counting ceiling `SALES_STOCK_QTY_MAX` and the one `create_order` really enforces,
        `MAX_QUANTITY_PER_ITEM` (M30, ruling 71). A second ceiling here would be a second
        opinion about the same pallet. The fallback is bounded too: a pre-fill the order door
        would refuse is a dead end on the confirm screen, not a suggestion.

        Which is also why a purchase minimum ABOVE that ceiling suggests nothing (M02):
        `min_order_quantity` is bounded from below only (the admin write enforces `>= 1`), so
        a floor over the ceiling leaves the two line guards unsatisfiable together — the clamp
        would hand back `qty_max` and `VisitService._assert_line_quantities`, with
        `create_order`'s own minimum behind it, would refuse it for being under the floor. The
        floor is not bent to reach an orderable number: `create_order` applies it to every
        channel, so bending it here would only move the refusal one screen later.
        """
        qty_max = effective_line_qty_max()
        if int(product.min_order_quantity or 1) > qty_max:
            return None
        if rate is None:
            return min(int(last_qty), qty_max) if last_qty is not None else None

        cover_days = int(cadence) + int(current_app.config["SALES_DELIVERY_LEAD_DAYS"])
        factor = Decimal(str(current_app.config["SALES_SUGGEST_SAFETY_FACTOR"]))
        target = Decimal(rate) * Decimal(cover_days) * factor - Decimal(int(on_hand))
        quantity = int(target.to_integral_value(rounding=ROUND_CEILING))
        if quantity <= 0:
            return 0
        return min(max(quantity, int(product.min_order_quantity or 1)), qty_max)

    @staticmethod
    def predicted_stockout_at(outlet: Outlet, product: Product, *, now: datetime) -> Optional[datetime]:
        """When the shelf runs dry: last count plus anything delivered since, at
        the current rate. No rate (or a rate of zero) means no prediction.
        """
        check = ReplenishmentService.latest_stock_check(outlet, product)
        if check is None:
            return None

        rate, _source = ReplenishmentService.rate_per_day(outlet, product, now=now)
        if not rate or rate <= 0:
            return None

        checked_at = _aware(check.visit.started_at)
        delivered = ReplenishmentService.delivered_qty(outlet, product, since=checked_at)
        incoming = ReplenishmentService.incoming_qty(outlet, product, now=now)
        days_of_cover = Decimal(check.on_hand_qty + delivered + incoming) / rate
        return checked_at + timedelta(days=float(days_of_cover))

    @staticmethod
    def _agent_date_utc(day: date) -> datetime:
        local_tz = ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", DISPLAY_TIMEZONE))
        local = datetime.combine(day, time(hour=AGENT_VISIT_LOCAL_HOUR)).replace(tzinfo=local_tz)
        return local.astimezone(timezone.utc)

    @staticmethod
    def next_visit_due_at(outlet: Outlet, *, now: datetime, product=_CATALOGUE_UNSET) -> Optional[datetime]:
        """D7: the earliest of the agent's own date, the predicted stock-out minus
        the delivery lead, and the last visit plus the class cadence — never earlier
        than the visit that produced them.

        `product` is the primary returnable SKU, looked up here unless a caller already
        holds it. The nightly recompute is the one caller that does: the lookup is an
        estate-wide product query and the answer is the same for every outlet in the run.
        """
        moment = _aware(now)
        candidates = []

        if outlet.agent_next_visit_at:
            candidates.append(ReplenishmentService._agent_date_utc(outlet.agent_next_visit_at))

        if product is _CATALOGUE_UNSET:
            product = ReplenishmentService.primary_returnable_product()
        if product is not None:
            stockout = ReplenishmentService.predicted_stockout_at(outlet, product, now=moment)
            if stockout is not None:
                lead_days = int(current_app.config["SALES_DELIVERY_LEAD_DAYS"])
                candidates.append(stockout - timedelta(days=lead_days))

        last_visit_at = _aware(outlet.last_visit_at)
        if last_visit_at is not None:
            candidates.append(last_visit_at + timedelta(days=ReplenishmentService.cadence_days(outlet)))
            # The D7 floor (ruling 33). A visit answers everything the outlet knew
            # before it, so a candidate that predates it is history, not a plan: a
            # stock-out projected off a three-week-old count lands weeks in the past,
            # and publishing it would make the outlet read as overdue the instant its
            # visit closed. Such a candidate is DROPPED rather than clamped to
            # `last_visit_at` — clamping would answer "due right now" and drown out
            # the date the agent actually chose at close. In practice only
            # `predicted_stockout_at - lead` can trip this: `agent_next_visit_at` is
            # picked at close, and `last_visit_at + cadence` is later by construction
            # (`cadence_days` never returns less than 1), which is also why the list
            # can never be emptied here.
            candidates = [candidate for candidate in candidates if candidate >= last_visit_at]

        if candidates:
            return min(candidates)
        if outlet.stage in DUE_WHEN_NEVER_VISITED_STAGES:
            return moment
        return None

    @staticmethod
    def recompute_outlet(outlet: Outlet, *, now: Optional[datetime] = None, product=_CATALOGUE_UNSET) -> None:
        """Publish the due date onto the row. The caller owns the commit."""
        moment = _aware(now) if now is not None else datetime.now(timezone.utc)
        outlet.next_visit_due_at = ReplenishmentService.next_visit_due_at(outlet, now=moment, product=product)

    @staticmethod
    def recompute_all(now: Optional[datetime] = None) -> int:
        """The 01:00 job: republish `next_visit_due_at` for every outlet outside
        `DUE_DATE_FROZEN_STAGES`.

        The catalogue is read ONCE for the whole run and handed to each outlet (L30);
        everything else per outlet is the existing D7 rule, unchanged, so the job and a visit
        close cannot publish different dates for the same shop.

        One commit at the end: the job is the transaction.
        """
        moment = _aware(now) if now is not None else datetime.now(timezone.utc)
        product = ReplenishmentService.primary_returnable_product()
        outlets = Outlet.query.filter(Outlet.stage.notin_(DUE_DATE_FROZEN_STAGES)).order_by(Outlet.id.asc()).all()
        for outlet in outlets:
            ReplenishmentService.recompute_outlet(outlet, now=moment, product=product)
        db.session.commit()
        return len(outlets)

    @staticmethod
    def local_day_end_utc(now: Optional[datetime] = None) -> datetime:
        """End of the local business day — the "due today" cut-off for the due list.

        Derived from `local_windows.local_day_bounds`, the same local-midnight
        conversion `_driver_day_start_utc` performs, so the due list and every
        phase-3 KPI window answer to ONE calendar. Keeping a second expression is
        how a visit at 20:00 Tashkent lands on today's due list and yesterday's
        metrics row. The answer is unchanged: `tests/unit/test_local_windows.py::
        test_the_due_lists_day_boundary_is_this_modules_boundary` freezes both
        module clocks and asserts the two definitions still agree.
        """
        return local_day_bounds(local_date(now))[1]
