"""Admin API endpoints for try-out and bottle-custody workflows."""

import csv
from io import StringIO

from flask import Blueprint, Response, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.tryout_serializers import (
    BottleAdjustmentPayload,
    CreateTryoutPayload,
    CreateTryoutTaskPayload,
    RecordPickupPayload,
    UpdateTryoutPayload,
)
from business_app.services.tryout_service import AdminTryoutService, TryoutService
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import validate_admin_action
from business_app.utils.error_handlers import handle_api_exception


admin_tryouts_bp = Blueprint("admin_tryouts", __name__)


def _validated_payload(schema_cls):
    payload = request.get_json() or {}
    try:
        return schema_cls(**payload).model_dump(exclude_none=True)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


@admin_tryouts_bp.route("/tryouts", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_tryouts():
    result = AdminTryoutService.list_tryouts(
        page=request.args.get("page", 1, type=int),
        per_page=request.args.get("per_page", 20, type=int),
        search=request.args.get("search"),
        status=request.args.get("status"),
        outcome=request.args.get("outcome"),
        pickup_state=request.args.get("pickup_state"),
        driver_id=request.args.get("driver_id", type=int),
        start_date=request.args.get("start_date"),
        end_date=request.args.get("end_date"),
        due_start_date=request.args.get("due_start_date"),
        due_end_date=request.args.get("due_end_date"),
    )
    return success_response(data=result)


@admin_tryouts_bp.route("/tryouts", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def create_tryout():
    payload = _validated_payload(CreateTryoutPayload)
    if not isinstance(payload, dict):
        return payload

    actor_id = get_jwt_identity()
    tryout = TryoutService.create_tryout(payload, actor_id, source="admin")
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)}, status_code=201)


@admin_tryouts_bp.route("/tryouts/<int:tryout_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_tryout(tryout_id: int):
    return success_response(data={"tryout": AdminTryoutService.get_tryout(tryout_id)})


@admin_tryouts_bp.route("/tryouts/<int:tryout_id>", methods=["PUT"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def update_tryout(tryout_id: int):
    payload = _validated_payload(UpdateTryoutPayload)
    if not isinstance(payload, dict):
        return payload

    actor_id = get_jwt_identity()
    tryout = TryoutService.update_tryout(tryout_id, payload, actor_id)
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)})


@admin_tryouts_bp.route("/tryouts/<int:tryout_id>/convert", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def convert_tryout(tryout_id: int):
    actor_id = get_jwt_identity()
    result = TryoutService.convert_tryout(tryout_id, actor_id)
    user = result.get("user")
    return success_response(
        data={
            "tryout": TryoutService.serialize_tryout(result["tryout"]),
            "conversion": {
                "action": result.get("action"),
                "user": {
                    "id": user.id,
                    "full_name": user.full_name,
                    "phone": user.phone,
                } if user else None,
            },
        }
    )


@admin_tryouts_bp.route("/tryouts/<int:tryout_id>/tasks", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def create_tryout_task(tryout_id: int):
    payload = _validated_payload(CreateTryoutTaskPayload)
    if not isinstance(payload, dict):
        return payload

    actor_id = get_jwt_identity()
    task = TryoutService.create_task(tryout_id, payload, actor_id)
    return success_response(data={"task": TryoutService.serialize_task(task)}, status_code=201)


@admin_tryouts_bp.route("/tryout-tasks/<int:task_id>/assign", methods=["PUT"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def assign_tryout_task(task_id: int):
    payload = request.get_json() or {}
    driver_user_id = payload.get("assigned_driver_user_id")
    if not driver_user_id:
        return validation_error_response({"assigned_driver_user_id": ["assigned_driver_user_id is required"]})

    task = TryoutService.assign_task(task_id, int(driver_user_id))
    return success_response(data={"task": TryoutService.serialize_task(task)})


@admin_tryouts_bp.route("/tryout-tasks/<int:task_id>/complete", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def complete_tryout_task(task_id: int):
    actor_id = get_jwt_identity()
    payload = request.get_json() or {}
    task = TryoutService.get_task(task_id)
    task_type = task["task_type"]

    if task_type == "handoff":
        tryout = TryoutService.complete_handoff_task(task_id, actor_id, payload.get("notes"))
    else:
        pickup_payload = _validated_payload(RecordPickupPayload)
        if not isinstance(pickup_payload, dict):
            return pickup_payload
        tryout = TryoutService.record_pickup(
            task_id,
            pickup_payload["pickups"],
            actor_id,
            notes=pickup_payload.get("notes"),
            idempotency_key=pickup_payload.get("idempotency_key"),
        )
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)})


@admin_tryouts_bp.route("/tryouts/<int:tryout_id>/adjust-bottles", methods=["POST"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["manage_orders"])
def adjust_tryout_bottles(tryout_id: int):
    payload = _validated_payload(BottleAdjustmentPayload)
    if not isinstance(payload, dict):
        return payload

    actor_id = get_jwt_identity()
    tryout = TryoutService.adjust_bottles(
        tryout_id,
        payload["product_id"],
        payload["units"],
        actor_id,
        notes=payload.get("notes"),
        idempotency_key=payload.get("idempotency_key"),
    )
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)})


@admin_tryouts_bp.route("/tryouts/export", methods=["GET"])
@handle_api_exception
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def export_tryouts():
    rows = AdminTryoutService.export_tryouts(
        search=request.args.get("search"),
        status=request.args.get("status"),
        outcome=request.args.get("outcome"),
        pickup_state=request.args.get("pickup_state"),
        driver_id=request.args.get("driver_id", type=int),
        start_date=request.args.get("start_date"),
        end_date=request.args.get("end_date"),
        due_start_date=request.args.get("due_start_date"),
        due_end_date=request.args.get("due_end_date"),
    )

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "tryout_number",
            "status",
            "outcome",
            "contact_name",
            "phone",
            "source",
            "pickup_state",
            "outstanding_bottles_total",
            "return_due_at",
            "created_at",
            "converted_user_id",
        ],
    )
    writer.writeheader()
    for row in rows:
        contact = row.get("trial_contact") or {}
        writer.writerow(
            {
                "tryout_number": row.get("tryout_number"),
                "status": row.get("status"),
                "outcome": row.get("outcome"),
                "contact_name": contact.get("full_name"),
                "phone": contact.get("phone"),
                "source": row.get("source"),
                "pickup_state": row.get("pickup_state"),
                "outstanding_bottles_total": row.get("outstanding_bottles_total"),
                "return_due_at": row.get("return_due_at"),
                "created_at": row.get("created_at"),
                "converted_user_id": row.get("converted_user_id"),
            }
        )

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tryouts_export.csv"},
    )
