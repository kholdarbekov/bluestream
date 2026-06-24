from flask import Blueprint, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.support_serializers import InboundSupportMessageRequest
from business_app.utils.api_responses import (
    internal_error_response,
    not_found_response,
    success_response,
    validation_error_response,
)
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.service_factory import get_support_conversation_service

support_bp = Blueprint("support", __name__)


@support_bp.route("/messages", methods=["POST"])
@jwt_required()
def create_support_message():
    """Record an inbound free-text message from the authenticated customer (bot calls this)."""
    try:
        data = request.get_json(silent=True) or {}
        payload = InboundSupportMessageRequest(**data)
        user_id = int(get_jwt_identity())
        msg = get_support_conversation_service().record_inbound_message(user_id, payload.content)
        return success_response(data=msg.to_dict(), message="Message received")
    except PydanticValidationError as exc:
        return validation_error_response(str(exc))
    except ValidationError as exc:
        return validation_error_response(str(exc))
    except NotFoundError as exc:
        return not_found_response(message=str(exc))
    except Exception:  # noqa: BLE001
        current_app.logger.exception("record inbound support message failed")
        return internal_error_response("Failed to record message")
