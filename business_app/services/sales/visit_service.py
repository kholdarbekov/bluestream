"""Visits: the agent's in-store loop — start, check-in, stock check, order, close.

One open visit per agent. The partial unique index `uq_visits_one_open_per_agent` is the
real guard; this service checks BEFORE the insert so the bot gets the OPEN visit's id back
in a 409 to offer Resume/Abandon, and catches the index's IntegrityError AFTER it so a
double tap that slipped between the check and the insert answers the same 409 rather than
a 500. Both arms raise `_already_open`, so there is one payload shape either way.

`current_step` is the SERVER's resume point. The staff bot never remembers where it
was: it asks `GET /visits/current` and renders whatever step the backend names. That
is what makes a menu tap, a bot restart or a dead battery lose nothing.

Ownership is never re-decided here. `OutletService.get_for_agent` is the one answer to
"may this agent touch this outlet" and `get_owned` is the one answer to "is this the
agent's own visit" — a second copy of either would be a second, drifting rule.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.sales import BUSINESS_OUTLET_TYPES, Outlet
from business_app.models.sales_visits import (
    NO_ORDER_REASONS,
    PHOTO_KINDS,
    VISIT_OUTCOMES,
    Visit,
    VisitPhoto,
    VisitStockCheck,
)
from business_app.serializers.sales_serializers import serialize_order_brief, serialize_stock_check
from business_app.services.order_service import OrderService
from business_app.services.sales.agent_order_confirmation_service import AgentOrderConfirmationService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.replenishment_service import ReplenishmentService, effective_line_qty_max
from business_app.services.staff_service import StaffService
from business_app.services.telegram_file_proxy import TelegramFileProxy
from business_app.utils.constants import MAX_PAGE_SIZE
from business_app.utils.delivery_window import local_now, parse_and_validate_schedule
from business_app.utils.exceptions import (
    AttachmentUnavailableError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from business_app.utils.helpers import calculate_distance
from business_app.utils.local_windows import window_bounds
from business_app.utils.telegram_tokens import get_staff_bot_token
from business_app.utils.timezone_utils import ensure_utc
from shared.staff_constants import STAFF_ACTIONS

# The only two rails an agent may take at the door. ONE definition, read by the menu the bot
# renders AND by the guard that accepts an order — a menu that offers what the guard refuses
# (or hides what it accepts) is the same rule expressed twice.
AGENT_PAYMENT_METHODS = ("cash", "business_account")

# visit_photos.telegram_file_id is VARCHAR(255).
TELEGRAM_FILE_ID_MAX = 255
# hashlib's `hexdigest()` shape: what the bot sends, nothing looser.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# The one value `GET /admin/sales/visits?photo=` accepts.
PHOTO_FILTER_MISSING = "missing"


def _int_or_none(raw: Any) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _hhmm_or_none(value: Optional[time]) -> Optional[str]:
    """Render an outlet's stored window edge for `parse_and_validate_schedule`.

    That helper parses "HH:MM" STRINGS; handing it the `time` off the outlet row reaches
    `parse_window_time`, which calls `.strip()` and turns a good window into
    "Invalid delivery schedule".
    """
    return value.strftime("%H:%M") if value is not None else None


class VisitService:
    # ------------------------------------------------------------------ lifecycle
    @staticmethod
    def _already_open(open_visit: Visit) -> ConflictError:
        """The ONE shape of SALES_VISIT_ALREADY_OPEN.

        `start` can refuse from two places — the pre-check and the unique index — and the bot
        builds its Resume/Abandon offer straight from `details`. Two hand-written copies is how
        one of those arms ends up without the `outlet_id` the Resume button needs.
        """
        return ConflictError(
            "A visit is already open",
            details={"visit_id": open_visit.id, "outlet_id": open_visit.outlet_id},
            error_code="SALES_VISIT_ALREADY_OPEN",
        )

    @staticmethod
    def _not_open(visit: Visit) -> ConflictError:
        """The ONE shape of SALES_VISIT_NOT_OPEN, raised from both places that can see it:
        the route's ownership gate and the re-read under `place_order`'s lock.

        Carries the visit the way `_already_open` does. A 409 whose body says only "Visit
        is not open" leaves the bot's dead-end alert and the operator's log guessing which
        row ended and whether the agent closed it or the hourly sweep did.
        """
        return ConflictError(
            "Visit is not open",
            details={"visit_id": visit.id, "status": visit.status},
            error_code="SALES_VISIT_NOT_OPEN",
        )

    @staticmethod
    def start(agent_user_id: int, outlet_id: int) -> Visit:
        outlet = OutletService.get_for_agent(agent_user_id, outlet_id)
        open_visit = VisitService.current(agent_user_id)
        if open_visit is not None:
            raise VisitService._already_open(open_visit)
        # `planned` is decided once, HERE, against the same local-day boundary the due list
        # sorts on — otherwise a visit could be "unplanned" on a card the agent reached from
        # the due list. Never re-derived by the bot.
        due_at = ensure_utc(outlet.next_visit_due_at) if outlet.next_visit_due_at is not None else None
        planned = due_at is not None and due_at <= ReplenishmentService.local_day_end_utc()
        visit = Visit(
            outlet_id=outlet.id,
            agent_user_id=agent_user_id,
            status="in_progress",
            planned=planned,
            current_step="checkin",
            started_at=datetime.now(UTC),
        )
        db.session.add(visit)
        try:
            db.session.commit()
        except IntegrityError:
            # The pre-check and this INSERT are not one atomic unit, so an agent double-tapping
            # *Start visit* can get two requests past the check and only `uq_visits_one_open_per_
            # agent` decides. Without this arm the loser gets a 500 and the bot shows "Something
            # went wrong" to an agent whose visit is in fact open; with it, both taps get the
            # identical 409 and the second one offers Resume.
            db.session.rollback()
            open_visit = VisitService.current(agent_user_id)
            if open_visit is None:
                # Some OTHER constraint failed (a stale outlet FK, say). Not ours to reshape.
                raise
            raise VisitService._already_open(open_visit) from None
        StaffService._log_activity(
            user_id=agent_user_id,
            action=STAFF_ACTIONS["VISIT_STARTED"],
            entity_type="visit",
            entity_id=visit.id,
            metadata_={"outlet_id": outlet.id, "planned": planned, "stage": outlet.stage},
        )
        return visit

    @staticmethod
    def current(agent_user_id: int) -> Optional[Visit]:
        return (
            Visit.query.filter_by(agent_user_id=agent_user_id, status="in_progress")
            .order_by(Visit.started_at.desc(), Visit.id.desc())
            .first()
        )

    @staticmethod
    def open_visit_ids_by_outlet(agent_user_id: int, *, open_visit: Optional[Visit] = None) -> Dict[int, int]:
        """``{outlet_id: visit_id}`` for the agent's open visit — at most one entry.

        The list route and `OutletService.card` both have to answer "is the open visit THIS
        outlet's?", and two copies of that comparison is how a card offers *Start visit* while the
        list next to it offers *Resume* on a different shop. One lookup, one mapping, both
        surfaces read it.

        `open_visit` is that visit when the caller has ALREADY resolved it — `GET
        /visits/current` loads it to render the screen. The MAPPING is still built here, so
        passing it drops the second identical query without handing the caller a second
        expression of the rule.
        """
        visit = open_visit if open_visit is not None else VisitService.current(agent_user_id)
        return {visit.outlet_id: visit.id} if visit is not None else {}

    @staticmethod
    def get_owned(visit_id: int, agent_user_id: int, *, must_be_open: bool = True) -> Visit:
        visit = Visit.query.get(visit_id)
        if visit is None:
            raise NotFoundError("Visit not found", error_code="SALES_VISIT_NOT_FOUND")
        if visit.agent_user_id != agent_user_id:
            raise ForbiddenError("Visit belongs to another agent", error_code="SALES_VISIT_NOT_OWNED")
        if must_be_open and visit.status != "in_progress":
            raise VisitService._not_open(visit)
        return visit

    @staticmethod
    def previous_stock_rows(visit: Visit) -> List[VisitStockCheck]:
        """The stock-check rows of this outlet's most recent COMPLETED visit (ruling 29).

        Spec *Visit* step 2 opens the shelf screen "pre-filled from the previous stock
        check", so the agent corrects numbers instead of typing them. Which visit is the
        previous one is answered HERE and published as
        `serialize_visit(...)["previous_stock"]` by the two screen-opening routes — the
        bot never runs this query.

        It selects the same COMPLETED-visit rows the D7 rate rule counts
        (`ReplenishmentService._recent_checks`), with one deliberate difference: the rate
        rule ALSO counts `current_visit_id`, the caller's own open visit, while the
        pre-fill must exclude it — the screen is being drawn for that visit, so its rows
        are what the agent is about to overwrite, never "the previous count".
        """
        previous = (
            Visit.query.filter(
                Visit.outlet_id == visit.outlet_id,
                Visit.id != visit.id,
                Visit.status == "completed",
            )
            .order_by(Visit.started_at.desc(), Visit.id.desc())
            .first()
        )
        return list(previous.stock_checks) if previous is not None else []

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _require_step(visit: Visit, *allowed: str) -> None:
        """The step machine lives on the server (D15). The bot renders `current_step`; it
        never decides whether a step is legal, so a stale keyboard cannot skip check-in."""
        if visit.current_step not in allowed:
            raise ValidationError(
                f"Visit is at step '{visit.current_step}'",
                details={"current_step": visit.current_step, "expected": list(allowed)},
                error_code="SALES_VISIT_STEP_INVALID",
            )

    @staticmethod
    def checkin(
        visit: Visit,
        *,
        latitude: Optional[float],
        longitude: Optional[float],
        horizontal_accuracy: Optional[float],
        skipped: bool,
    ) -> Visit:
        VisitService._require_step(visit, "checkin")
        outlet = visit.outlet
        visit.checkin_at = datetime.now(UTC)
        visit.checkin_skipped = bool(skipped)
        if skipped or latitude is None or longitude is None:
            visit.checkin_latitude = None
            visit.checkin_longitude = None
            visit.checkin_accuracy_m = None
            visit.distance_m = None
            visit.in_radius = None
        else:
            visit.checkin_latitude = float(latitude)
            visit.checkin_longitude = float(longitude)
            visit.checkin_accuracy_m = float(horizontal_accuracy) if horizontal_accuracy is not None else None
            if outlet.latitude is not None and outlet.longitude is not None:
                km = calculate_distance(float(latitude), float(longitude), outlet.latitude, outlet.longitude)
                visit.distance_m = km * 1000
                visit.in_radius = visit.distance_m <= float(current_app.config["SALES_GEOFENCE_RADIUS_M"])
            else:
                # A prospect can be pinned later; presence is still recorded, just not measured.
                visit.distance_m = None
                visit.in_radius = None
        # D16: a far or missing fix is DATA, never a refusal — the visit always advances.
        visit.current_step = "stock"
        db.session.commit()
        return visit

    @staticmethod
    def _qty(raw: Any, qty_max: int, field: str) -> int:
        try:
            value = int(raw if raw is not None else 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"{field} must be a whole number",
                details={"field": field, "value": raw},
                error_code="SALES_STOCK_QTY_INVALID",
            ) from exc
        if value < 0 or value > qty_max:
            raise ValidationError(
                f"{field} must be between 0 and {qty_max}",
                details={"field": field, "value": value, "max": qty_max},
                error_code="SALES_STOCK_QTY_INVALID",
            )
        return value

    @staticmethod
    def stock_check(visit: Visit, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # "order" is allowed as well as "stock": the order screen has a Back button, and a
        # re-submission overwrites (uq_visit_stock_checks_visit_product) rather than duplicating.
        VisitService._require_step(visit, "stock", "order")
        outlet = visit.outlet
        qty_max = int(current_app.config["SALES_STOCK_QTY_MAX"])
        allowed = {product.id: product for product in ReplenishmentService.stock_check_products()}
        cadence = ReplenishmentService.cadence_days(outlet)
        now = datetime.now(UTC)

        # Pass 1 — validate the WHOLE submission before touching the session. One bad line must
        # not leave earlier lines pending for the next commit (_log_activity commits) to adopt.
        cleaned = []
        for item in items:
            product = allowed.get(_int_or_none(item.get("product_id")))
            if product is None:
                raise ValidationError(
                    "Product is not on the stock-check list",
                    details={"product_id": item.get("product_id")},
                    error_code="SALES_STOCK_PRODUCT_INVALID",
                )
            empties = None
            if item.get("empties_qty") is not None:
                empties = VisitService._qty(item.get("empties_qty"), qty_max, "empties_qty")
            cleaned.append(
                {
                    "product": product,
                    "on_hand_qty": VisitService._qty(item.get("on_hand_qty"), qty_max, "on_hand_qty"),
                    "empties_qty": empties,
                    "is_sold_out": bool(item.get("is_sold_out")),
                    "is_low": bool(item.get("is_low")),
                }
            )

        # Pass 2 — upsert the rows.
        pending = []
        for entry in cleaned:
            product = entry["product"]
            row = VisitStockCheck.query.filter_by(visit_id=visit.id, product_id=product.id).first()
            if row is None:
                row = VisitStockCheck(visit_id=visit.id, product_id=product.id)
                db.session.add(row)
            row.on_hand_qty = entry["on_hand_qty"]
            row.empties_qty = entry["empties_qty"]
            row.is_sold_out = entry["is_sold_out"]
            row.is_low = entry["is_low"]
            pending.append((row, product))

        # Pass 3 — rates AFTER the flush. `rate_per_day` reads the most recent stock checks for
        # this outlet x product and THIS one is the newer half of that pair, so the rows must be
        # visible to that query. Flushed, not committed: the submission stays one unit of work.
        # `current_visit_id` is what makes them visible at all: the rule counts COMPLETED visits
        # plus the caller's own, so this visit's rows count here and another agent's open visit
        # at the same shop never does.
        db.session.flush()
        rows = []
        for row, product in pending:
            rate, source = ReplenishmentService.rate_per_day(outlet, product, now=now, current_visit_id=visit.id)
            last_qty = ReplenishmentService.last_delivered_qty(outlet, product)
            row.rate_per_day = rate
            row.rate_source = source
            row.suggested_qty = ReplenishmentService.suggested_qty(
                product, rate, row.on_hand_qty, cadence, last_qty=last_qty
            )
            rows.append((row, last_qty))

        visit.current_step = "order"
        db.session.commit()
        # `serialize_stock_check` stays the one shaper of a stock-check row; the order screen
        # additionally needs "what did they order last time", which is not a column on the row.
        return [{**serialize_stock_check(row), "last_order_qty": last_qty} for row, last_qty in rows]

    # ------------------------------------------------------------------ order on behalf
    @staticmethod
    def _order_exists(visit: Visit) -> ConflictError:
        """The ONE shape of SALES_VISIT_ORDER_EXISTS.

        `place_order` refuses from two places — the read under the lock and the
        `uq_orders_visit_id` violation inside `create_order` — and `details["order"]` is the SAME
        dict `serialize_visit` publishes (ruling 25), because the bot's double-tap copy prints the
        existing order's NUMBER, which an id alone cannot give it.
        """
        return ConflictError(
            "This visit already has an order",
            details={
                "visit_id": visit.id,
                "order_id": visit.order.id,
                "order": serialize_order_brief(visit.order),
            },
            error_code="SALES_VISIT_ORDER_EXISTS",
        )

    @staticmethod
    def _assert_orderable(outlet: Outlet) -> None:
        """An outlet can only buy once approval gave it a customer account AND an address.

        Both, not either: `create_order` needs a `delivery_address_id` it can put on the order,
        and a prospect has neither. One guard, so the menu, the quote and the order agree about
        what "active" means.
        """
        if not outlet.user_id or not outlet.address_id:
            raise ValidationError(
                "Outlet has no customer account yet",
                details={"outlet_id": outlet.id, "stage": outlet.stage},
                error_code="SALES_OUTLET_NOT_ACTIVE",
            )

    @staticmethod
    def _assert_line_quantities(items: List[Dict[str, Any]]) -> None:
        """The per-product purchase minimum AND the one quantity ceiling, refused where the
        agent can still change the number.

        The floor lives on `products.min_order_quantity` — read here, never re-stated — and is
        published to every surface already (`serialize_stock_check_product`). Its only guard was
        inside `create_order`, as a code-less 400 raised AFTER the agent had read a total out to
        the shopkeeper, which the bot can only render as "Something went wrong". Checked on the
        quote AND on the order, so the same basket gets the same answer on both doors, and the
        refusal names the product, the bound and what was asked for. `create_order`'s own branch
        stays the last backstop for every other channel.

        The ceiling is `effective_line_qty_max()` — the smaller of `SALES_STOCK_QTY_MAX` (what
        an agent may COUNT, `_qty`) and `MAX_QUANTITY_PER_ITEM` (what
        `OrderService._process_order_items` really enforces), read by
        `ReplenishmentService.suggested_qty` from the same helper so a suggestion can never
        exceed what this door accepts (M30, ruling 71).

        The ceiling was not absent before, it was unreachable and unnamed: `OrderItemPayload`
        asks only for `ge=1`, so the first thing to refuse a fat finger, a fabricated rate or a
        replayed basket was `create_order`'s own code-less 400 — raised at the bottom, after the
        agent had read a total out to the shopkeeper, and rendered by the bot as "Something went
        wrong". Refusing here names the product, the bound and what was asked for, on the screen
        that can still change the number.
        """
        qty_max = effective_line_qty_max()
        for item in items:
            product = Product.query.get(int(item["product_id"]))
            if product is None:
                # "Which products exist" is `create_order`'s question (NotFoundError), not a
                # second answer here.
                continue
            minimum = int(product.min_order_quantity or 1)
            quantity = int(item["quantity"])
            if quantity < minimum:
                raise ValidationError(
                    f"{product.name}: minimum order quantity is {minimum} (you ordered {quantity})",
                    details={
                        "product_id": product.id,
                        "product_name": product.name,
                        "min_order_quantity": minimum,
                        "quantity": quantity,
                    },
                    error_code="SALES_ORDER_MIN_QTY",
                )
            if quantity > qty_max:
                raise ValidationError(
                    f"{product.name}: at most {qty_max} units per line (you ordered {quantity})",
                    details={
                        "product_id": product.id,
                        "product_name": product.name,
                        "quantity": quantity,
                        "max_quantity": qty_max,
                    },
                    error_code="SALES_ORDER_QTY_INVALID",
                )

    @staticmethod
    def payment_methods(outlet: Outlet) -> List[Dict[str, Any]]:
        """The rails this store may pay with, narrowed to the ones an agent may take.

        `StaffService.get_client_payment_methods` already answers "what may this customer pay
        with" — COD cap (person AND place arms, hence the address), entity-subtype gate and
        business-account coverage included. The agent menu is a SUBSET of that answer, never a
        second computation of it.
        """
        VisitService._assert_orderable(outlet)
        available = StaffService.get_client_payment_methods(
            outlet.user_id,
            delivery_address_id=outlet.address_id,
        ).get("available_methods", [])
        return [method for method in available if method.get("method") in AGENT_PAYMENT_METHODS]

    @staticmethod
    def _rail(raw: Any) -> str:
        """The ONE gate on which rail an agent may take, applied by the quote and the order.

        A menu that offers what the guard refuses is the same rule expressed twice, and a quote
        priced on a rail the order would refuse is worse still: the agent reads out a total for
        a payment method the shop can never use.
        """
        rail = (raw or "").strip().lower()
        if rail not in AGENT_PAYMENT_METHODS:
            raise ValidationError(
                "An agent may take cash or the business account only",
                details={"payment_method": raw},
                error_code="SALES_PAYMENT_METHOD_INVALID",
            )
        return rail

    @staticmethod
    def order_estimate(
        outlet: Outlet, items: List[Dict[str, Any]], payment_method: Optional[str] = None
    ) -> Dict[str, Any]:
        """READ-ONLY quote for the basket the agent is building at the counter.

        The operator estimator replays exactly the pricing `create_order` charges, so the figure
        the agent reads out and the figure the store is charged cannot diverge. Writes nothing.

        The RAIL is part of that price: the loyalty tier discount is a COD-only benefit that
        `create_order` quotes live, so a quote that did not know the rail omitted it and the
        confirm screen's total was not the total charged. Optional because the bot may quote
        before the rail step; validated by the same gate the order uses when it is supplied.
        """
        VisitService._assert_orderable(outlet)
        VisitService._assert_line_quantities(items)
        rail = VisitService._rail(payment_method) if payment_method is not None else None
        return StaffService.estimate_phone_order(outlet.user_id, {"items": items}, payment_method=rail)

    @staticmethod
    def place_order(visit: Visit, payload: Dict[str, Any], agent_user_id: int) -> Tuple[Order, str]:
        """Place the store's order through the ONE order-creation path (D12).

        `visit_id` is the idempotency key: one visit, at most one order, so a double tap on
        "Order suggested" buys the store one load of water and not two. Everything about the
        order — pricing, the COD cap, inventory reservation, the payment row, the instant-COD
        confirmation — is `create_order`'s, unchanged.

        Returns `(order, confirmation_state)`; the state is
        `AgentOrderConfirmationService.open_or_confirm`'s single published answer.
        """
        # Lock the visit row BEFORE reading `visit.order`. `Order.visit_id` is what makes the
        # guard below true, and a concurrent `place_order` that has already committed its order is
        # invisible to an unlocked, session-cached `visit` — so two taps could both see `None` and
        # both buy the store a load of water. `populate_existing=True` is load-bearing for exactly
        # the reason `OrderScheduleService` documents at :188: `with_for_update` alone locks the
        # row but hands back the STALE instance already in the identity map, so the decision would
        # still be made on pre-lock data. The partial unique `uq_orders_visit_id` is the last-ditch
        # backstop; this lock is what turns the race into a clean 409.
        visit = db.session.get(Visit, visit.id, with_for_update=True, populate_existing=True)
        outlet = visit.outlet
        # The route's `get_owned` ran when the bot ASKED for the screen; `abandon_stale` sweeps
        # hourly and can have closed the visit since. An order on a closed visit is unreachable
        # by `close`, so the outlet's due date would never be recomputed and the visit would
        # carry no outcome for the KPIs.
        if visit.status != "in_progress":
            raise VisitService._not_open(visit)
        if visit.order is not None:
            raise VisitService._order_exists(visit)
        # AFTER the order guard, deliberately: a visit that already bought water sits at step
        # "close", and a double tap on it must keep getting the 409 whose `details.order` the bot
        # prints the existing order NUMBER from — not a step error it would render as "wrong
        # step". Before it, this is the money step, and it was the only one of the three the
        # server did not own (M02).
        VisitService._require_step(visit, "order")
        VisitService._assert_orderable(outlet)

        payment_method = VisitService._rail(payload.get("payment_method"))

        items = [
            {"product_id": int(item["product_id"]), "quantity": int(item["quantity"])}
            for item in (payload.get("items") or [])
        ]
        # Before `create_order`, so a sub-minimum line is refused with the code the bot maps to a
        # "fix this line" screen rather than with create_order's code-less 400.
        VisitService._assert_line_quantities(items)

        # The schedule goes through the ONE validator every write path shares, so a sales order
        # can never carry a schedule the web checkout would have refused. `delivery_date` is
        # passed through unchanged — the "Tomorrow" default lives in the bot, not here. The clock
        # is read ONCE and injected, so the fallback below and the validator cannot disagree
        # about what "now" is.
        now_local = local_now()
        delivery_date, window_start, window_end, schedule_errors = parse_and_validate_schedule(
            payload.get("delivery_date"),
            payload.get("delivery_window_start"),
            payload.get("delivery_window_end"),
            now_local=now_local,
        )
        if schedule_errors:
            # Coded, unlike `create_order`'s own schedule 400: the bot has to say "that day does
            # not work" and re-open the day screen, and a code-less 400 reaches it as the generic
            # "Something went wrong" (L47(4)/L105). `validation_errors` still carries the
            # validator's own sentences for the logs and the admin surfaces.
            raise ValidationError(
                "; ".join(schedule_errors),
                validation_errors=schedule_errors,
                error_code="SALES_DELIVERY_DATE_INVALID",
            )

        if payload.get("delivery_window_start") is None and payload.get("delivery_window_end") is None:
            # The store's standing window is a default the agent never typed, so it may only ever
            # ADD a window — never cost the agent the order. Hence all-or-nothing (never one edge
            # of the outlet's window pinned to one edge of the payload's), and only when the SAME
            # validator accepts it for the chosen date. A 09:00-12:00 store ordering "Today" at
            # 14:00 therefore gets "anytime" rather than "delivery window has already passed for
            # today", which used to make the bot's Today button unusable there all afternoon.
            # Asking the validator IS the feasibility test — a second hand-written "is this window
            # still reachable" would be the same rule expressed twice. The date was accepted
            # above, so anything wrong here is the outlet's window and never the agent's date.
            # An explicit "" from the payload is the agent saying "anytime": not absence, so it
            # does not reach this branch.
            #
            # The date the window has to be reachable ON is the one the ORDER will carry, and an
            # undated order is TODAY's: `OrderScheduleService.is_awaiting_release` never holds
            # back an order with no `delivery_date`, so its delivery is created immediately.
            # Asking with `None` skipped the validator's "already passed for today" rule, which
            # is the only thing that makes this branch a feasibility test at all (M03).
            _, outlet_start, outlet_end, outlet_errors = parse_and_validate_schedule(
                delivery_date or now_local.date(),
                _hhmm_or_none(outlet.delivery_window_start),
                _hhmm_or_none(outlet.delivery_window_end),
                now_local=now_local,
            )
            if not outlet_errors:
                window_start, window_end = outlet_start, outlet_end

        order_service = OrderService()
        user, address = order_service.get_user_and_address_for_order(outlet.user_id, outlet.address_id)
        visit_id = visit.id
        try:
            order = order_service.create_order(
                user.id,
                {
                    "items": items,
                    "delivery_address": {
                        "delivery_address_id": address.id,
                        "street": address.street_address,
                        "latitude": address.latitude,
                        "longitude": address.longitude,
                    },
                    "payment_method": payment_method,
                    "delivery_date": delivery_date,
                    "delivery_window_start": window_start,
                    "delivery_window_end": window_end,
                    "delivery_notes": payload.get("delivery_notes"),
                    "order_source": "sales_agent",
                    "created_by_staff_id": agent_user_id,
                    # The idempotency key travels WITH the order so `uq_orders_visit_id` is
                    # evaluated inside `create_order`'s own transaction. Writing it afterwards
                    # put it in a later transaction — one that only began once `create_order`'s
                    # commit had already released the lock taken above — so the loser of a
                    # double tap had a real, paid-for order before anything refused it.
                    "visit_id": visit_id,
                },
            )
        except IntegrityError:
            # The index got there first: another tap's order already owns this visit. Mirrors
            # `start()`'s arm — the guard above and this one raise the identical 409, so the bot
            # prints the same "already ordered" copy whichever side of the race it lost on.
            db.session.rollback()
            visit = db.session.get(Visit, visit_id)
            if visit is None or visit.order is None:
                # Some OTHER constraint failed. Not ours to reshape.
                raise
            raise VisitService._order_exists(visit) from None

        # No `order.visit_id = visit.id` here: `create_order` wrote it, under the unique.
        # Both figures are stored per line so the safety factor can be tuned against reality and
        # suggested-vs-accepted can be reported without re-deriving either.
        accepted = {item["product_id"]: item["quantity"] for item in items}
        for row in visit.stock_checks:
            if row.product_id in accepted:
                row.accepted_qty = accepted[row.product_id]
        visit.current_step = "close"
        db.session.commit()

        state = AgentOrderConfirmationService.open_or_confirm(order, outlet, agent_user_id)
        StaffService._log_activity(
            user_id=agent_user_id,
            action=STAFF_ACTIONS["AGENT_ORDER_CREATED"],
            entity_type="order",
            entity_id=order.id,
            metadata_={
                "visit_id": visit.id,
                "outlet_id": outlet.id,
                "payment_method": payment_method,
                "confirmation_state": state,
            },
        )
        return order, state

    # ------------------------------------------------------------------ close
    @staticmethod
    def close(
        visit: Visit,
        *,
        outcome: Optional[str],
        no_order_reason: Optional[str],
        notes: Optional[str],
        next_visit_at: Optional[date],
        dm_present: Optional[bool],
        now: Optional[datetime] = None,
    ) -> Visit:
        outlet = visit.outlet
        order = visit.order
        if order is not None:
            # An order IS the outcome. Letting the bot send one would allow "no_order" on a
            # visit that booked 8 bottles, and every KPI downstream reads this column.
            outcome = "order_placed"
            no_order_reason = None
        else:
            if not outcome:
                raise ValidationError("An outcome is required", error_code="SALES_VISIT_OUTCOME_REQUIRED")
            if outcome not in VISIT_OUTCOMES:
                raise ValidationError(
                    f"Unknown outcome {outcome}",
                    details={"outcome": outcome},
                    error_code="SALES_VISIT_OUTCOME_INVALID",
                )
            if outcome == "no_order":
                if not no_order_reason:
                    raise ValidationError(
                        "A reason is required when no order was placed",
                        error_code="SALES_VISIT_OUTCOME_REQUIRED",
                    )
                if no_order_reason not in NO_ORDER_REASONS:
                    raise ValidationError(
                        f"Unknown no-order reason {no_order_reason}",
                        details={"no_order_reason": no_order_reason},
                        error_code="SALES_VISIT_OUTCOME_INVALID",
                    )
            else:
                no_order_reason = None

        # The sweep's moment when there is one, exactly as `abandon` takes it: `abandon_stale`
        # SELECTS on the instant it was handed and routes an ordered visit through HERE, and
        # this one read stamps `ended_at`, `outlet.last_visit_at` AND the D7 recompute below.
        # An agent's own close tap passes nothing and gets "now".
        now = now or datetime.now(UTC)
        visit.status = "completed"
        visit.ended_at = now
        visit.outcome = outcome
        visit.no_order_reason = no_order_reason
        visit.notes = (notes or "").strip() or None
        visit.next_visit_at = next_visit_at
        visit.dm_present = dm_present

        outlet.last_visit_at = now
        outlet.agent_next_visit_at = next_visit_at
        if order is not None and outlet.stage in ("at_risk", "dormant"):
            OutletService.transition(outlet, "active", visit.agent_user_id, reason_code="visit_order")
        # Recomputed AFTER last_visit_at is set, so the cadence leg of the D7 min() uses this
        # visit — a due date computed from the previous visit would fire immediately.
        ReplenishmentService.recompute_outlet(outlet, now=now)
        db.session.commit()
        StaffService._log_activity(
            user_id=visit.agent_user_id,
            action=STAFF_ACTIONS["VISIT_CLOSED"],
            entity_type="visit",
            entity_id=visit.id,
            metadata_={
                "outlet_id": outlet.id,
                "outcome": outcome,
                "no_order_reason": no_order_reason,
                "order_id": order.id if order is not None else None,
                "planned": bool(visit.planned),
            },
        )
        return visit

    @staticmethod
    def abandon(visit: Visit, actor_id: Optional[int], *, at: Optional[datetime] = None) -> Visit:
        # ONE expression of "an ordered visit is never abandoned" (ruling 74). The store has
        # already bought water on this visit, so there is nothing to abandon: abandoning it
        # would leave the visit with no outcome (every KPI downstream reads that column, and an
        # order with no `order_placed` is invisible to them) and would skip `close` -- the only
        # thing on the visit path that republishes the outlet's next-visit date after a
        # productive call. Both the agent's own tap and the hourly sweep land here, so neither
        # can answer differently. `close` forces `order_placed` itself, so no outcome is passed
        # in; the standing next-visit date is carried forward because nobody here has a new one
        # to offer, and passing None would erase the date the agent and the shopkeeper agreed
        # at the PREVIOUS close -- a candidate in the D7 `min()` (ruling 54).
        if visit.order is not None:
            return VisitService.close(
                visit,
                outcome=None,
                no_order_reason=None,
                notes=None,
                next_visit_at=visit.outlet.agent_next_visit_at,
                dm_present=None,
                now=at,
            )
        # `at` is the sweep's moment. `abandon_stale` SELECTS on the instant it was handed,
        # so it must STAMP with the same one — reading the clock again here ended a visit at
        # a time the selection never used. An agent's own tap passes nothing and gets "now".
        visit.status = "abandoned"
        visit.ended_at = at or datetime.now(UTC)
        db.session.commit()
        # `staff_activity_log.user_id` is NOT NULL, and the hourly sweep has no actor, so the
        # visit's own agent owns the line — the log answers "whose visit ended", not "who tapped".
        StaffService._log_activity(
            user_id=actor_id or visit.agent_user_id,
            action=STAFF_ACTIONS["VISIT_ABANDONED"],
            entity_type="visit",
            entity_id=visit.id,
            metadata_={"outlet_id": visit.outlet_id, "by_sweep": actor_id is None},
        )
        return visit

    @staticmethod
    def abandon_stale(*, now: Optional[datetime] = None) -> int:
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(hours=int(current_app.config["SALES_VISIT_AUTO_ABANDON_HOURS"]))
        stale = Visit.query.filter(Visit.status == "in_progress", Visit.started_at <= cutoff).all()
        for visit in stale:
            # No branch on `visit.order` here: `abandon` owns that rule for the sweep and for
            # the agent's own tap alike (ruling 74), and a second copy of it is exactly how the
            # two came to disagree.
            VisitService.abandon(visit, None, at=moment)
        return len(stale)

    # ------------------------------------------------------------------ admin list
    @staticmethod
    def list_for_admin(
        *,
        start_date: date,
        end_date: date,
        agent_user_id: Optional[int] = None,
        outlet_id: Optional[int] = None,
        outcome: Optional[str] = None,
        in_radius: Optional[bool] = None,
        photo: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Visit], int]:
        """Every visit STARTED inside a LOCAL-day window, newest first (R2/R11).

        `started_at` is the window column for every visit surface in phase 3: it is the only
        indexed instant on `visits`, it is the boundary `planned` was stamped against, and it is
        what the KPI card counts on. A list that windowed on `ended_at` would put a visit closed
        after midnight on a different day from the one its own plan row measures.

        `in_radius` is a TRI-STATE filter. `None` means "no filter"; the column itself is
        nullable, because a skipped check-in, a phone that sent no pin and an outlet with no pin
        all leave it NULL — so the FALSE branch is `is_(False)`, never `~Visit.in_radius`, which
        would publish every unmeasured visit as an out-of-range exception.

        `outcome` is validated against the model's own vocabulary and refused with the SAME code
        `close` raises, so a typo in a filter is a 400 the page can show rather than an empty
        table that reads as "this agent sold nothing".

        `now` is accepted for call-shape parity with the rest of the phase-3 read surface; both
        bounds here are absolute local dates, so nothing in this query reads the clock.
        """
        start_utc, end_utc = window_bounds(start_date, end_date)
        query = Visit.query.filter(Visit.started_at >= start_utc, Visit.started_at < end_utc)
        if agent_user_id is not None:
            query = query.filter(Visit.agent_user_id == agent_user_id)
        if outlet_id is not None:
            query = query.filter(Visit.outlet_id == outlet_id)
        if outcome:
            if outcome not in VISIT_OUTCOMES:
                raise ValidationError(
                    f"Unknown outcome {outcome}",
                    details={"outcome": outcome, "expected": list(VISIT_OUTCOMES)},
                    error_code="SALES_VISIT_OUTCOME_INVALID",
                )
            query = query.filter(Visit.outcome == outcome)
        if in_radius is not None:
            query = query.filter(Visit.in_radius.is_(bool(in_radius)))
        if photo is not None:
            if photo != PHOTO_FILTER_MISSING:
                raise ValidationError(
                    f"Unknown photo filter {photo}",
                    details={"photo": photo, "expected": [PHOTO_FILTER_MISSING]},
                    error_code="SALES_VISIT_PHOTO_FILTER_INVALID",
                )
            # D28: only a business outlet is ever asked for a photo, so a home visit without one
            # is missing nothing.
            query = query.join(Outlet, Outlet.id == Visit.outlet_id).filter(
                Outlet.outlet_type.in_(BUSINESS_OUTLET_TYPES),
                ~Visit.photos.any(),
            )
        # The five relationships `serialize_visit_admin_row` reads are all default-lazy, so a
        # page costs 1 + 5n SELECTs left alone -- ~501 at the `MAX_PAGE_SIZE` cap. Five eager
        # loads instead, the `AgentMetricsService` precedent. `selectinload` issues a separate
        # IN query per relationship rather than joining, so the ORDER BY below stays the ONLY
        # thing deciding row order and the paging contract is untouched.
        pagination = (
            query.options(
                selectinload(Visit.outlet),
                selectinload(Visit.agent),
                selectinload(Visit.order),
                selectinload(Visit.stock_checks),
                selectinload(Visit.photos),
            )
            .order_by(Visit.started_at.desc(), Visit.id.desc())
            .paginate(page=page, per_page=min(per_page, MAX_PAGE_SIZE), error_out=False)
        )
        return list(pagination.items), pagination.total

    # ------------------------------------------------------------------ photos
    @staticmethod
    def add_photo(
        visit: Visit,
        *,
        telegram_file_id: Optional[str],
        telegram_file_unique_id: Optional[str],
        sha256: Optional[str],
        kind: Optional[str],
        agent_user_id: int,
    ) -> VisitPhoto:
        """Record one visit photo by the staff bot's Telegram file id, and flag a repeat (D27).

        Nothing is downloaded or stored here. The bot hashed the bytes it fetched from Telegram and
        sends the digest; the picture stays on Telegram until an admin opens it (`stream_photo`).
        The DIGEST, not the file id, is what the duplicate rule compares: Telegram mints a new file
        id for every upload, so a re-sent gallery photo carries a new id and the same bytes.

        A duplicate is RECORDED, never refused: the agent's visit carries on and the
        supervisor's feed gets the row. It points at the EARLIEST match rather than the most
        recent, so a third copy references the original instead of chaining -- a chain makes
        one re-sent photo look like two distinct exceptions.

        The scope is per AGENT (R8). Two reps photographing the same chain storefront on the
        same morning is ordinary work; flagging it would bury the case this exists for.

        No `_require_step`: a photo is accepted at every step of the visit (spec *Visit*),
        and the route's `get_owned(..., must_be_open=True)` is the whole gate.
        """
        if kind not in PHOTO_KINDS:
            raise ValidationError(
                "Unsupported photo kind",
                details={"kind": kind, "expected": list(PHOTO_KINDS)},
                error_code="SALES_PHOTO_INVALID",
            )
        file_id = (telegram_file_id or "").strip()
        if not file_id or len(file_id) > TELEGRAM_FILE_ID_MAX:
            raise ValidationError("A Telegram file id is required", error_code="SALES_PHOTO_INVALID")
        digest = sha256 or ""
        if not _SHA256_HEX.fullmatch(digest):
            raise ValidationError("sha256 must be 64 lowercase hex characters", error_code="SALES_PHOTO_INVALID")
        original = (
            db.session.query(VisitPhoto.id)
            .join(Visit, Visit.id == VisitPhoto.visit_id)
            .filter(Visit.agent_user_id == agent_user_id, VisitPhoto.sha256 == digest)
            .order_by(VisitPhoto.id.asc())
            .first()
        )
        photo = VisitPhoto(
            visit_id=visit.id,
            kind=kind,
            telegram_file_id=file_id,
            sha256=digest,
            telegram_file_unique_id=(telegram_file_unique_id or "").strip()[:100] or None,
            received_at=datetime.now(UTC),
            duplicate_of_photo_id=original[0] if original is not None else None,
        )
        db.session.add(photo)
        db.session.commit()
        StaffService._log_activity(
            user_id=agent_user_id,
            action=STAFF_ACTIONS["VISIT_PHOTO_ADDED"],
            entity_type="visit",
            entity_id=visit.id,
            metadata_={
                "outlet_id": visit.outlet_id,
                "photo_id": photo.id,
                "kind": kind,
                "is_duplicate": photo.duplicate_of_photo_id is not None,
            },
        )
        return photo

    @staticmethod
    def list_outlet_photos(outlet_id: int, *, page: int, per_page: int) -> Tuple[List[VisitPhoto], int]:
        """Every photo taken at one outlet, across all its visits, newest first."""
        pagination = (
            VisitPhoto.query.join(Visit, Visit.id == VisitPhoto.visit_id)
            .filter(Visit.outlet_id == outlet_id)
            .options(selectinload(VisitPhoto.visit).selectinload(Visit.agent))
            .order_by(VisitPhoto.received_at.desc(), VisitPhoto.id.desc())
            .paginate(page=page, per_page=min(per_page, MAX_PAGE_SIZE), error_out=False)
        )
        return list(pagination.items), pagination.total

    @staticmethod
    def stream_photo(photo_id: int):
        """Stream one visit photo from Telegram through the STAFF bot (D27).

        The file id is read from the row, never from the request. A photo Telegram cannot serve --
        a dead file id, a replaced bot, no token, an outage -- is a 404 the admin UI draws as
        "Photo unavailable", never a 500.
        """
        photo = db.session.get(VisitPhoto, photo_id)
        if photo is None:
            raise NotFoundError("Photo not found", error_code="SALES_PHOTO_NOT_FOUND")
        proxy = TelegramFileProxy(get_staff_bot_token(), cache_namespace="sales_photo")
        try:
            return proxy.stream(
                photo.telegram_file_id,
                mime="image/jpeg",
                filename=f"visit_{photo.visit_id}_photo_{photo.id}.jpg",
            )
        except AttachmentUnavailableError as exc:
            # str(exc) is already scrubbed of the token by the proxy.
            current_app.logger.warning("Visit photo %s unavailable: %s", photo.id, exc)
            raise NotFoundError("Photo is no longer available", error_code="SALES_PHOTO_UNAVAILABLE") from exc
