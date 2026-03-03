"""Staff API endpoints for try-out tasks and bottle returns."""

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.tryout_serializers import CreateTryoutPayload, RecordPickupPayload
from business_app.services.tryout_service import TryoutService
from business_app.utils.api_responses import success_response, validation_error_response
from business_app.utils.decorators import require_staff_roles
from business_app.utils.error_handlers import handle_api_exception


staff_tryouts_bp = Blueprint("staff_tryouts", __name__)


def _current_actor_id() -> int:
    return int(get_jwt_identity())


def _validated_payload(schema_cls):
    payload = request.get_json() or {}
    try:
        return schema_cls(**payload).model_dump(exclude_none=True)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


@staff_tryouts_bp.route("/tryouts", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def create_staff_tryout():
    payload = _validated_payload(CreateTryoutPayload)
    if not isinstance(payload, dict):
        return payload

    payload["complete_handoff"] = True
    if not payload.get("assigned_driver_user_id"):
        payload["assigned_driver_user_id"] = _current_actor_id()

    tryout = TryoutService.create_tryout(payload, _current_actor_id(), source="driver")
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)}, status_code=201)


@staff_tryouts_bp.route("/tryout-tasks/pool", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_tryout_task_pool():
    items = [
        TryoutService.serialize_task(task)
        for task in TryoutService.list_tasks_for_driver(_current_actor_id(), include_pool=True)
    ]
    return success_response(data={"items": items, "total": len(items)})


@staff_tryouts_bp.route("/tryout-tasks/<int:task_id>/accept", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def accept_tryout_task(task_id: int):
    task = TryoutService.accept_task(task_id, _current_actor_id())
    return success_response(data={"task": TryoutService.serialize_task(task)})


@staff_tryouts_bp.route("/tryout-tasks/active", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_active_tryout_tasks():
    items = [
        TryoutService.serialize_task(task)
        for task in TryoutService.list_tasks_for_driver(_current_actor_id(), include_pool=False)
    ]
    return success_response(data={"items": items, "total": len(items)})


@staff_tryouts_bp.route("/tryouts/active", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_active_tryouts():
    items = [
        TryoutService.serialize_tryout(tryout)
        for tryout in TryoutService.list_active_tryouts_for_driver(_current_actor_id())
    ]
    return success_response(data={"items": items, "total": len(items)})


@staff_tryouts_bp.route("/tryouts/<int:tryout_id>", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver", "operator")
def get_staff_tryout(tryout_id: int):
    return success_response(data={"tryout": TryoutService.serialize_tryout(TryoutService.get_tryout(tryout_id))})


@staff_tryouts_bp.route("/tryout-tasks/<int:task_id>/complete-handoff", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def complete_staff_handoff(task_id: int):
    payload = request.get_json() or {}
    tryout = TryoutService.complete_handoff_task(task_id, _current_actor_id(), payload.get("notes"))
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)})


@staff_tryouts_bp.route("/tryout-tasks/<int:task_id>/record-pickup", methods=["POST"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def record_staff_pickup(task_id: int):
    payload = _validated_payload(RecordPickupPayload)
    if not isinstance(payload, dict):
        return payload

    tryout = TryoutService.record_pickup(
        task_id,
        payload["pickups"],
        _current_actor_id(),
        notes=payload.get("notes"),
        idempotency_key=payload.get("idempotency_key"),
    )
    return success_response(data={"tryout": TryoutService.serialize_tryout(tryout)})


@staff_tryouts_bp.route("/tryouts/history", methods=["GET"])
@handle_api_exception
@jwt_required()
@require_staff_roles("delivery_driver")
def get_tryout_history():
    items = [
        TryoutService.serialize_task(task)
        for task in TryoutService.list_history_for_driver(_current_actor_id())
    ]
    return success_response(data={"items": items, "total": len(items)})
