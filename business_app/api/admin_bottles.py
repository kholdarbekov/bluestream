"""Admin API endpoints for returnable bottle tracking."""

from datetime import date

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app import db
from business_app.serializers.bottle_serializers import (
    AdminForceCloseSessionRequest,
    AdminResolveTransferRequest,
    BottleAdjustmentRequest,
    BottleCollectionRequest,
    BottleFineCreateRequest,
    BottleFineUpdateRequest,
    BottleInitialBalanceRequest,
    serialize_bottle_balance_list,
    serialize_bottle_fine,
    serialize_bottle_ledger_entry,
    serialize_bottle_session,
    serialize_bottle_transfer,
)
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import validate_admin_action
from business_app.utils.error_handlers import handle_api_exception

admin_bottles_bp = Blueprint("admin_bottles", __name__)


def _validated_payload(schema_cls):
    payload = request.get_json() or {}
    try:
        return schema_cls(**payload).model_dump(exclude_none=True)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/dashboard", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def bottle_dashboard():
    """Get bottle tracking dashboard stats."""
    service = BottleTrackingService()
    stats = service.get_dashboard_stats()
    return success_response(data=stats)


# ------------------------------------------------------------------
# Balances
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/balances", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_bottle_balances():
    """Get paginated list of all bottle balances."""
    service = BottleTrackingService()
    result = service.get_all_balances(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        min_balance=request.args.get("min_balance", type=float),
        user_id=request.args.get("user_id", type=int),
        search=request.args.get("search"),
    )
    return success_response(data={
        "items": serialize_bottle_balance_list(result["items"], include_user=True),
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    })


@admin_bottles_bp.route("/bottles/balances/<int:user_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_customer_bottle_balances(user_id):
    """Get all bottle balances for a specific customer."""
    service = BottleTrackingService()
    summary = service.get_customer_summary(user_id)
    return success_response(data=summary)


# ------------------------------------------------------------------
# Ledger
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/ledger", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_bottle_ledger():
    """Get paginated ledger entries with optional filters."""
    service = BottleTrackingService()
    result = service.get_all_ledger_entries(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        user_id=request.args.get("user_id", type=int),
        address_id=request.args.get("address_id", type=int),
        event_type=request.args.get("event_type"),
    )
    result["items"] = [serialize_bottle_ledger_entry(e) for e in result["items"]]
    return success_response(data=result)


@admin_bottles_bp.route("/bottles/ledger/<int:user_id>/<int:address_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_bottle_ledger(user_id, address_id):
    """Get paginated ledger for a specific user+address."""
    service = BottleTrackingService()
    result = service.get_address_ledger(
        user_id=user_id,
        address_id=address_id,
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
    )
    return success_response(data=result)


# ------------------------------------------------------------------
# Adjustments & Initial Balance
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/adjustment", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def create_bottle_adjustment():
    """Admin manually adjusts a bottle balance."""
    data = _validated_payload(BottleAdjustmentRequest)
    if not isinstance(data, dict):
        return data  # validation error response

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    entry = service.admin_adjust_balance(
        user_id=data["user_id"],
        address_id=data["address_id"],
        adjustment=data["adjustment"],
        actor_user_id=current_user_id,
        notes=data["notes"],
    )
    db.session.commit()
    return success_response(data=entry.to_dict(), message="Balance adjusted successfully")


@admin_bottles_bp.route("/bottles/initial-balance", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def set_bottle_initial_balance():
    """Set initial bottle balance for a customer address."""
    data = _validated_payload(BottleInitialBalanceRequest)
    if not isinstance(data, dict):
        return data

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    entry = service.set_initial_balance(
        user_id=data["user_id"],
        address_id=data["address_id"],
        quantity=data["quantity"],
        actor_user_id=current_user_id,
        notes=data.get("notes"),
    )
    db.session.commit()
    return success_response(data=entry.to_dict(), message="Initial balance set successfully")


# ------------------------------------------------------------------
# Fines
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/fines", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_bottle_fines():
    """Get paginated list of bottle fines."""
    service = BottleTrackingService()
    result = service.get_all_fines(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        status=request.args.get("status"),
        user_id=request.args.get("user_id", type=int),
    )
    return success_response(data=result)


@admin_bottles_bp.route("/bottles/fines", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def create_bottle_fine():
    """Manually issue a fine for missing bottles."""
    data = _validated_payload(BottleFineCreateRequest)
    if not isinstance(data, dict):
        return data

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()

    bottle_balance_id = data.get("bottle_balance_id")
    if not bottle_balance_id:
        address_id = data.get("address_id")
        if not address_id:
            return validation_error_response(
                "Either bottle_balance_id or address_id is required"
            )
        balance = service.get_balance(data["user_id"], address_id)
        bottle_balance_id = balance.id

    fine = service.issue_fine(
        user_id=data["user_id"],
        bottle_balance_id=bottle_balance_id,
        quantity=data["quantity"],
        fine_amount=data["fine_amount"],
        actor_user_id=current_user_id,
        notes=data.get("notes"),
    )
    db.session.commit()
    return success_response(data=fine.to_dict(), message="Fine issued successfully")


@admin_bottles_bp.route("/bottles/fines/<int:fine_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def update_bottle_fine(fine_id):
    """Update a fine (waive or mark as paid)."""
    data = _validated_payload(BottleFineUpdateRequest)
    if not isinstance(data, dict):
        return data

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()

    if data["action"] == "waive":
        fine = service.waive_fine(fine_id, current_user_id, notes=data.get("notes"))
    else:
        fine = service.mark_fine_paid(fine_id, current_user_id, notes=data.get("notes"))

    db.session.commit()
    return success_response(data=fine.to_dict(), message="Fine updated successfully")


# ------------------------------------------------------------------
# Reconciliation
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/reconcile/<int:user_id>/<int:address_id>", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def reconcile_bottle_balance(user_id, address_id):
    """Recalculate a balance from ledger entries and fix discrepancy."""
    service = BottleTrackingService()
    result = service.reconcile_balance(user_id, address_id)
    db.session.commit()
    return success_response(data=result)


# ------------------------------------------------------------------
# Driver Bottle Sessions
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/sessions", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_bottle_sessions():
    """Get paginated driver bottle sessions with optional filters."""
    service = BottleTrackingService()

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    result = service.get_all_sessions(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        driver_user_id=request.args.get("driver_user_id", type=int),
        status=request.args.get("status"),
        only_discrepancies=request.args.get("only_discrepancies", "false").lower() == "true",
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    return success_response(data={
        "items": [serialize_bottle_session(s) for s in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "per_page": result["per_page"],
        "pages": result["pages"],
    })


@admin_bottles_bp.route("/bottles/sessions/<int:session_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_bottle_session(session_id):
    """Get full session detail including bound orders and transfers."""
    service = BottleTrackingService()
    session = service.get_session_detail(session_id)
    if not session:
        from business_app.utils.exceptions import NotFoundError
        raise NotFoundError("Bottle session not found")
    return success_response(data=serialize_bottle_session(
        session, include_orders=True, include_transfers=True, include_members=True
    ))


@admin_bottles_bp.route("/bottles/sessions/<int:session_id>/force-close", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def force_close_bottle_session(session_id):
    """Admin force-closes an abandoned open session."""
    data = _validated_payload(AdminForceCloseSessionRequest)
    if not isinstance(data, dict):
        return data

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    session = service.admin_force_close_session(
        session_id=session_id,
        actor_user_id=current_user_id,
        bottles_returned_to_warehouse=data.get("bottles_returned_to_warehouse", 0),
        reason=data["reason"],
    )
    db.session.commit()
    return success_response(
        data=serialize_bottle_session(session),
        message="Session force-closed successfully",
    )


# ------------------------------------------------------------------
# Driver Bottle Transfers
# ------------------------------------------------------------------

@admin_bottles_bp.route("/bottles/transfers", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_bottle_transfers():
    """Get paginated bottle transfers (filter by status, driver)."""
    service = BottleTrackingService()
    result = service.get_all_transfers(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        status=request.args.get("status"),
        driver_user_id=request.args.get("driver_user_id", type=int),
    )
    return success_response(data=result)


@admin_bottles_bp.route("/bottles/transfers/<int:transfer_id>/resolve", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def resolve_bottle_transfer_dispute(transfer_id):
    """Admin resolves a disputed driver-to-driver bottle transfer."""
    data = _validated_payload(AdminResolveTransferRequest)
    if not isinstance(data, dict):
        return data

    current_user_id = get_jwt_identity()
    service = BottleTrackingService()
    transfer = service.admin_resolve_transfer_dispute(
        transfer_id=transfer_id,
        actor_user_id=current_user_id,
        resolved_quantity=data["resolved_quantity"],
        resolution_notes=data["resolution_notes"],
    )
    db.session.commit()
    return success_response(
        data=serialize_bottle_transfer(transfer),
        message="Transfer dispute resolved successfully",
    )
