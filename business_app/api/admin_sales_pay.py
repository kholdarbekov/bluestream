"""Admin API for sales-agent pay (spec §5).

Every route here is ADMIN-only: `super_admin_required` checks the role claim, the DB row and an
ACTIVE account, because every figure these routes touch is pay (C11). The manager penalty-proposal
pair (§5.3) is the one exception, and its decorator says so.

Routes stay thin. A body goes through `validated_json_payload` (an unknown key is a 400), the
service decides, and `to_wire` is the only converter to JSON.
"""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from business_app.serializers.sales_pay_serializers import (
    AdjustmentPayload,
    CreatePlanPayload,
    EmploymentPayload,
    HolidaysPayload,
    MarkPaidPayload,
    PayLinesQuery,
    PenaltiesQuery,
    PenaltyConfirmPayload,
    PenaltyCreatePayload,
    PenaltyNotePayload,
    PenaltyTypeCreatePayload,
    PenaltyTypeUpdatePayload,
    ProposalPayload,
    ProposalsQuery,
    StartPayPayload,
    TermsPayload,
    UnpaidDaysPayload,
    VersionPayload,
    serialize_admin_penalty_row,
    serialize_penalty_proposal,
    serialize_penalty_type,
    to_wire,
)
from business_app.services.sales.pay_penalty_service import SalesPayAdjustmentService, SalesPayPenaltyService
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_statement_service import SalesPayStatementService
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.utils import local_windows
from business_app.utils.api_responses import pagination_meta, success_response
from business_app.utils.decorators import manager_or_higher_required, super_admin_required
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.request_helpers import validated_json_payload
from shared.staff_constants import SALES_PAY_PENALTY_STATUSES

admin_sales_pay_bp = Blueprint("admin_sales_pay", __name__)


def _actor_id() -> int:
    return int(get_jwt_identity())


# --- Plans and versions (A12-A15) ---


@admin_sales_pay_bp.route("/sales/pay/plans", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def list_pay_plans():
    return success_response(data=to_wire(SalesPayPlanService.list_plans()))


@admin_sales_pay_bp.route("/sales/pay/plans", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def create_pay_plan():
    payload = validated_json_payload(CreatePlanPayload)
    if not isinstance(payload, dict):
        return payload
    plan = SalesPayPlanService.create_plan(payload["name"], payload["version"], actor_id=_actor_id())
    return success_response(data=to_wire({"plan": SalesPayPlanService.plan_row(plan)}), status_code=201)


@admin_sales_pay_bp.route("/sales/pay/plans/<int:plan_id>/versions/<int:version_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def get_pay_plan_version(plan_id, version_id):
    return success_response(data=to_wire({"version": SalesPayPlanService.get_version(plan_id, version_id)}))


@admin_sales_pay_bp.route("/sales/pay/plans/<int:plan_id>/versions", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def create_pay_plan_version(plan_id):
    payload = validated_json_payload(VersionPayload)
    if not isinstance(payload, dict):
        return payload
    version = SalesPayPlanService.create_version(plan_id, payload, actor_id=_actor_id())
    return success_response(
        data=to_wire({"version": SalesPayPlanService.get_version(plan_id, version.id)}), status_code=201
    )


# --- Months (A0-A7) ---
#
# `<month>` is a "YYYY-MM" path segment; `parse_month` refuses anything else with
# SALES_PAY_MONTH_INVALID. Every lifecycle route answers the month's `PeriodDetail`, so the page
# re-renders from the backend's own figures and `next_actions`.


def _period_detail(month):
    return success_response(data=to_wire(SalesPayStatementService.period_detail(month)))


@admin_sales_pay_bp.route("/sales/pay/start", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def start_pay():
    payload = validated_json_payload(StartPayPayload)
    if not isinstance(payload, dict):
        return payload
    month = local_windows.parse_month(payload["month"])
    SalesPayPeriodService.start(month, is_shadow=payload["is_shadow"], actor_id=_actor_id())
    return success_response(data=to_wire(SalesPayStatementService.period_detail(month)), status_code=201)


@admin_sales_pay_bp.route("/sales/pay/periods", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def list_pay_periods():
    return success_response(data=to_wire(SalesPayStatementService.period_list()))


@admin_sales_pay_bp.route("/sales/pay/periods/<month>", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def get_pay_period(month):
    return _period_detail(local_windows.parse_month(month))


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/close", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def close_pay_period(month):
    parsed = local_windows.parse_month(month)
    SalesPayPeriodService.close(parsed, actor_id=_actor_id())
    return _period_detail(parsed)


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/recalculate", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def recalculate_pay_period(month):
    parsed = local_windows.parse_month(month)
    SalesPayPeriodService.recalculate(parsed, actor_id=_actor_id())
    return _period_detail(parsed)


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/approve", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def approve_pay_period(month):
    parsed = local_windows.parse_month(month)
    SalesPayPeriodService.approve(parsed, actor_id=_actor_id())
    return _period_detail(parsed)


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/mark-paid", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def mark_pay_period_paid(month):
    parsed = local_windows.parse_month(month)
    payload = validated_json_payload(MarkPaidPayload)
    if not isinstance(payload, dict):
        return payload
    SalesPayPeriodService.mark_paid(parsed, payload["paid_on"], actor_id=_actor_id())
    return _period_detail(parsed)


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/holidays", methods=["PUT"])
@handle_api_exception
@jwt_required()
@super_admin_required
def set_pay_period_holidays(month):
    parsed = local_windows.parse_month(month)
    payload = validated_json_payload(HolidaysPayload)
    if not isinstance(payload, dict):
        return payload
    SalesPayPeriodService.set_holidays(parsed, payload["days"], actor_id=_actor_id())
    return _period_detail(parsed)


# --- One agent in one month (A8-A11) ---


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/agents/<int:agent_user_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def get_pay_statement(month, agent_user_id):
    data = SalesPayStatementService.statement_detail(local_windows.parse_month(month), agent_user_id)
    return success_response(data=to_wire(data))


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/agents/<int:agent_user_id>/lines", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def list_pay_statement_lines(month, agent_user_id):
    query = PayLinesQuery.model_validate(request.args.to_dict())
    data = SalesPayStatementService.statement_lines(
        local_windows.parse_month(month), agent_user_id, kind=query.kind, page=query.page, per_page=query.per_page
    )
    return success_response(data=to_wire(data))


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/agents/<int:agent_user_id>/unpaid-days", methods=["PUT"])
@handle_api_exception
@jwt_required()
@super_admin_required
def set_pay_unpaid_days(month, agent_user_id):
    parsed = local_windows.parse_month(month)
    payload = validated_json_payload(UnpaidDaysPayload)
    if not isinstance(payload, dict):
        return payload
    SalesPayTermsService.set_unpaid_days(agent_user_id, parsed, payload["days"], actor_id=_actor_id())
    return success_response(data=to_wire(SalesPayStatementService.statement_detail(parsed, agent_user_id)))


@admin_sales_pay_bp.route("/sales/pay/periods/<month>/agents/<int:agent_user_id>/adjustments", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def create_pay_adjustment(month, agent_user_id):
    parsed = local_windows.parse_month(month)
    payload = validated_json_payload(AdjustmentPayload)
    if not isinstance(payload, dict):
        return payload
    adjustment = SalesPayAdjustmentService.create(
        agent_user_id, parsed, payload["amount"], payload["reason"], actor_id=_actor_id()
    )
    return success_response(
        data=to_wire({"adjustment": SalesPayStatementService.adjustment_row(adjustment)}), status_code=201
    )


# --- Agent pay terms and employment (A16-A18) ---


@admin_sales_pay_bp.route("/sales/pay/agents/<int:agent_user_id>/terms", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def get_agent_pay_terms(agent_user_id):
    return success_response(data=to_wire(SalesPayTermsService.agent_terms(agent_user_id)))


@admin_sales_pay_bp.route("/sales/pay/agents/<int:agent_user_id>/terms", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def add_agent_pay_terms(agent_user_id):
    payload = validated_json_payload(TermsPayload)
    if not isinstance(payload, dict):
        return payload
    SalesPayTermsService.add_terms(
        agent_user_id,
        effective_month=local_windows.parse_month(payload["effective_month"]),
        base_salary=payload["base_salary"],
        plan_id=payload["plan_id"],
        note=payload.get("note"),
        actor_id=_actor_id(),
    )
    return success_response(data=to_wire(SalesPayTermsService.agent_terms(agent_user_id)), status_code=201)


@admin_sales_pay_bp.route("/sales/pay/agents/<int:agent_user_id>/employment", methods=["PUT"])
@handle_api_exception
@jwt_required()
@super_admin_required
def set_agent_employment(agent_user_id):
    payload = validated_json_payload(EmploymentPayload)
    if not isinstance(payload, dict):
        return payload
    SalesPayTermsService.set_employment(
        agent_user_id, start=payload["start"], end=payload.get("end"), actor_id=_actor_id()
    )
    return success_response(data=to_wire(SalesPayTermsService.agent_terms(agent_user_id)))


# --- Penalty types (A19-A21): admins only (§5.2) ---


@admin_sales_pay_bp.route("/sales/pay/penalty-types", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def list_pay_penalty_types():
    types = SalesPayPenaltyService.list_types()
    return success_response(data=to_wire({"items": [serialize_penalty_type(t, with_amount=True) for t in types]}))


@admin_sales_pay_bp.route("/sales/pay/penalty-types", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def create_pay_penalty_type():
    payload = validated_json_payload(PenaltyTypeCreatePayload)
    if not isinstance(payload, dict):
        return payload
    penalty_type = SalesPayPenaltyService.create_type(payload["names"], payload["default_amount"], actor_id=_actor_id())
    return success_response(
        data=to_wire({"type": serialize_penalty_type(penalty_type, with_amount=True)}), status_code=201
    )


@admin_sales_pay_bp.route("/sales/pay/penalty-types/<int:type_id>", methods=["PATCH"])
@handle_api_exception
@jwt_required()
@super_admin_required
def update_pay_penalty_type(type_id):
    payload = validated_json_payload(PenaltyTypeUpdatePayload)
    if not isinstance(payload, dict):
        return payload
    penalty_type = SalesPayPenaltyService.update_type(type_id, actor_id=_actor_id(), **payload)
    return success_response(data=to_wire({"type": serialize_penalty_type(penalty_type, with_amount=True)}))


# --- Penalties (A22-A26): admins only (§5.2) ---


def _penalty_response(penalty, *, status_code: int = 200):
    return success_response(
        data=to_wire({"penalty": serialize_admin_penalty_row(penalty, now=local_windows.local_now())}),
        status_code=status_code,
    )


@admin_sales_pay_bp.route("/sales/pay/penalties", methods=["GET"])
@handle_api_exception
@jwt_required()
@super_admin_required
def list_pay_penalties():
    query = PenaltiesQuery.model_validate(request.args.to_dict())
    month = local_windows.parse_month(query.month) if query.month else None
    now = local_windows.local_now()
    # A pending row publishes where a confirm would land (`route_manual`), which reads the
    # incident month's period row; `confirm` opens every month up to now before it routes, and
    # so does this read, so the button and the write agree on the first day of a month.
    SalesPayPeriodService.ensure_periods(now)
    page = SalesPayPenaltyService.list(
        status=query.status, agent_user_id=query.agent_id, month=month, page=query.page, per_page=query.per_page
    )
    return success_response(
        data=to_wire(
            {
                "items": [serialize_admin_penalty_row(penalty, now=now) for penalty in page["items"]],
                "meta": pagination_meta(page["page"], page["per_page"], page["total"]),
                "statuses": list(SALES_PAY_PENALTY_STATUSES),
            }
        )
    )


@admin_sales_pay_bp.route("/sales/pay/penalties", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def create_pay_penalty():
    payload = validated_json_payload(PenaltyCreatePayload)
    if not isinstance(payload, dict):
        return payload
    penalty = SalesPayPenaltyService.create_confirmed(
        payload["agent_user_id"],
        payload["penalty_type_id"],
        payload["incident_date"],
        payload["reason"],
        payload["evidence"],
        payload.get("amount"),
        actor_id=_actor_id(),
    )
    return _penalty_response(penalty, status_code=201)


@admin_sales_pay_bp.route("/sales/pay/penalties/<int:penalty_id>/confirm", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def confirm_pay_penalty(penalty_id):
    payload = validated_json_payload(PenaltyConfirmPayload)
    if not isinstance(payload, dict):
        return payload
    return _penalty_response(
        SalesPayPenaltyService.confirm(penalty_id, actor_id=_actor_id(), amount=payload.get("amount"))
    )


@admin_sales_pay_bp.route("/sales/pay/penalties/<int:penalty_id>/reject", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def reject_pay_penalty(penalty_id):
    payload = validated_json_payload(PenaltyNotePayload)
    if not isinstance(payload, dict):
        return payload
    return _penalty_response(SalesPayPenaltyService.reject(penalty_id, actor_id=_actor_id(), note=payload["note"]))


@admin_sales_pay_bp.route("/sales/pay/penalties/<int:penalty_id>/cancel", methods=["POST"])
@handle_api_exception
@jwt_required()
@super_admin_required
def cancel_pay_penalty(penalty_id):
    payload = validated_json_payload(PenaltyNotePayload)
    if not isinstance(payload, dict):
        return payload
    return _penalty_response(SalesPayPenaltyService.cancel(penalty_id, actor_id=_actor_id(), note=payload["note"]))


# --- Penalty proposals (M1-M2): managers and admins, amount-free (§5.3) ---


@admin_sales_pay_bp.route("/sales/penalty-proposals", methods=["GET"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def list_penalty_proposals():
    # Proposals only: a direct booking is the admin's own (review gaming F9). Every row and type
    # goes through the manager serializers, which have no pay key to leak (C11, T-VISIB-2).
    query = ProposalsQuery.model_validate(request.args.to_dict())
    page = SalesPayPenaltyService.list(
        status=query.status, agent_user_id=query.agent_id, origin="proposal", page=query.page, per_page=query.per_page
    )
    types = SalesPayPenaltyService.list_types(active_only=True)
    return success_response(
        data=to_wire(
            {
                "items": [serialize_penalty_proposal(penalty) for penalty in page["items"]],
                "meta": pagination_meta(page["page"], page["per_page"], page["total"]),
                "statuses": list(SALES_PAY_PENALTY_STATUSES),
                "types": [serialize_penalty_type(penalty_type, with_amount=False) for penalty_type in types],
            }
        )
    )


@admin_sales_pay_bp.route("/sales/penalty-proposals", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def create_penalty_proposal():
    payload = validated_json_payload(ProposalPayload)
    if not isinstance(payload, dict):
        return payload
    penalty = SalesPayPenaltyService.propose(
        payload["agent_user_id"],
        payload["penalty_type_id"],
        payload["incident_date"],
        payload["reason"],
        payload["evidence"],
        actor_id=_actor_id(),
    )
    return success_response(data=to_wire({"proposal": serialize_penalty_proposal(penalty)}), status_code=201)
