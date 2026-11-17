"""Admin price changes (spec 2026-11-16 §5.2): list, schedule, read, edit and cancel.

Thin: `PriceChangeService` owns every rule and write, `serialize_price_change` builds every published field. No
model access here (API boundary budget 0).
"""

from flask import Blueprint, g, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from business_app.serializers.price_change_serializers import PriceChangePayload, serialize_price_change
from business_app.serializers.price_courtesy_serializers import GrantPriceCourtesyPayload
from business_app.serializers.sales_pay_serializers import to_wire
from business_app.services.price_change.courtesy import customer_price_courtesy_view, grant_exception
from business_app.services.price_change.permissions import (
    PRICE_CHANGE_CANCEL_ANNOUNCED_PERMISSIONS,
    PRICE_CHANGE_MANAGE_PERMISSIONS,
    PRICE_CHANGE_VIEW_PERMISSIONS,
)
from business_app.services.price_change.report import granted_exceptions, price_change_counts, unreachable_customers
from business_app.services.price_change.schedule import PriceChangeService, create_action
from business_app.utils.api_responses import pagination_meta, success_response
from business_app.utils.constants import DEFAULT_PAGE_SIZE
from business_app.utils.decorators import (
    has_admin_permission,
    has_manager_or_higher_claim,
    manager_or_higher_required,
    staff_or_higher_required,
    validate_admin_action,
)
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.request_helpers import validated_json_payload
from business_app.utils.timezone_utils import get_utc_now

admin_price_changes_bp = Blueprint("admin_price_changes", __name__)


def _row(price_change):
    return serialize_price_change(price_change, viewer=g.current_user, now=get_utc_now())


@admin_price_changes_bp.route("/price-changes", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_VIEW_PERMISSIONS))
def list_price_changes():
    return success_response(
        data={
            "items": [_row(price_change) for price_change in PriceChangeService().list_all()],
            "allowed_actions": {"create": create_action(g.current_user)},
        }
    )


@admin_price_changes_bp.route("/price-changes", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_MANAGE_PERMISSIONS))
def create_price_change():
    payload = validated_json_payload(PriceChangePayload)
    if not isinstance(payload, dict):
        return payload
    price_change = PriceChangeService().create(payload, int(get_jwt_identity()))
    return success_response(data={"price_change": _row(price_change)}, status_code=201)


@admin_price_changes_bp.route("/price-changes/<int:price_change_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_VIEW_PERMISSIONS))
def get_price_change(price_change_id: int):
    price_change = PriceChangeService().get(price_change_id)
    return success_response(
        data=to_wire({"price_change": _row(price_change), "counts": price_change_counts(price_change.id)})
    )


@admin_price_changes_bp.route("/price-changes/<int:price_change_id>/unreachable", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_VIEW_PERMISSIONS))
def list_unreachable_customers(price_change_id: int):
    PriceChangeService().get(price_change_id)
    # Pre-parsed so `?page=abc` lands on the default (the reorder-reminders precedent).
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = max(request.args.get("per_page", DEFAULT_PAGE_SIZE, type=int), 1)
    items, total = unreachable_customers(price_change_id, page=page, per_page=per_page)
    return success_response(
        data=to_wire({"items": items, "meta": pagination_meta(page=page, per_page=per_page, total=total)})
    )


@admin_price_changes_bp.route("/price-changes/<int:price_change_id>/exceptions", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_VIEW_PERMISSIONS))
def list_granted_exceptions(price_change_id: int):
    PriceChangeService().get(price_change_id)
    return success_response(data=to_wire({"items": granted_exceptions(price_change_id)}))


@admin_price_changes_bp.route("/price-changes/<int:price_change_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_MANAGE_PERMISSIONS))
def update_price_change(price_change_id: int):
    payload = validated_json_payload(PriceChangePayload)
    if not isinstance(payload, dict):
        return payload
    price_change = PriceChangeService().update(price_change_id, payload, int(get_jwt_identity()))
    return success_response(data={"price_change": _row(price_change)})


@admin_price_changes_bp.route("/price-changes/<int:price_change_id>/cancel", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(list(PRICE_CHANGE_MANAGE_PERMISSIONS))
def cancel_price_change(price_change_id: int):
    price_change = PriceChangeService().cancel(
        price_change_id,
        int(get_jwt_identity()),
        actor_is_admin=has_admin_permission(g.current_user, PRICE_CHANGE_CANCEL_ANNOUNCED_PERMISSIONS),
    )
    return success_response(data={"price_change": _row(price_change)})


@admin_price_changes_bp.route("/customers/<int:user_id>/price-courtesy", methods=["GET"])
@handle_api_exception
@jwt_required()
@staff_or_higher_required
def get_customer_price_courtesy(user_id: int):
    return success_response(data=customer_price_courtesy_view(user_id, can_grant=has_manager_or_higher_claim()))


@admin_price_changes_bp.route("/customers/<int:user_id>/price-courtesy", methods=["POST"])
@handle_api_exception
@jwt_required()
@manager_or_higher_required
def grant_customer_price_courtesy(user_id: int):
    payload = validated_json_payload(GrantPriceCourtesyPayload)
    if not isinstance(payload, dict):
        return payload
    grant_exception(user_id, payload["reason"], int(get_jwt_identity()))
    return success_response(
        data=customer_price_courtesy_view(user_id, can_grant=has_manager_or_higher_claim()), status_code=201
    )
