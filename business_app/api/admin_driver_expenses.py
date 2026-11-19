"""Admin review of driver cash expense claims (spec 2026-11-17 §6.4, §6.8): the Driver expenses page's list, one
claim, and the decision calls (preview, approve, reject, undo).

Admins and managers: every route is guarded by `DRIVER_EXPENSE_REVIEW_PERMISSIONS`, and the admin UI shows the page
and its badge by the same rule, the `can_review_driver_expenses` permission. No model access here (API boundary
budget 0): `DriverExpenseReview` builds every published field and `DriverExpenseDecision` makes every decision.
"""

from typing import Any

from flask import Blueprint, g, request
from flask_jwt_extended import jwt_required

from business_app.serializers.driver_expense_serializers import DriverExpenseListQuery
from business_app.services.driver_expense.decision import DriverExpenseDecision
from business_app.services.driver_expense.permissions import DRIVER_EXPENSE_REVIEW_PERMISSIONS
from business_app.services.driver_expense.receipt import stream_receipt
from business_app.services.driver_expense.review import DriverExpenseReview
from business_app.utils.api_responses import success_response
from business_app.utils.decorators import validate_admin_action
from business_app.utils.error_handlers import handle_api_exception

admin_driver_expenses_bp = Blueprint("admin_driver_expenses", __name__)


def _field(name: str) -> Any:
    """One field of the JSON object the page posts; None when the body is missing or not an object, so the service
    refuses it with that field's own code."""
    body = request.get_json(silent=True)
    return body.get(name) if isinstance(body, dict) else None


@admin_driver_expenses_bp.route("/driver-expenses", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def list_driver_expenses():
    params = DriverExpenseListQuery.model_validate(request.args.to_dict())
    return success_response(data=DriverExpenseReview.list_for_admin(params, g.current_user))


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def get_driver_expense(expense_id: int):
    return success_response(data=DriverExpenseReview.detail(expense_id, g.current_user))


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>/receipt", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def get_driver_expense_receipt(expense_id: int):
    # An expense id only: the Telegram file id is resolved from the row inside `stream_receipt`.
    return stream_receipt(expense_id)


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>/preview", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def preview_driver_expense_decision(expense_id: int):
    """The approve dialog's effect line for `{approved_amount}`; writes nothing."""
    return success_response(
        data=DriverExpenseDecision.preview(expense_id, _field("approved_amount"), g.current_user.id)
    )


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>/approve", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def approve_driver_expense(expense_id: int):
    return success_response(
        data=DriverExpenseDecision.approve(expense_id, _field("approved_amount"), g.current_user.id)
    )


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>/reject", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def reject_driver_expense(expense_id: int):
    return success_response(data=DriverExpenseDecision.reject(expense_id, _field("reason"), g.current_user.id))


@admin_driver_expenses_bp.route("/driver-expenses/<int:expense_id>/undo", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(DRIVER_EXPENSE_REVIEW_PERMISSIONS))
def undo_driver_expense_approval(expense_id: int):
    return success_response(data=DriverExpenseDecision.undo(expense_id, _field("reason"), g.current_user.id))
