"""Staff API for the sales module: outlets for sales agents, activation approvals for operators."""

from datetime import UTC, datetime

from flask import Blueprint, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from business_app.serializers.sales_pay_serializers import EarningsLinesQuery, to_wire
from business_app.serializers.sales_serializers import (
    AddVisitPhotoPayload,
    ApprovePayload,
    CheckinPayload,
    CloseVisitPayload,
    ContactPayload,
    CreateOutletPayload,
    FieldTryoutPayload,
    OrderEstimatePayload,
    PlaceOrderPayload,
    RejectPayload,
    SetPrimaryPhonePayload,
    StockCheckPayload,
    UpdateOutletPayload,
    serialize_agent_metrics,
    serialize_order_placed,
    serialize_outlet,
    serialize_outlet_agent_row,
    serialize_outlet_contact,
    serialize_stock_check_product,
    serialize_visit,
    serialize_visit_photo,
)
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.pay_statement_service import SalesPayStatementService
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.services.sales.visit_service import VisitService
from business_app.services.tryout_service import TryoutService
from business_app.utils import local_windows
from business_app.utils.api_responses import paginated_response, success_response
from business_app.utils.decorators import require_staff_roles
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import NotFoundError
from business_app.utils.local_windows import resolve_stats_period
from business_app.utils.request_helpers import validated_json_payload


staff_sales_bp = Blueprint("staff_sales", __name__)

# What a SALES AGENT may write through PUT /sales/outlets/<id>. `UpdateOutletPayload` is shared
# with the admin route -- one shape for one resource -- so without this allowlist every admin-only
# column (credit terms, tax id, legal form, district, visit cadence, the operator's status warning)
# parsed cleanly out of an agent's payload and was written by `OutletService.update`. The schema
# says what the resource looks like; the ROUTE says who may write which part of it.
#
# `outlet_class` is admin-only too (D-CLASS, owner-confirmed): the class sets the cadence, the
# cadence sets the due date, and the due date decides which shops enter the 01:20 frozen plan an
# agent's compliance is measured against. The class picked at creation is unchanged, the operator
# reviews it at approval, and the staff bot never calls this route.
AGENT_EDITABLE_OUTLET_FIELDS = frozenset(
    {
        "name",
        "channel",
        "opening_hours",
        "preferred_visit_window",
        "delivery_window_start",
        "delivery_window_end",
        "competitor_note",
        "notes",
        "preferred_language",
    }
)


def _actor_id() -> int:
    return int(get_jwt_identity())


# --- Sales agent: outlets ---


@staff_sales_bp.route("/sales/outlets", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def list_outlets():
    actor_id = _actor_id()
    scope = request.args.get("scope", "all")
    # One open visit per agent, so at most one row in the page carries it. Resolved ONCE, here,
    # and published as a field: the bot never decides "is this the shop I am standing in" from
    # its own conversation state, which a restart has already thrown away.
    open_by_outlet = VisitService.open_visit_ids_by_outlet(actor_id)
    now = datetime.now(UTC)
    if scope == "nearby":
        # A single top-N screen, not a page (R1): the agent has just sent a pin and wants the
        # shops around them. `page`, `per_page` and `search` are ignored on purpose and the
        # ceiling is the configured count -- but the ENVELOPE stays the paginated one, so the
        # bot keeps one list renderer across all four scopes.
        limit = int(current_app.config["SALES_NEARBY_LIMIT"])
        measured = OutletService.nearby(
            actor_id,
            lat=request.args.get("lat", type=float),
            lng=request.args.get("lng", type=float),
            limit=limit,
        )
        rows = [
            serialize_outlet_agent_row(
                outlet, open_visit_id=open_by_outlet.get(outlet.id), now=now, distance_m=distance_m
            )
            for outlet, distance_m in measured
        ]
        # `per_page` is the CONFIGURED limit and `total` is what was actually measured, so
        # this is a page that is not a page: there is no page 2, and asking for one returns
        # the same top-N. That is correct for a "what is around me" screen and it is the
        # reason `page`/`per_page` are ignored above -- do not paginate it later without
        # first moving `SALES_NEARBY_LIMIT` somewhere the bot reads too, or the cap becomes
        # a rule with two expressions.
        return paginated_response(items=rows, page=1, per_page=limit, total=len(rows))
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    items, total = OutletService.list_for_agent(
        actor_id,
        scope,
        search=(request.args.get("search") or "").strip() or None,
        page=page,
        per_page=per_page,
    )
    rows = [serialize_outlet_agent_row(o, open_visit_id=open_by_outlet.get(o.id), now=now) for o in items]
    return paginated_response(items=rows, page=page, per_page=per_page, total=total)


@staff_sales_bp.route("/sales/outlets/dedupe", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def dedupe_outlet():
    candidates = OutletService.find_duplicates(
        request.args.get("name"),
        request.args.get("phone"),
        request.args.get("latitude", type=float),
        request.args.get("longitude", type=float),
    )
    return success_response(data={"candidates": candidates})


@staff_sales_bp.route("/sales/outlets", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def create_outlet():
    payload = validated_json_payload(CreateOutletPayload)
    if not isinstance(payload, dict):
        return payload
    force = bool(payload.pop("force", False))
    link_user_id = payload.pop("link_user_id", None)
    actor_id = _actor_id()
    outlet = OutletService.create(actor_id, payload, force=force, link_user_id=link_user_id)
    # The CARD, as `create_outlet_tryout` answers: the bot draws the receipt and its buttons from
    # this reply, so it carries what the card publishes (`can_request_activation`, D31).
    return success_response(data={"outlet": OutletService.card(outlet, viewer_user_id=actor_id)}, status_code=201)


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def get_outlet(outlet_id):
    actor_id = _actor_id()
    outlet = OutletService.get_for_agent(actor_id, outlet_id)
    return success_response(data={"outlet": OutletService.card(outlet, viewer_user_id=actor_id)})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def update_outlet(outlet_id):
    payload = validated_json_payload(UpdateOutletPayload)
    if not isinstance(payload, dict):
        return payload
    # Dropped, not refused: the agent-editable half of a visit note still lands, rather than the
    # whole edit 400ing over a field the bot never meant to be theirs to send.
    payload = {key: value for key, value in payload.items() if key in AGENT_EDITABLE_OUTLET_FIELDS}
    outlet = OutletService.get_for_agent(_actor_id(), outlet_id)
    return success_response(data={"outlet": serialize_outlet(OutletService.update(outlet, payload, _actor_id()))})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/contacts", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def add_outlet_contact(outlet_id):
    payload = validated_json_payload(ContactPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.get_for_agent(_actor_id(), outlet_id)
    return success_response(
        data={"contact": serialize_outlet_contact(OutletService.add_contact(outlet, payload))}, status_code=201
    )


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/primary-phone", methods=["PUT"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def set_outlet_primary_phone(outlet_id):
    # D31.4: a SET, not the contacts POST above -- `add_contact` adds a NON-primary row once the
    # outlet has a contact, so a name-only primary would keep blocking activation. A PUT because
    # it is idempotent, and the staff client retries a PUT after an ambiguous transport failure.
    # The CARD travels back, as create, request-activation and the try-out answer (P6), so the bot
    # redraws the phone and Request activation buttons from this reply without a second GET.
    payload = validated_json_payload(SetPrimaryPhonePayload)
    if not isinstance(payload, dict):
        return payload
    actor_id = _actor_id()
    outlet = OutletService.get_for_agent(actor_id, outlet_id)
    OutletService.set_primary_phone(outlet, payload.get("phone"))
    return success_response(data={"outlet": OutletService.card(outlet, viewer_user_id=actor_id)})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/request-activation", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def request_outlet_activation(outlet_id):
    actor_id = _actor_id()
    outlet = OutletService.get_for_agent(actor_id, outlet_id)
    OutletService.request_activation(outlet, actor_id)
    # The moved card, for the same reason as the create reply: the bot redraws the outlet from it.
    return success_response(data={"outlet": OutletService.card(outlet, viewer_user_id=actor_id)})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/tryouts", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def create_outlet_tryout(outlet_id):
    payload = validated_json_payload(FieldTryoutPayload)
    if not isinstance(payload, dict):
        return payload
    actor_id = _actor_id()
    outlet = OutletService.get_for_agent(actor_id, outlet_id)
    tryout = OutletService.create_tryout_from_field(outlet, payload, actor_id)
    # `TryoutService.serialize_tryout` is the try-out's ONE shape -- the admin page, the driver
    # bot's task card and this receipt all render the same dict -- so the agent's receipt can
    # never disagree with the driver's screen about what was left at the counter.
    #
    # The CARD travels with it, exactly as `close` and `abandon` answer: this route is the one
    # place outside the visit loop that MOVES the outlet's stage, so a reply carrying only the
    # try-out would hand the bot a screen it knows is stale (still `prospect`) and force a
    # second GET to correct it. One reply, one truth; the receipt lands on a card that already
    # says `trial`.
    return success_response(
        data={
            "tryout": TryoutService.serialize_tryout(tryout),
            "outlet": OutletService.card(outlet, viewer_user_id=actor_id),
        },
        status_code=201,
    )


# --- Sales agent: the visit loop ---


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/visits", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def start_visit(outlet_id):
    visit = VisitService.start(_actor_id(), outlet_id)
    # Ruling 29 / spec *Visit* step 2: this is one of the two routes that OPEN a screen, so it
    # carries the previous visit's counts and the bot's shelf screen arrives pre-filled. The
    # service decides which visit is "previous"; the bot never queries for it.
    return success_response(
        data={"visit": serialize_visit(visit, previous_stock=VisitService.previous_stock_rows(visit))},
        status_code=201,
    )


@staff_sales_bp.route("/sales/visits/current", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def current_visit():
    actor_id = _actor_id()
    visit = VisitService.current(actor_id)
    if visit is None:
        # 404, not an empty 200: "no visit open" is the answer the bot's resume path branches on,
        # and a machine-readable code keeps that branch off prose matching.
        raise NotFoundError("No open visit", error_code="SALES_VISIT_NOT_FOUND")
    # The other screen-opening route (ruling 29): a resumed visit renders its own
    # `stock_checks`, a fresh one renders `previous_stock`, and both arrive in one reply.
    data = serialize_visit(visit, previous_stock=VisitService.previous_stock_rows(visit))
    # `OutletService.get`, not `get_for_agent`: the visit IS the ownership proof, and re-asking the
    # assignment question would 403 an agent mid-visit the moment a manager reassigns the outlet.
    outlet = OutletService.get(data["outlet_id"])
    # The open visit is already in hand, so the card is handed the SAME mapping the due-list
    # route builds instead of asking the database the question this route has just answered.
    return success_response(
        data={
            "visit": data,
            "outlet": OutletService.card(
                outlet,
                viewer_user_id=actor_id,
                open_visit_ids=VisitService.open_visit_ids_by_outlet(actor_id, open_visit=visit),
            ),
        }
    )


@staff_sales_bp.route("/sales/visits/<int:visit_id>/checkin", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def checkin_visit(visit_id):
    payload = validated_json_payload(CheckinPayload)
    if not isinstance(payload, dict):
        return payload
    visit = VisitService.get_owned(visit_id, _actor_id())
    visit = VisitService.checkin(
        visit,
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
        horizontal_accuracy=payload.get("horizontal_accuracy"),
        skipped=bool(payload.get("skipped", False)),
    )
    return success_response(data={"visit": serialize_visit(visit)})


@staff_sales_bp.route("/sales/visits/<int:visit_id>/stock-check", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def stock_check_visit(visit_id):
    payload = validated_json_payload(StockCheckPayload)
    if not isinstance(payload, dict):
        return payload
    visit = VisitService.get_owned(visit_id, _actor_id())
    items = VisitService.stock_check(visit, payload["items"])
    return success_response(data={"visit": serialize_visit(visit), "items": items})


@staff_sales_bp.route("/sales/stock-check-products", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def stock_check_products():
    products = ReplenishmentService.stock_check_products()
    return success_response(data={"items": [serialize_stock_check_product(p) for p in products]})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/payment-methods", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def outlet_payment_methods(outlet_id):
    # An EMPTY list is a valid answer (200), not an error: only a missing customer account or
    # address is SALES_OUTLET_NOT_ACTIVE, and the service raises that itself. Task 12 shows the
    # empty menu as `staff.sales.error.outlet_not_active` and keeps the agent on the order screen.
    outlet = OutletService.get_for_agent(_actor_id(), outlet_id)
    return success_response(data={"methods": VisitService.payment_methods(outlet)})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/order-estimate", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def outlet_order_estimate(outlet_id):
    payload = validated_json_payload(OrderEstimatePayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.get_for_agent(_actor_id(), outlet_id)
    return success_response(
        data={"estimate": VisitService.order_estimate(outlet, payload["items"], payload.get("payment_method"))}
    )


@staff_sales_bp.route("/sales/visits/<int:visit_id>/order", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def place_visit_order(visit_id):
    payload = validated_json_payload(PlaceOrderPayload)
    if not isinstance(payload, dict):
        return payload
    actor_id = _actor_id()
    visit = VisitService.get_owned(visit_id, actor_id)
    order, state = VisitService.place_order(visit, payload, actor_id)
    # `serialize_order_placed` is built ON `serialize_order_brief` -- the same shaper the visit's
    # own `order` block uses -- plus the RESOLVED schedule, which no other surface publishes and
    # which the bot must not re-derive from the payload it sent (ruling 34 may have supplied or
    # dropped the window).
    data = serialize_visit(visit)
    return success_response(
        data={"order": serialize_order_placed(order), "confirmation": {"state": state}, "visit": data},
        status_code=201,
    )


@staff_sales_bp.route("/sales/visits/<int:visit_id>/photos", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def add_visit_photo(visit_id):
    payload = validated_json_payload(AddVisitPhotoPayload)
    if not isinstance(payload, dict):
        return payload
    actor_id = _actor_id()
    # `must_be_open=True` (R8), spelled out rather than left to the default: a closed visit
    # is a record of what happened, and a photo is work in progress.
    visit = VisitService.get_owned(visit_id, actor_id, must_be_open=True)
    photo = VisitService.add_photo(
        visit,
        telegram_file_id=payload.get("telegram_file_id"),
        telegram_file_unique_id=payload.get("telegram_file_unique_id"),
        sha256=payload.get("sha256"),
        kind=(payload.get("kind") or "").strip(),
        agent_user_id=actor_id,
    )
    return success_response(data={"photo": serialize_visit_photo(photo)}, status_code=201)


@staff_sales_bp.route("/sales/visits/<int:visit_id>/close", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def close_visit(visit_id):
    payload = validated_json_payload(CloseVisitPayload)
    if not isinstance(payload, dict):
        return payload
    actor_id = _actor_id()
    visit = VisitService.get_owned(visit_id, actor_id)
    visit = VisitService.close(
        visit,
        outcome=payload.get("outcome"),
        no_order_reason=payload.get("no_order_reason"),
        notes=payload.get("notes"),
        next_visit_at=payload.get("next_visit_at"),
        dm_present=payload.get("dm_present"),
    )
    data = serialize_visit(visit)
    # The CARD, same as `GET /visits/current`: the closing screen shows the recomputed next-due
    # date and the bot lands back on a card whose Start/Resume button is already correct.
    outlet = OutletService.get(data["outlet_id"])
    return success_response(data={"visit": data, "outlet": OutletService.card(outlet, viewer_user_id=actor_id)})


@staff_sales_bp.route("/sales/visits/<int:visit_id>/abandon", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def abandon_visit(visit_id):
    actor_id = _actor_id()
    visit = VisitService.get_owned(visit_id, actor_id)
    data = serialize_visit(VisitService.abandon(visit, actor_id))
    # The SAME reply shape as the close route, because this endpoint now has the same two
    # endings: `abandon` CLOSES a visit that carries an order (ruling 74), and the bot renders
    # the close receipt -- whose date is the recomputed `next_visit_due_at` -- off this card.
    # One shape for both endings also means the bot reads `visit.status` to tell them apart
    # rather than guessing from which key happens to be present.
    outlet = OutletService.get(data["outlet_id"])
    return success_response(data={"visit": data, "outlet": OutletService.card(outlet, viewer_user_id=actor_id)})


# --- Sales agent: my stats ---


@staff_sales_bp.route("/sales/me/stats", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def my_stats():
    # ONE window per call. `today`, `week` and `month` are three different local windows,
    # and the bot's card is three buttons wide rather than three cards deep -- computing all
    # three on every tap would run the twenty aggregates three times for a screen that shows
    # one of them.
    #
    # `_actor_id()`, never an `agent_id` query parameter: this route is "mine", and a route
    # that accepted an id would be one missing ownership check away from letting any agent
    # read any other agent's numbers.
    period, start_date, end_date = resolve_stats_period(request.args.get("period"))
    data = serialize_agent_metrics(
        AgentMetricsService.compute(_actor_id(), start_date, end_date), start_date=start_date, end_date=end_date
    )
    data["period"] = period
    return success_response(data=data)


# --- Sales agent: my earnings ---


@staff_sales_bp.route("/sales/me/earnings", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def my_earnings():
    # "Mine", like /me/stats: `_actor_id()`, never an agent id, so there is no ownership check to
    # forget (§5.6). The service syncs the agent's open months before it estimates them.
    return success_response(data=to_wire(SalesPayStatementService.agent_earnings(_actor_id())))


@staff_sales_bp.route("/sales/me/earnings/lines", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def my_earnings_lines():
    # The page rides inside `data` (`{items, page, has_more}`): the bot client drops the envelope's
    # `meta`. The month is parsed here like every month on the admin pay routes.
    query = EarningsLinesQuery.model_validate(request.args.to_dict())
    month = local_windows.parse_month(query.month)
    return success_response(data=to_wire(SalesPayStatementService.agent_lines(_actor_id(), month, page=query.page)))


@staff_sales_bp.route("/sales/me/statements", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def my_statements():
    # S3: the agent's own approved-or-paid months, newest first. "Mine", like S1: no agent id.
    return success_response(data=to_wire(SalesPayStatementService.agent_statements(_actor_id())))


@staff_sales_bp.route("/sales/me/statements/<month>", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("sales_agent")
def my_statement(month):
    # S4: one of those months in full. The month is parsed as S2 parses its own, so anything but
    # "YYYY-MM" is the same SALES_PAY_MONTH_INVALID.
    data = SalesPayStatementService.agent_statement(_actor_id(), local_windows.parse_month(month))
    return success_response(data=to_wire(data))


# --- Operator: activation requests ---


@staff_sales_bp.route("/sales/activation-requests", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def list_activation_requests():
    return success_response(data={"items": OutletService.activation_request_rows(viewer_id=_actor_id())})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/approve", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def approve_outlet(outlet_id):
    payload = validated_json_payload(ApprovePayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.approve(
        outlet_id,
        actor_id=_actor_id(),
        contract_number=payload.get("contract_number"),
        attach=bool(payload.get("attach", False)),
    )
    return success_response(data={"outlet": serialize_outlet(outlet)})


@staff_sales_bp.route("/sales/outlets/<int:outlet_id>/reject", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("operator")
def reject_outlet(outlet_id):
    payload = validated_json_payload(RejectPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.reject(outlet_id, actor_id=_actor_id(), reason=payload["reason"])
    return success_response(data={"outlet": serialize_outlet(outlet)})
