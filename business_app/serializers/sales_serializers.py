"""Request models and dict serializers for the sales module (agents, outlets)."""

from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from business_app.utils.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from business_app.utils.local_windows import parse_date_range
from business_app.utils.timezone_utils import ensure_utc


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CreateSalesAgentPayload(_Payload):
    user_id: Optional[int] = None
    phone: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    preferred_language: Optional[str] = None
    districts: List[str] = Field(default_factory=list)
    weekly_new_outlet_target: Optional[int] = Field(default=None, ge=0)
    employment_type: Optional[str] = None
    notes: Optional[str] = None


class UpdateSalesAgentPayload(_Payload):
    phone: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    districts: Optional[List[str]] = None
    weekly_new_outlet_target: Optional[int] = Field(default=None, ge=0)
    employment_type: Optional[str] = None
    notes: Optional[str] = None
    # No `is_active` here on purpose: `PUT /admin/staff/sales-agents/<id>/active` is the single
    # door onto `SalesAgentProfile.is_active`, because it is the only one that writes the
    # dedicated `sales_agent_set_active` staff-activity entry. Accepting the field here too made
    # the same column reachable behind a generic `admin_update_sales_agent` audit line.
    # `extra="ignore"` means an admin UI that still sends it gets a clean 200 with the field
    # dropped, not a 400. `StaffService.update_sales_agent` keeps its service-level `is_active`
    # branch for internal callers; this route simply never fills it.


class SetActivePayload(_Payload):
    is_active: bool


class ContactPayload(_Payload):
    name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    role: str = "owner"
    presence_window: Optional[str] = Field(default=None, max_length=100)


class CreateOutletPayload(_Payload):
    name: str = Field(..., min_length=1, max_length=200)
    outlet_type: str
    channel: Optional[str] = Field(default=None, max_length=30)
    contact: Optional[ContactPayload] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address_text: Optional[str] = None
    district: Optional[str] = Field(default=None, max_length=100)
    outlet_class: Optional[str] = Field(default=None, alias="class")
    preferred_language: Optional[str] = Field(default=None, max_length=5)
    notes: Optional[str] = None
    force: bool = False
    link_user_id: Optional[int] = None


class UpdateOutletPayload(_Payload):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    channel: Optional[str] = Field(default=None, max_length=30)
    # Editable, unlike on `CreateOutletPayload`'s path, and validated the opposite way:
    # `_validate_common` stores an unresolvable geocoder hint as NULL so a completed walk-in
    # survives, on the promise that an admin fixes it HERE. Here a human picked the value, so
    # `OutletService.update` refuses one that names no district (SALES_DISTRICT_INVALID).
    district: Optional[str] = None
    outlet_class: Optional[str] = Field(default=None, alias="class")
    cadence_days_override: Optional[int] = Field(default=None, ge=1, le=365)
    opening_hours: Optional[Dict[str, Any]] = None
    preferred_visit_window: Optional[str] = Field(default=None, max_length=50)
    delivery_window_start: Optional[str] = None  # "HH:MM"
    delivery_window_end: Optional[str] = None
    payment_terms: Optional[str] = None
    legal_form: Optional[str] = Field(default=None, max_length=30)
    tax_id: Optional[str] = Field(default=None, max_length=20)
    preferred_language: Optional[str] = Field(default=None, max_length=5)
    competitor_note: Optional[str] = None
    status_warning: Optional[str] = None
    notes: Optional[str] = None


class ApprovePayload(_Payload):
    contract_number: Optional[str] = Field(default=None, max_length=100)


class RejectPayload(_Payload):
    reason: str = Field(..., min_length=1)


class AssignPayload(_Payload):
    agent_user_id: Optional[int] = None


class BulkAssignPayload(_Payload):
    district: str
    agent_user_id: int


class MarkLostPayload(_Payload):
    reason: str
    note: Optional[str] = None


class CheckinPayload(_Payload):
    # Optional on purpose: the Skip button posts `{"skipped": true}` and nothing else, and a
    # Telegram location message without `horizontal_accuracy` is normal (D16 — nothing blocks).
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    horizontal_accuracy: Optional[float] = None
    skipped: bool = False


class StockCheckItemPayload(_Payload):
    product_id: int
    on_hand_qty: int = Field(ge=0)
    empties_qty: Optional[int] = Field(default=None, ge=0)
    is_sold_out: bool = False
    is_low: bool = False


class StockCheckPayload(_Payload):
    # An EMPTY list is a legitimate answer, not a malformed request: until an admin flags a
    # product, `GET /stock-check-products` returns nothing at all, and a shelf screen with
    # nothing to ask about must still advance the visit instead of dead-ending at Abandon.
    # `VisitService.stock_check` writes no rows and moves `current_step` to "order" as usual.
    items: List[StockCheckItemPayload] = Field(min_length=0)


class CloseVisitPayload(_Payload):
    outcome: Optional[str] = None
    no_order_reason: Optional[str] = None
    notes: Optional[str] = None
    next_visit_at: Optional[date] = None
    dm_present: Optional[bool] = None


class AgentConfirmationPayload(_Payload):
    # The customer bot's Confirm/Decline body (Task 5b's `POST /orders/<id>/agent-confirmation`).
    # It lives here with the other sales payloads and is OWNED by this task: Task 5b only imports
    # it. The action VALUE is validated by `AgentOrderConfirmationService.respond`
    # (SALES_CONFIRMATION_ACTION_INVALID), so the allowed set is written once, in the service.
    action: str
    reason: Optional[str] = None


class OrderItemPayload(_Payload):
    product_id: int
    quantity: int = Field(..., ge=1)


class OrderEstimatePayload(_Payload):
    items: List[OrderItemPayload] = Field(..., min_length=1)
    # The rail the agent has chosen, when they have. The loyalty tier discount is a COD-only
    # benefit `create_order` prices live, so a quote taken without the rail cannot equal the
    # charge. Optional: a quote taken before the rail step keeps the rail-less pricing, and the
    # VALUE is validated by `VisitService._rail` -- the same gate the order itself goes through.
    payment_method: Optional[str] = None


class PlaceOrderPayload(_Payload):
    # The window is two "HH:MM" strings, not `time`s: `parse_and_validate_schedule` — the ONE
    # validator every write path shares — parses strings, and a pre-parsed `time` dies inside
    # `parse_window_time` on `.strip()`.
    items: List[OrderItemPayload] = Field(..., min_length=1)
    payment_method: str
    delivery_date: Optional[date] = None
    delivery_window_start: Optional[str] = None
    delivery_window_end: Optional[str] = None
    delivery_notes: Optional[str] = None


class FieldTryoutPayload(_Payload):
    """`POST /outlets/<id>/tryouts` -- the basket, and nothing else.

    Deliberately NOT `min_length=1` on `items`, unlike `PlaceOrderPayload`: an empty or absent
    basket is answered with `SALES_TRYOUT_ITEMS_INVALID`, the coded 400 the bot branches on to
    re-open the product screen, rather than with pydantic's field-level error, which reaches the
    bot as the generic "Something went wrong" (the L47(4) lesson, `visit_service.py:606-615`).

    `OrderItemPayload` is reused verbatim: one `{product_id, quantity}` shape for every basket
    this module accepts. Everything else a try-out needs -- who to call, where to carry it, the
    pin -- is read from the OUTLET by `OutletService.create_tryout_from_field`, so the bot sends
    none of it and cannot send a contact the outlet card does not show (D15).
    """

    items: List[OrderItemPayload] = Field(default_factory=list)
    notes: Optional[str] = None


class DateRangeQuery(_Payload):
    """`?start_date=&end_date=` on every phase-3 admin read route.

    Two strings and nothing else. The RULE -- inclusive LOCAL calendar days, a 7-day
    default, and the SALES_METRICS_MAX_RANGE_DAYS ceiling -- lives in
    `local_windows.parse_date_range`, so the metrics routes, the visits list,
    plan-vs-fact and the exceptions feed all refuse the same range with the same
    SALES_DATE_RANGE_INVALID instead of growing four hand-rolled parsers the way
    `analytics.py` and `admin.py` already have (two copies of the same ISO parser,
    one of which swallows a malformed date into a silent default).

    Fed through `model_validate(request.args.to_dict())`, never `**request.args`:
    splatting a query string into a pydantic model turns `?self=1` into a TypeError
    and a 500.
    """

    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def window(self, *, now: Optional[datetime] = None) -> Tuple[date, date]:
        """The inclusive LOCAL date range this query asked for.

        `window` is the ONE name: Task 4's two routes and Task 5's exceptions route
        call it, and `AdminVisitsQuery`/`AdminExceptionsQuery` inherit it rather than
        each growing a resolver of their own under a name of their own.
        """
        return parse_date_range(self.start_date, self.end_date, now=now)


class PaginatedRangeQuery(DateRangeQuery):
    """A windowed LIST query: the period, plus the page the table asked for.

    `page`/`per_page` CLAMP rather than refuse, and they clamp HERE — the one home. antd's
    pager sends whatever it is holding, so `?per_page=500` has to come back as a hundred rows
    and `?page=-1` as page one; a pydantic `ge`/`le` constraint would make either a 500, and a
    clamp written in the route would make the model's own value a lie the moment anything but
    that route read it. `pagination_meta` re-asserts the same bounds through `PaginationMeta`
    (`api_responses.py:101`), so the two must not disagree: a route that clamped by hand while
    the model did not is exactly how `?per_page=500` reaches `PaginationMeta` as a validation
    error instead of a page.

    Every sales list route that pages inherits this rather than restating the two fields.
    """

    page: int = 1
    per_page: int = DEFAULT_PAGE_SIZE

    @field_validator("page")
    @classmethod
    def _page_floor(cls, value: int) -> int:
        return max(1, value)

    @field_validator("per_page")
    @classmethod
    def _per_page_cap(cls, value: int) -> int:
        return min(max(1, value), MAX_PAGE_SIZE)


class AdminVisitsQuery(PaginatedRangeQuery):
    """`GET /admin/sales/visits`'s query string, typed once.

    Extends `PaginatedRangeQuery` so the period is parsed by the ONE helper every phase-3 read
    surface uses (R12) — a second date parser is how a Visits page and a metrics card end up
    disagreeing about what "last week" means — and so the page bounds are the resource's, not
    this handler's.

    Deliberately no `ge`/`le` constraints on `page`/`per_page`: a hostile `?page=-1` must land
    as page one, not as a pydantic error the route would have to translate into a 500. The
    BASE clamps, `paginate(error_out=False)` catches the rest, and the service caps `per_page`
    at `MAX_PAGE_SIZE` again the way `OutletService.list_admin` does.
    """

    agent_id: Optional[int] = None
    outlet_id: Optional[int] = None
    outcome: Optional[str] = None
    in_radius: Optional[bool] = None


def _iso(value) -> Optional[str]:
    """ISO-8601 — with the UTC offset for instants, bare for plain dates.

    Every datetime column here is `DateTime(timezone=True)`, but SQLite drops the
    tzinfo on round-trip while PostgreSQL keeps it. A raw `.isoformat()` would
    therefore publish `2026-09-08T06:00:00` in the test database and
    `2026-09-08T06:00:00+00:00` in production — the same field, two wire formats,
    with the bot's parser only ever exercised against the naive one. `ensure_utc`
    (the timezone SSOT, ruling 1) normalises first so both answer the same string.
    """
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return value.isoformat() if isinstance(value, date) else None


def _hhmm(value) -> Optional[str]:
    return value.strftime("%H:%M") if isinstance(value, time) else None


def serialize_outlet_contact(contact) -> Dict[str, Any]:
    return {
        "id": contact.id,
        "name": contact.name,
        "phone": contact.phone,
        "role": contact.role,
        "is_primary": bool(contact.is_primary),
        "presence_window": contact.presence_window,
    }


def serialize_outlet(outlet) -> Dict[str, Any]:
    agent = outlet.assigned_agent
    return {
        "id": outlet.id,
        "name": outlet.name,
        "outlet_type": outlet.outlet_type,
        "channel": outlet.channel,
        "stage": outlet.stage,
        "class": outlet.outlet_class,
        "cadence_days_override": outlet.cadence_days_override,
        "user_id": outlet.user_id,
        "address_id": outlet.address_id,
        "latitude": outlet.latitude,
        "longitude": outlet.longitude,
        "address_text": outlet.address_text,
        "district": outlet.district,
        "assigned_agent_user_id": outlet.assigned_agent_user_id,
        "assigned_agent_name": agent.full_name if agent else None,
        "onboarded_by_user_id": outlet.onboarded_by_user_id,
        "next_visit_due_at": _iso(outlet.next_visit_due_at),
        "agent_next_visit_at": _iso(outlet.agent_next_visit_at),
        "last_visit_at": _iso(outlet.last_visit_at),
        "last_order_at": _iso(outlet.last_order_at),
        "opening_hours": outlet.opening_hours,
        "preferred_visit_window": outlet.preferred_visit_window,
        "delivery_window_start": _hhmm(outlet.delivery_window_start),
        "delivery_window_end": _hhmm(outlet.delivery_window_end),
        "payment_terms": outlet.payment_terms,
        "legal_form": outlet.legal_form,
        "tax_id": outlet.tax_id,
        "preferred_language": outlet.preferred_language,
        "storefront_photo_path": outlet.storefront_photo_path,
        "competitor_note": outlet.competitor_note,
        "status_warning": outlet.status_warning,
        "dedupe_candidates": list(outlet.dedupe_candidates or []),
        "activation_requested_at": _iso(outlet.activation_requested_at),
        "approved_at": _iso(outlet.approved_at),
        "approved_by_user_id": outlet.approved_by_user_id,
        "rejected_reason": outlet.rejected_reason,
        "lost_reason": outlet.lost_reason,
        "lost_note": outlet.lost_note,
        "notes": outlet.notes,
        "contacts": [serialize_outlet_contact(c) for c in outlet.contacts],
        "created_at": _iso(outlet.created_at),
        "updated_at": _iso(outlet.updated_at),
    }


def overdue_days(due_at, now) -> Optional[int]:
    """Whole days an outlet is past due, or None when it has no due date.

    Published as a FIELD (D15): neither the staff bot nor the admin UI subtracts dates,
    so "+3d" on a due-list button and "3 days overdue" on the card are the same number.

    Public because Task 6's `OutletService.card` publishes the same number on the card
    (ruling 18) — one function, so the card line and the due-list button can never disagree.
    """
    due_at = ensure_utc(due_at) if due_at is not None else None
    now = ensure_utc(now) if now is not None else None
    if due_at is None or now is None:
        return None
    return max(0, (now - due_at).days)


def serialize_order_brief(order) -> Optional[Dict[str, Any]]:
    """The four fields every visit surface shows about an order, shaped ONCE.

    `serialize_visit`'s `order` block, the `POST /visits/<id>/order` reply (Task 6) and the
    `SALES_VISIT_ORDER_EXISTS` conflict details (Task 5a) are the same four fields; a second
    hand-written copy is how a 409 ends up without the order number the bot prints.
    """
    if order is None:
        return None
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status.value if hasattr(order.status, "value") else order.status,
        "total_amount": float(order.total_amount or 0),
    }


def serialize_order_placed(order) -> Optional[Dict[str, Any]]:
    """The `POST /visits/<id>/order` reply's `order` block: the brief plus the RESOLVED schedule.

    D15 again. The agent asked for "Tomorrow" or for nothing at all, and the backend may have
    filled the window from the outlet's standing hours (ruling 34) — or dropped it because those
    hours had already passed. Publishing what was WRITTEN means Task 12 prints the delivery line
    off this reply instead of echoing the request it sent, which is how a bot ends up telling a
    shopkeeper 09:00–12:00 for an order that is booked anytime.

    Built ON `serialize_order_brief`, never beside it, so the four money/identity fields stay one
    definition across the reply, the visit's `order` block and the 409 conflict details — and it
    answers None exactly where the brief does, instead of raising on a spread of the brief's None.
    """
    brief = serialize_order_brief(order)
    if brief is None:
        return None
    return {
        **brief,
        "delivery_date": _iso(order.delivery_date),
        "delivery_window_start": _hhmm(order.delivery_window_start),
        "delivery_window_end": _hhmm(order.delivery_window_end),
    }


def serialize_stock_check(row) -> Dict[str, Any]:
    return {
        "product_id": row.product_id,
        "product_name": row.product.name if row.product is not None else None,
        "on_hand_qty": row.on_hand_qty,
        "empties_qty": row.empties_qty,
        "is_sold_out": bool(row.is_sold_out),
        "is_low": bool(row.is_low),
        "suggested_qty": row.suggested_qty,
        "accepted_qty": row.accepted_qty,
        "rate_per_day": float(row.rate_per_day) if row.rate_per_day is not None else None,
        "rate_source": row.rate_source,
    }


def serialize_visit(visit, *, previous_stock=None) -> Dict[str, Any]:
    """The whole visit as the staff bot renders it.

    `previous_stock` (ruling 29, spec *Visit* step 2) is the outlet's LAST COMPLETED visit's
    rows, so the shelf screen opens pre-filled and the agent corrects numbers instead of
    typing them. `VisitService.previous_stock_rows(visit)` decides which visit that is; the
    two screen-opening routes (`POST /outlets/<id>/visits`, `GET /visits/current`) pass the
    rows in. Every other response publishes the key as `[]` — the bot keeps what it was
    given at entry and never runs the query itself.
    """
    order = visit.order
    return {
        "id": visit.id,
        "outlet_id": visit.outlet_id,
        "agent_user_id": visit.agent_user_id,
        "status": visit.status,
        "planned": bool(visit.planned),
        "current_step": visit.current_step,
        "started_at": _iso(visit.started_at),
        "checkin_at": _iso(visit.checkin_at),
        "checkin_latitude": visit.checkin_latitude,
        "checkin_longitude": visit.checkin_longitude,
        "checkin_accuracy_m": visit.checkin_accuracy_m,
        "distance_m": visit.distance_m,
        "in_radius": visit.in_radius,
        "checkin_skipped": bool(visit.checkin_skipped),
        "ended_at": _iso(visit.ended_at),
        "outcome": visit.outcome,
        "no_order_reason": visit.no_order_reason,
        "dm_present": visit.dm_present,
        "notes": visit.notes,
        "next_visit_at": _iso(visit.next_visit_at),
        "order": serialize_order_brief(order),
        "stock_checks": [serialize_stock_check(row) for row in visit.stock_checks],
        "previous_stock": [serialize_stock_check(row) for row in (previous_stock or [])],
    }


def serialize_visit_admin_row(visit) -> Dict[str, Any]:
    """One row of the admin *Visits* table: the visit, plus the three names a supervisor reads.

    Built ON `serialize_visit` (the `serialize_outlet_agent_row` precedent): it ADDS fields and
    never re-shapes them, so the supervisor's table and the agent's own screen can never
    disagree about a check-in distance, an outcome or whether a visit was planned.

    `order_number` is lifted to the top level because the table draws one column for it, and a
    column that reaches into `row.order?.order_number` is the same null-handling written twice —
    once here and once in every consumer.
    """
    data = serialize_visit(visit)
    order = data["order"]
    data["outlet_name"] = visit.outlet.name if visit.outlet is not None else None
    data["agent_name"] = visit.agent.full_name if visit.agent is not None else None
    data["order_number"] = order["order_number"] if order else None
    return data


def serialize_plan_vs_fact_row(row) -> Dict[str, Any]:
    """One plan-vs-fact row: one agent, one LOCAL day, plan beside fact.

    `day` is a bare calendar date, never an instant. The table's row key is
    `${agent_user_id}-${day}`; an ISO datetime would split one agent's day into two rows the
    moment the browser's clock differs from the warehouse's.

    `due` is null — not 0 — on a day the nightly snapshot never wrote, with `plan_source`
    saying which of the two it is. "Nobody recorded what was due" and "nothing was due" are
    different answers, and collapsing them scores a perfect day for every date before the
    snapshot job shipped.
    """
    return {
        "agent_user_id": row["agent_user_id"],
        "agent_name": row["agent_name"],
        "day": _iso(row["day"]),
        "due": row["due"],
        "completed": row["completed"],
        "unplanned": row["unplanned"],
        "strike_rate_pct": row["strike_rate_pct"],
        "plan_source": row["plan_source"],
    }


def serialize_visit_photo(photo) -> Dict[str, Any]:
    """One stored visit photo, as the 201 and (phase 3) the exception feed read it.

    `file_path` is the stored copy and `sha256` is over the bytes the phone sent, so the
    history survives a bot-token rotation; `telegram_file_unique_id` is kept only as a
    secondary reference, which is exactly the support-inbox lesson (D17).

    `is_duplicate` is published rather than derived: the hash comparison happened on the
    server, and a client that re-decided it from `duplicate_of_photo_id` would be the same
    rule expressed twice.
    """
    return {
        "id": photo.id,
        "visit_id": photo.visit_id,
        "kind": photo.kind,
        "file_path": photo.file_path,
        "sha256": photo.sha256,
        "telegram_file_unique_id": photo.telegram_file_unique_id,
        "received_at": _iso(photo.received_at),
        "duplicate_of_photo_id": photo.duplicate_of_photo_id,
        "is_duplicate": photo.duplicate_of_photo_id is not None,
    }


def serialize_stock_check_product(product) -> Dict[str, Any]:
    """One row of `GET /sales/stock-check-products` — the shelf list the agent counts.

    `is_returnable_bottle` is the model's SSOT property (`business_app/models/product.py:120`),
    never `tracks_returnable_bottles` re-derived here, so the bot's empties prompt and the
    bottle ledger agree about which SKU is a returnable.
    """
    return {
        "id": product.id,
        "name": product.name,
        "is_returnable_bottle": bool(product.is_returnable_bottle),
        "min_order_quantity": int(product.min_order_quantity or 1),
    }


def serialize_outlet_agent_row(outlet, *, open_visit_id, now, distance_m=None) -> Dict[str, Any]:
    """The agent's list row: the full outlet plus the three answers a list needs.

    `serialize_outlet` stays the SSOT for the outlet itself — this adds fields, it never
    re-shapes them, so the card and the row can never disagree about a stage or a pin.

    `distance_m` is metres from the pin the agent just sent, and it is published on EVERY
    scope (null off the *Nearby* list) so the bot keeps ONE row renderer instead of asking
    which scope drew it. Whole metres, like `find_duplicates`' candidates: "54.7 m" on a
    shelf label is precision nobody has.
    """
    data = serialize_outlet(outlet)
    data["overdue_days"] = overdue_days(outlet.next_visit_due_at, now)
    data["has_open_visit"] = open_visit_id is not None
    data["open_visit_id"] = open_visit_id
    data["distance_m"] = round(distance_m) if distance_m is not None else None
    return data


def serialize_agent_metrics(metrics: Dict[str, Any], *, start_date: date, end_date: date) -> Dict[str, Any]:
    """One agent's KPI card: the window it was measured over, then the metrics.

    The window travels WITH the numbers because every one of them is "as of now over
    these days" -- `orders_delivered_paid` moves when yesterday's order is delivered
    tomorrow -- so a card that showed the numbers without the dates would be a claim
    nobody could check.

    The twenty names and their ORDER are `AgentMetricsService.METRIC_KEYS`' to decide,
    and this function copies the dict rather than re-listing them: re-listing would put
    the same tuple in two modules, and importing METRIC_KEYS here would close a cycle
    (`visit_service` imports this module at import time, and the metrics service reads
    the visit loop).
    """
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "metrics": dict(metrics),
    }


class AdminExceptionsQuery(PaginatedRangeQuery):
    """`GET /admin/sales/exceptions`'s whole query string, typed once.

    Here rather than in the route so `business_app/api/admin_sales.py` keeps its zero
    API-boundary budget (tests/unit/test_structure_boundary_regressions.py:22), and so the
    cap on `per_page` is a property of the resource instead of one handler's opinion.

    SUBCLASSES `PaginatedRangeQuery`: the two date fields, `window()`, and the page bounds are
    all inherited, never re-declared. Five read routes ask the same question, and a second
    parser here is how one typo'd range gets two different answers on two tabs of the same page.
    """

    agent_id: Optional[int] = None
    # `type` is the wire name the admin UI sends; `type_` is the attribute, so nothing in
    # this module shadows the builtin.
    type_: Optional[str] = Field(default=None, alias="type")


def serialize_exception_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One exception-feed row on the wire: the service's dict with its instant normalised.

    The seven types share ONE shape (`EXCEPTION_ROW_KEYS`) so the Visits page's exceptions
    tab is a single antd table. `detail` is the only per-type part and is always a dict,
    never null, so the renderer never has to branch on `type` before it can read a row.
    """
    return {
        "type": row["type"],
        "occurred_at": _iso(row["occurred_at"]),
        "agent_user_id": row["agent_user_id"],
        "agent_name": row["agent_name"],
        "outlet_id": row["outlet_id"],
        "outlet_name": row["outlet_name"],
        "visit_id": row["visit_id"],
        "detail": row["detail"],
    }
