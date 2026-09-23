"""Admin API endpoints for the sales module: sales agents and outlets."""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.sales_serializers import (
    AdminExceptionsQuery,
    AdminVisitsQuery,
    ApprovePayload,
    AssignPayload,
    BulkAssignPayload,
    ContactPayload,
    CreateSalesAgentPayload,
    DateRangeQuery,
    MarkLostPayload,
    RejectPayload,
    SetActivePayload,
    UpdateContactPayload,
    UpdateOutletPayload,
    UpdateSalesAgentPayload,
    serialize_agent_metrics,
    serialize_exception_row,
    serialize_outlet,
    serialize_outlet_contact,
    serialize_outlet_photo_row,
    serialize_plan_vs_fact_row,
    serialize_visit_admin_row,
)
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.sales.day_plan_service import AgentDayPlanService
from business_app.services.sales.exception_feed_service import EXCEPTION_TYPES, ExceptionFeedService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_service import VisitService
from business_app.services.staff_service import StaffService
from business_app.utils.api_responses import (
    paginated_response,
    pagination_meta,
    success_response,
    validation_error_response,
)
from business_app.utils.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from business_app.utils.decorators import manager_or_higher_required
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.request_helpers import parse_bool_arg


admin_sales_bp = Blueprint("admin_sales", __name__)


def _actor_id() -> int:
    return int(get_jwt_identity())


def _validated_payload(schema_cls):
    payload = request.get_json() or {}
    try:
        # exclude_unset, NOT exclude_none: a field the client never mentioned is dropped, but a
        # null it deliberately sent survives, so emptying an input in the admin UI actually clears
        # the column instead of returning a success toast that wrote nothing.
        #
        # The rule the services then apply: an explicit null CLEARS a nullable column, and a
        # NOT-NULL column REFUSES it (`outlets.preferred_language` keeps its truthiness guard;
        # `outlets.payment_terms` 400s with SALES_PAYMENT_TERMS_INVALID). Both halves are pinned in
        # tests/integration/test_admin_sales_outlets_api.py.
        return schema_cls(**payload).model_dump(exclude_unset=True)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


# --- Sales agents ---


@admin_sales_bp.route("/staff/sales-agents", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_sales_agents():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int), MAX_PAGE_SIZE)
    items, total, summary = SalesAgentAccountService.list_agents(
        page=page,
        per_page=per_page,
        search=(request.args.get("search") or "").strip() or None,
        status=request.args.get("status"),
    )
    return paginated_response(
        items=items, page=page, per_page=per_page, total=total, additional_meta={"summary": summary}
    )


@admin_sales_bp.route("/staff/sales-agents", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def create_sales_agent():
    payload = _validated_payload(CreateSalesAgentPayload)
    if not isinstance(payload, dict):
        return payload
    user = StaffService.create_sales_agent(payload, created_by=_actor_id())
    return success_response(data={"sales_agent": SalesAgentAccountService.get_agent(user.id)}, status_code=201)


@admin_sales_bp.route("/staff/sales-agents/<int:user_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_sales_agent(user_id):
    return success_response(data={"sales_agent": SalesAgentAccountService.get_agent(user_id)})


@admin_sales_bp.route("/staff/sales-agents/<int:user_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def update_sales_agent(user_id):
    payload = _validated_payload(UpdateSalesAgentPayload)
    if not isinstance(payload, dict):
        return payload
    StaffService.update_sales_agent(user_id, payload, updated_by=_actor_id())
    return success_response(data={"sales_agent": SalesAgentAccountService.get_agent(user_id)})


@admin_sales_bp.route("/staff/sales-agents/<int:user_id>/active", methods=["PUT"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def set_sales_agent_active(user_id):
    payload = _validated_payload(SetActivePayload)
    if not isinstance(payload, dict):
        return payload
    agent = SalesAgentAccountService.set_active(user_id, payload["is_active"], actor_id=_actor_id())
    return success_response(data={"sales_agent": agent})


@admin_sales_bp.route("/sales/agents/metrics", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_agent_metrics():
    # EVERY active agent in ONE pass, deliberately unpaginated. The Agent-performance tab
    # draws a row per agent and exports exactly those rows, and the Sales-agents page draws
    # the same numbers in its cards; calling the per-agent route N times would run twenty
    # aggregates twenty times per render AND make the CSV a different query from the table
    # it claims to export.
    start_date, end_date = DateRangeQuery.model_validate(request.args.to_dict()).window()
    return success_response(
        data={
            "agents": AgentMetricsService.rows_for_period(start_date, end_date),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
    )


@admin_sales_bp.route("/sales/agents/<int:user_id>/metrics", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_agent_metrics(user_id):
    # `<int:user_id>` is a users.id, never a sales_agent_profiles.id -- the try-out
    # id-space bug wrote one id space into the other's column and stranded eight rows.
    #
    # The range is parsed first so a malformed window always reports as a malformed
    # window, and `get_agent` is the ONE answer to "is this id a sales agent": it already
    # 404s STAFF_USER_NOT_FOUND for a customer, a driver or an admin, so this route does
    # not re-ask the question with a second predicate that could drift from it.
    start_date, end_date = DateRangeQuery.model_validate(request.args.to_dict()).window()
    agent = SalesAgentAccountService.get_agent(user_id)
    data = serialize_agent_metrics(
        AgentMetricsService.compute(user_id, start_date, end_date), start_date=start_date, end_date=end_date
    )
    data["agent"] = {"id": agent["user_id"], "full_name": agent["full_name"]}
    return success_response(data=data)


# --- Outlets ---


def _serialize_history(outlet):
    return [
        {
            "id": h.id,
            "from_stage": h.from_stage,
            "to_stage": h.to_stage,
            "reason_code": h.reason_code,
            "note": h.note,
            "actor_user_id": h.actor_user_id,
            "created_at": h.created_at.isoformat() if h.created_at else None,
        }
        for h in outlet.stage_history
    ]


@admin_sales_bp.route("/sales/outlets", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_outlets():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int), MAX_PAGE_SIZE)
    filters = {
        "search": (request.args.get("search") or "").strip() or None,
        "stage": request.args.get("stage"),
        "outlet_class": request.args.get("class"),
        "outlet_type": request.args.get("outlet_type"),
        "agent_user_id": request.args.get("agent_user_id", type=int),
        "district": request.args.get("district"),
        "unvisited_days": request.args.get("unvisited_days", type=int),
    }
    if request.args.get("format") == "pins":
        # Unpaginated on purpose: `list_pins` answers "every matching outlet that has a pin", so a
        # 400-outlet estate draws 400 pins. Reusing the paginated list here would cap the map at one
        # page and make a truncated estate look like an empty one.
        pins = [
            {
                "id": o.id,
                "name": o.name,
                "stage": o.stage,
                "outlet_type": o.outlet_type,
                "lat": o.latitude,
                "lng": o.longitude,
                "assigned_agent_user_id": o.assigned_agent_user_id,
                "district": o.district,
            }
            for o in OutletService.list_pins(filters)
        ]
        return success_response(data={"pins": pins})
    items, total, summary = OutletService.list_admin(filters, page=page, per_page=per_page)
    return paginated_response(
        items=[serialize_outlet(o) for o in items],
        page=page,
        per_page=per_page,
        total=total,
        additional_meta={"summary": summary},
    )


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_outlet_admin(outlet_id):
    outlet = OutletService.get(outlet_id)
    return success_response(data={"outlet": OutletService.card(outlet), "stage_history": _serialize_history(outlet)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def update_outlet_admin(outlet_id):
    payload = _validated_payload(UpdateOutletPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.update(OutletService.get(outlet_id), payload, _actor_id())
    return success_response(data={"outlet": serialize_outlet(outlet)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/contacts", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def add_outlet_contact_admin(outlet_id):
    payload = _validated_payload(ContactPayload)
    if not isinstance(payload, dict):
        return payload
    contact = OutletService.add_contact(OutletService.get(outlet_id), payload)
    return success_response(data={"contact": serialize_outlet_contact(contact)}, status_code=201)


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/contacts/<int:contact_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def update_outlet_contact_admin(outlet_id, contact_id):
    payload = _validated_payload(UpdateContactPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.get(outlet_id)
    contact = OutletService.update_contact(outlet, OutletService.get_contact(outlet, contact_id), payload)
    return success_response(data={"contact": serialize_outlet_contact(contact)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/contacts/<int:contact_id>", methods=["DELETE"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def delete_outlet_contact_admin(outlet_id, contact_id):
    outlet = OutletService.get(outlet_id)
    OutletService.delete_contact(outlet, OutletService.get_contact(outlet, contact_id))
    return success_response(data={"contact_id": contact_id})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/approve", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def approve_outlet_admin(outlet_id):
    payload = _validated_payload(ApprovePayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.approve(
        outlet_id,
        actor_id=_actor_id(),
        contract_number=payload.get("contract_number"),
        attach=bool(payload.get("attach", False)),
    )
    return success_response(data={"outlet": serialize_outlet(outlet)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/reject", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def reject_outlet_admin(outlet_id):
    payload = _validated_payload(RejectPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.reject(outlet_id, actor_id=_actor_id(), reason=payload["reason"])
    return success_response(data={"outlet": serialize_outlet(outlet)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/assign", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def assign_outlet_admin(outlet_id):
    payload = _validated_payload(AssignPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.assign(outlet_id, payload.get("agent_user_id"), actor_id=_actor_id())
    return success_response(data={"outlet": serialize_outlet(outlet)})


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/mark-lost", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def mark_outlet_lost_admin(outlet_id):
    payload = _validated_payload(MarkLostPayload)
    if not isinstance(payload, dict):
        return payload
    outlet = OutletService.mark_lost(
        outlet_id, actor_id=_actor_id(), reason=payload["reason"], note=payload.get("note")
    )
    return success_response(data={"outlet": serialize_outlet(outlet)})


@admin_sales_bp.route("/sales/outlets/bulk-assign", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def bulk_assign_outlets_admin():
    payload = _validated_payload(BulkAssignPayload)
    if not isinstance(payload, dict):
        return payload
    updated = OutletService.bulk_assign_by_district(payload["district"], payload["agent_user_id"], actor_id=_actor_id())
    return success_response(data={"updated": updated})


@admin_sales_bp.route("/sales/outlets/import-existing-customers", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def import_existing_customers_admin():
    return success_response(data={"created": OutletService.import_existing_customers(actor_id=_actor_id())})


# --- Visits ---


@admin_sales_bp.route("/sales/visits", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_visits_admin():
    # `in_radius` comes through `parse_bool_arg`, never `type=bool`: Flask's converter makes
    # `bool("false")` True, and the whole point of this filter is the third state (absent).
    #
    # ONE construction style across the phase (Task 3's binding): `model_validate` over a dict
    # built from `request.args`, never `Model(**request.args)` — splatting a query string turns
    # `?self=1` into a TypeError and a 500. The values that need house parsing go into the dict
    # FIRST, so a junk `?page=abc` lands on the default instead of raising.
    # The local is `params`, never `query` (M11) — one name for the validated model across
    # Tasks 4 and 5, and `test_structure_boundary_regressions.py` scores a bare dot-query
    # attribute access anywhere in this file, so a local called `query` reads like ORM access
    # to anyone skimming it (and spelling that regex out in a comment scores against the
    # budget too — this file's is 0).
    params = AdminVisitsQuery.model_validate(
        {
            **request.args.to_dict(),
            "agent_id": request.args.get("agent_id", type=int),
            "outlet_id": request.args.get("outlet_id", type=int),
            "outcome": (request.args.get("outcome") or "").strip() or None,
            "in_radius": parse_bool_arg("in_radius"),
            "photo": (request.args.get("photo") or "").strip() or None,
            "page": request.args.get("page", 1, type=int),
            "per_page": request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int),
        }
    )
    start_date, end_date = params.window()
    # No clamp here: `PaginatedRangeQuery` floors `page` and caps `per_page` at MAX_PAGE_SIZE
    # for every sales list route (sales_serializers.py:238). Two expressions of one bound is
    # how `?per_page=500` reaches `PaginationMeta` as a validation error on one route and as a
    # hundred rows on the next.
    visits, total = VisitService.list_for_admin(
        start_date=start_date,
        end_date=end_date,
        agent_user_id=params.agent_id,
        outlet_id=params.outlet_id,
        outcome=params.outcome,
        in_radius=params.in_radius,
        photo=params.photo,
        page=params.page,
        per_page=params.per_page,
    )
    # `meta` rides INSIDE `data` here, unlike the outlets list: the page reads
    # `response.data.data` through `salesService`, and the resolved window has to arrive beside
    # the rows so the table never re-derives the period it asked for.
    return success_response(
        data={
            "visits": [serialize_visit_admin_row(visit) for visit in visits],
            "meta": pagination_meta(page=params.page, per_page=params.per_page, total=total),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
    )


@admin_sales_bp.route("/sales/outlets/<int:outlet_id>/photos", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_outlet_photos_admin(outlet_id):
    # `list_outlets`' page parsing: a junk `?page=abc` lands on the default, and
    # `paginate(error_out=False)` answers an empty page past the end rather than a 404.
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int), 1), MAX_PAGE_SIZE)
    outlet = OutletService.get(outlet_id)
    photos, total = VisitService.list_outlet_photos(outlet.id, page=page, per_page=per_page)
    return success_response(
        data={
            "photos": [serialize_outlet_photo_row(photo) for photo in photos],
            "meta": pagination_meta(page=page, per_page=per_page, total=total),
        }
    )


@admin_sales_bp.route("/sales/visit-photos/<int:photo_id>/file", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def get_visit_photo_file_admin(photo_id):
    # A photo id only: the Telegram file id is resolved from the row inside `stream_photo`.
    return VisitService.stream_photo(photo_id)


@admin_sales_bp.route("/sales/plan-vs-fact", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def plan_vs_fact_admin():
    # Deliberately UNPAGINATED: the table is read as a grid (agents down, days across), and a
    # page of it is not a smaller grid. The 92-day cap inside `window()` is what bounds it.
    #
    # Same construction style as the visits route above: `model_validate` over a dict, never a
    # splat of the query string.
    params = DateRangeQuery.model_validate(request.args.to_dict())
    start_date, end_date = params.window()
    rows = AgentDayPlanService.plan_vs_fact(start_date, end_date, agent_user_id=request.args.get("agent_id", type=int))
    # The service answers the question; the ROUTE shapes the wire payload — the same division
    # Task 5's exceptions feed keeps (`ExceptionFeedService.list` returns raw rows, the route
    # maps `serialize_exception_row` over them). A service that serialized itself would be the
    # only one in the sales estate that did.
    return success_response(
        data={
            "rows": [serialize_plan_vs_fact_row(row) for row in rows],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
    )


# --- Exceptions feed ---


@admin_sales_bp.route("/sales/exceptions", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_sales_exceptions():
    # `model_validate` over the dict, never `AdminExceptionsQuery(**request.args.to_dict())`:
    # splatting a query string means `?self=1` or `?cls=x` is a TypeError and a 500, which is
    # exactly what `tests/integration/test_api_query_resilience_matrix.py` throws at every
    # registered route. The ints are pre-parsed the house way so `?page=abc` lands on the
    # default rather than raising.
    params = AdminExceptionsQuery.model_validate(
        {
            **request.args.to_dict(),
            "agent_id": request.args.get("agent_id", type=int),
            "page": request.args.get("page", 1, type=int),
            "per_page": request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int),
        }
    )
    start_date, end_date = params.window()
    rows, total = ExceptionFeedService.list(
        start_date,
        end_date,
        agent_user_id=params.agent_id,
        type_=params.type_,
        page=params.page,
        per_page=params.per_page,
    )
    # `meta` rides INSIDE `data`, exactly as Task 4's visits route publishes it, and is built
    # by the ONE page-arithmetic helper. A top-level `meta` would be dropped on the floor by
    # `salesService`'s `response.data.data` unwrap and the table would page on `total: 0`.
    return success_response(
        data={
            "exceptions": [serialize_exception_row(row) for row in rows],
            "meta": pagination_meta(page=params.page, per_page=params.per_page, total=total),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            # The vocabulary travels with the feed: the type filter's options are the
            # backend's tuple, so a new type reaches the dropdown without a second copy of
            # the list living in JavaScript.
            "types": list(EXCEPTION_TYPES),
        }
    )
