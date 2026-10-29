"""
Subscriptions API endpoints
This file should be placed in business_app/api/subscriptions.py
"""

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from business_app.utils.service_factory import get_subscription_service, get_notification_service
from business_app.utils.helpers import get_current_language
from business_app.utils.local_windows import local_date
from business_app.utils.translations import get_translation
from business_app.serializers.subscription_serializers import (
    CLEARABLE_SUBSCRIPTION_FIELDS,
    serialize_subscription,
    serialize_subscription_item,
    serialize_subscription_log,
    CreateSubscriptionRequest,
    UpdateSubscriptionRequest,
    PauseSubscriptionRequest,
    CancelSubscriptionRequest,
    AddSubscriptionItemRequest,
    UpdateSubscriptionItemRequest,
    SubscriptionPreviewRequest,
    SubscriptionPreviewResponse,
    ChangePaymentMethodRequest,
    SkipDeliveryRequest,
    ReplaceSubscriptionItemsRequest,
    SubscriptionChangePreviewRequest,
)
from business_app.utils.constants import NotificationType
from business_app.utils.exceptions import NotFoundError, ValidationError, ConflictError, StateChangedError
from business_app.utils.pydantic_helpers import dump_changes, validate_json_with_model, serialize_response
from business_app.utils.api_responses import (
    success_response,
    error_response,
    paginated_response,
    created_response,
    not_found_response,
    conflict_response,
    internal_error_response,
    state_changed_response,
)
from business_app import db

subscriptions_bp = Blueprint("subscriptions", __name__)

# A refused request body answers with this, translated, as its `message` (spec 2026-10-26 §5 B12).
_INVALID_BODY = "api.subscriptions.error.validation_failed"


def _refused(exc):
    """A refused request: nothing it changed stays, and it answers the translated reason (404 not found,
    409 conflict, otherwise 400). A stale screen answers 409 STATE_CHANGED (spec 2026-10-27 §16 M2)."""
    db.session.rollback()
    if isinstance(exc, StateChangedError):
        return state_changed_response()
    if isinstance(exc, NotFoundError):
        return not_found_response(get_translation(exc.message))
    if isinstance(exc, ConflictError):
        return conflict_response(get_translation(exc.message))
    return error_response(get_translation(exc.message), status_code=400)


def _page_args(default_per_page: int = 20, max_per_page: int = 50):
    """`?page=` and `?per_page=` as positive whole numbers; anything else is the client's mistake (400)."""
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError) as exc:
        raise ValidationError("api.subscriptions.error.invalid_page") from exc
    if page < 1 or per_page < 1:
        raise ValidationError("api.subscriptions.error.invalid_page")
    return page, min(per_page, max_per_page)


@subscriptions_bp.route("/", methods=["GET"])
@jwt_required()
def get_subscriptions():
    """Get user's subscriptions with filtering and pagination"""
    try:
        current_user_id = get_jwt_identity()

        # Get query parameters
        page, per_page = _page_args()
        status = request.args.get("status")
        frequency = request.args.get("frequency")

        pagination = get_subscription_service().get_user_subscriptions_paginated(
            user_id=current_user_id,
            page=page,
            per_page=per_page,
            status=status,
            frequency=frequency,
        )

        return paginated_response(
            # Each card carries its items, so the page never fetches a detail per card (spec 2026-10-26 B5).
            items=[serialize_subscription(sub, include_items=True) for sub in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={"subscriptions_key": "items"},
        )

    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get subscriptions error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_failed"))


@subscriptions_bp.route("/products", methods=["GET"])
@jwt_required(optional=True)
def get_subscription_products():
    """Every product a subscription can take, at its subscription price (spec 2026-10-26 B10): the
    /my-subscriptions picker, the homepage builder and the customer bot's picker. Unpaged. A token is optional:
    the builder lists products before sign-in, and `product_catalog()` takes no user (the price is the
    product's own), so a visitor gets the signed-in customer's list. Like the public `/products` list, no
    rate limit of its own."""
    try:
        return success_response(data={"items": get_subscription_service().product_catalog()})
    except Exception as e:
        current_app.logger.error(f"Get subscription products error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_failed"))


@subscriptions_bp.route("/<int:subscription_id>", methods=["GET"])
@jwt_required()
def get_subscription(subscription_id):
    """Get specific subscription details"""
    try:
        current_user_id = get_jwt_identity()
        details = get_subscription_service().get_subscription_details_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )
        # Its orders are `/history` (spec 2026-10-26 §12).
        return success_response(
            data={
                "subscription": serialize_subscription(details["subscription"], include_items=True),
                "billing_info": details["billing_info"],
            }
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_failed"))


@subscriptions_bp.route("/", methods=["POST"])
@jwt_required()
@validate_json_with_model(CreateSubscriptionRequest, message_key=_INVALID_BODY)
def create_subscription():
    """Create a new subscription"""
    try:
        current_user_id = get_jwt_identity()

        # Get validated data from the decorator
        validated_data = request.validated_json
        subscription = get_subscription_service().create_subscription_for_user(
            user_id=current_user_id,
            validated_data=validated_data,
        )
        current_app.logger.info(f"Create subscription: created subscription={subscription}")
        # Send confirmation notification
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.SUBSCRIPTION_CREATED,
            template_data={
                "subscription_name": subscription.get("name"),
                "billing_amount": subscription.get("billing_amount"),
                "frequency": subscription.get("frequency"),
                "next_delivery_date": subscription.get("next_delivery_date"),
            },
        )

        current_app.logger.info("Create subscription: SUCCESSFULL send_notification")

        return created_response(
            data={"subscription": subscription}, message=get_translation("api.subscriptions.created")
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        current_app.logger.warning(f"Create subscription validation error: {e}")
        return error_response(get_translation(e.message), status_code=400)
    except ValueError as e:
        db.session.rollback()
        current_app.logger.warning(f"Create subscription validation error: {e}")
        return error_response(get_translation("api.subscriptions.error.validation_failed"), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.create_failed"))


@subscriptions_bp.route("/<int:subscription_id>", methods=["PUT"])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionRequest, message_key=_INVALID_BODY)
def update_subscription(subscription_id):
    """Update subscription details. An address change also answers the orders that moved with it and the ones that
    still go to their old address, each with its reason (spec 2026-10-27 §7, §17 Q6)."""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        result = get_subscription_service().update_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            update_data=dump_changes(
                validated_data, clearable=CLEARABLE_SUBSCRIPTION_FIELDS, exclude={"expected_state"}
            ),
            expected_state=validated_data.expected_state,
        )

        return success_response(
            data={
                "subscription": serialize_subscription(result["subscription"]),
                "moved_orders": result["moved_orders"],
                "orders_at_old_address": result["orders_at_old_address"],
            },
            message=get_translation("api.subscriptions.updated"),
        )

    except ValidationError as e:
        # The refusal's code and details reach the client: SUBSCRIPTION_PAYMENT_METHOD_REQUIRED carries the new
        # address's payment menu, from which the customer chooses before saving again (B8 re-pick).
        db.session.rollback()
        return error_response(
            get_translation(e.message), status_code=400, data={"error_code": e.error_code, **(e.details or {})}
        )
    except (NotFoundError, StateChangedError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.update_failed"))


@subscriptions_bp.route("/<int:subscription_id>/pause", methods=["POST"])
@jwt_required()
@validate_json_with_model(PauseSubscriptionRequest, message_key=_INVALID_BODY)
def pause_subscription(subscription_id):
    """Pause a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        reason = validated_data.reason or get_translation("api.subscriptions.default_reason_customer_request")
        outcome = get_subscription_service().pause_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            reason=reason,
            expected_state=validated_data.expected_state,
        )
        subscription = outcome["subscription"]

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            "subscription_paused",
            template_data={
                "subscription_name": subscription.get_translated("name", language),
                "pause_reason": reason,
                "resume_date": get_translation("api.subscriptions.manual_resume_required"),
            },
        )

        return success_response(
            data={
                "subscription": serialize_subscription(subscription),
                "cancelled_orders": outcome["cancelled_orders"],
                "kept_orders": outcome["kept_orders"],
            },
            message=get_translation("api.subscriptions.paused"),
        )

    except (NotFoundError, ValidationError, StateChangedError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Pause subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.pause_failed"))


@subscriptions_bp.route("/<int:subscription_id>/resume", methods=["POST"])
@jwt_required()
def resume_subscription(subscription_id):
    """Resume a paused subscription"""
    try:
        current_user_id = get_jwt_identity()
        subscription = get_subscription_service().resume_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            "subscription_resumed",
            template_data={
                "subscription_name": subscription.get_translated("name", language),
                "next_delivery_date": subscription.next_delivery_date.isoformat(),
            },
        )

        return success_response(
            data={"subscription": serialize_subscription(subscription)},
            message=get_translation("api.subscriptions.resumed"),
        )

    except (NotFoundError, ValidationError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Resume subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.resume_failed"))


@subscriptions_bp.route("/<int:subscription_id>/cancel", methods=["POST"])
@jwt_required()
@validate_json_with_model(CancelSubscriptionRequest, message_key=_INVALID_BODY)
def cancel_subscription(subscription_id):
    """Cancel a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json

        reason = validated_data.reason or get_translation("api.subscriptions.default_reason_customer_request")
        # Always immediate (spec 2026-10-23 §4.6); a paid upcoming order still stays (§2.7).
        outcome = get_subscription_service().cancel_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            reason=reason,
            expected_state=validated_data.expected_state,
        )
        subscription = outcome["subscription"]

        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.SUBSCRIPTION_CANCELLED,
            template_data={
                "subscription_name": subscription.get_translated("name", language),
                "cancellation_reason": reason,
                "effective_date": local_date().isoformat(),
            },
        )

        return success_response(
            data={
                "subscription": serialize_subscription(subscription),
                "cancelled_orders": outcome["cancelled_orders"],
                "kept_orders": outcome["kept_orders"],
            },
            message=get_translation("api.subscriptions.cancelled"),
        )

    except (NotFoundError, ValidationError, StateChangedError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Cancel subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.cancel_failed"))


@subscriptions_bp.route("/<int:subscription_id>/items", methods=["GET"])
@jwt_required()
def get_subscription_items(subscription_id):
    """Get subscription items"""
    try:
        current_user_id = get_jwt_identity()
        items = get_subscription_service().get_subscription_items_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        return success_response(data={"items": [serialize_subscription_item(item) for item in items]})

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get subscription items error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_items_failed"))


@subscriptions_bp.route("/<int:subscription_id>/items", methods=["POST"])
@jwt_required()
@validate_json_with_model(AddSubscriptionItemRequest, message_key=_INVALID_BODY)
def add_subscription_item(subscription_id):
    """Add item to subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        language = get_current_language()
        item_result = get_subscription_service().add_subscription_item_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            product_id=validated_data.product_id,
            quantity=validated_data.quantity,
            special_instructions=validated_data.special_instructions,
            language=language,
        )
        item = item_result["item"]
        billing_amount = item_result["billing_amount"]

        item_response = item.to_dict()
        current_app.logger.info(f"Add subscription item SUCCESS: item_response: {item_response}")

        return created_response(
            data={"item": item_response, "new_billing_amount": float(billing_amount)},
            message=get_translation("api.subscriptions.item_added"),
        )

    except (NotFoundError, ValidationError, ConflictError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Add subscription item error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.add_item_failed"))


@subscriptions_bp.route("/<int:subscription_id>/items/<int:item_id>", methods=["PUT"])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionItemRequest, message_key=_INVALID_BODY)
def update_subscription_item(subscription_id, item_id):
    """Update subscription item quantity"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        update_result = get_subscription_service().update_subscription_item_for_user(
            subscription_id=subscription_id,
            item_id=item_id,
            user_id=current_user_id,
            quantity=validated_data.quantity,
            special_instructions=validated_data.special_instructions,
        )
        item = update_result["item"]
        billing_amount = update_result["billing_amount"]

        # Same shape as add_subscription_item. SubscriptionItemSchema requires a
        # `product_name` attribute the model does not have, so it fell back to a raw
        # attribute dump (SQLAlchemy MetaData included) and every update 500'd after
        # its commit.
        item_response = item.to_dict()

        return success_response(
            data={"item": item_response, "new_billing_amount": float(billing_amount)},
            message=get_translation("api.subscriptions.item_updated"),
        )

    except (NotFoundError, ValidationError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription item error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.update_item_failed"))


@subscriptions_bp.route("/<int:subscription_id>/items/<int:item_id>", methods=["DELETE"])
@jwt_required()
def remove_subscription_item(subscription_id, item_id):
    """Remove item from subscription"""
    try:
        current_user_id = get_jwt_identity()
        subscription = get_subscription_service().remove_subscription_item_for_user(
            subscription_id=subscription_id,
            item_id=item_id,
            user_id=current_user_id,
        )

        return success_response(
            data={"new_billing_amount": float(subscription.billing_amount)},
            message=get_translation("api.subscriptions.item_removed"),
        )

    except (NotFoundError, ValidationError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Remove subscription item error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.remove_item_failed"))


@subscriptions_bp.route("/<int:subscription_id>/items", methods=["PUT"])
@jwt_required()
@validate_json_with_model(ReplaceSubscriptionItemsRequest, message_key=_INVALID_BODY)
def replace_subscription_items(subscription_id):
    """Replace the whole item list in one transaction (spec 2026-10-26 §5 B7): the website's Items save."""
    try:
        subscription = get_subscription_service().replace_items_for_user(
            subscription_id=subscription_id,
            user_id=get_jwt_identity(),
            items=request.validated_json.model_dump()["items"],
            expected_state=request.validated_json.expected_state,
        )
        return success_response(
            data={"subscription": serialize_subscription(subscription, include_items=True)},
            message=get_translation("api.subscriptions.updated"),
        )
    except (NotFoundError, ValidationError, StateChangedError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Replace subscription items error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.update_failed"))


@subscriptions_bp.route("/<int:subscription_id>/preview", methods=["POST"])
@jwt_required()
@validate_json_with_model(SubscriptionChangePreviewRequest, message_key=_INVALID_BODY)
def preview_subscription_change(subscription_id):
    """What a schedule or items change would give, before Save (spec 2026-10-26 §5 B4). Writes nothing."""
    try:
        preview = get_subscription_service().preview_changes_for_user(
            subscription_id=subscription_id,
            user_id=get_jwt_identity(),
            changes=dump_changes(request.validated_json, clearable=CLEARABLE_SUBSCRIPTION_FIELDS),
        )
        return success_response(data={"preview": preview})
    except (NotFoundError, ValidationError) as e:
        return _refused(e)
    except Exception as e:
        current_app.logger.error(f"Preview subscription change error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.preview_failed"))


@subscriptions_bp.route("/<int:subscription_id>/history", methods=["GET"])
@jwt_required()
def get_subscription_history(subscription_id):
    """The Orders section: orders and skipped deliveries in one list, newest first (spec
    2026-10-26 §5 B6, §12). `meta.total` counts every row."""
    try:
        page, per_page = _page_args(default_per_page=10)
        rows, total = get_subscription_service().subscription_history_for_user(
            subscription_id=subscription_id,
            user_id=get_jwt_identity(),
            page=page,
            per_page=per_page,
        )
        return paginated_response(items=rows, page=page, per_page=per_page, total=total)
    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get subscription history error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_failed"))


@subscriptions_bp.route("/<int:subscription_id>/logs", methods=["GET"])
@jwt_required()
def get_subscription_logs(subscription_id):
    """Get subscription activity logs"""
    try:
        current_user_id = get_jwt_identity()
        page, per_page = _page_args()

        pagination = get_subscription_service().get_subscription_logs_paginated_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            page=page,
            per_page=per_page,
        )

        return paginated_response(
            items=[serialize_subscription_log(log) for log in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total,
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get subscription logs error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_logs_failed"))


@subscriptions_bp.route("/preview", methods=["POST"])
@jwt_required()
@validate_json_with_model(SubscriptionPreviewRequest, message_key=_INVALID_BODY)
def preview_subscription():
    """Preview subscription cost and details before creation"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json

        # Calculate subscription preview
        preview = get_subscription_service().calculate_subscription_preview(
            user_id=current_user_id,
            frequency=validated_data.frequency,
            items=validated_data.items,
            delivery_time_slot_id=validated_data.delivery_time_slot_id,
        )

        # Serialize the preview response using the response schema
        preview_response = serialize_response(preview, SubscriptionPreviewResponse)

        return success_response(
            data={"preview": preview_response}, message=get_translation("api.subscriptions.preview_calculated")
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except ValueError as e:
        current_app.logger.warning(f"Preview subscription validation error: {e}")
        return error_response(get_translation("api.subscriptions.error.validation_failed"), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Preview subscription error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.preview_failed"))


@subscriptions_bp.route("/statistics", methods=["GET"])
@jwt_required()
def get_subscription_statistics():
    """Get user's subscription statistics"""
    try:
        current_user_id = get_jwt_identity()
        language = get_current_language()
        statistics = get_subscription_service().get_subscription_statistics_for_user(
            user_id=current_user_id,
            language=language,
        )

        return success_response(data={"statistics": statistics})

    except Exception as e:
        current_app.logger.error(f"Get subscription statistics error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.get_statistics_failed"))


@subscriptions_bp.route("/<int:subscription_id>/skip-next-delivery", methods=["POST"])
@jwt_required()
@validate_json_with_model(SkipDeliveryRequest, message_key=_INVALID_BODY)
def skip_next_delivery(subscription_id):
    """Skip the next delivery for a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        # The date the customer's button named (spec 2026-10-26 §12): a skip that would now land elsewhere is
        # a 409 and skips nothing.
        skip_result = get_subscription_service().skip_next_delivery_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            expected_date=validated_data.expected_date,
            reason_code=validated_data.reason_code,
            reason_text=validated_data.reason_text,
        )
        subscription = skip_result["subscription"]
        reason = skip_result["reason"]
        current_next_delivery = skip_result["original_delivery_date"]
        new_next_delivery = skip_result["new_next_delivery_date"]
        new_next_iso = new_next_delivery.isoformat() if new_next_delivery else None

        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            "delivery_skipped",
            template_data=get_subscription_service().skip_notice_data(
                subscription, skipped_date=current_next_delivery, next_date=new_next_delivery, reason=reason
            ),
        )

        return success_response(
            data={
                "original_delivery_date": current_next_delivery.isoformat(),
                "new_next_delivery_date": new_next_iso,
                "cancelled_orders": skip_result["cancelled_orders"],
                "kept_orders": skip_result["kept_orders"],
            },
            message=get_translation("api.subscriptions.next_delivery_skipped"),
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except StateChangedError:
        # The date the button named is stale (spec 2026-10-26 §12, 2026-10-27 §16 M2). Nothing written.
        db.session.rollback()
        return state_changed_response()
    except (ValidationError, ConflictError) as e:
        # A refusal (400), or another conflict (409). Nothing written.
        db.session.rollback()
        status_code = 409 if isinstance(e, ConflictError) else 400
        return error_response(get_translation(e.message), status_code=status_code)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Skip next delivery error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.skip_failed"))


@subscriptions_bp.route("/<int:subscription_id>/change-payment-method", methods=["POST"])
@jwt_required()
@validate_json_with_model(ChangePaymentMethodRequest, message_key=_INVALID_BODY)
def change_payment_method(subscription_id):
    """Change payment method for subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        payment_change = get_subscription_service().change_payment_method_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            payment_method=validated_data.payment_method,
            expected_state=validated_data.expected_state,
        )
        subscription = payment_change["subscription"]

        return success_response(
            data={"subscription": serialize_subscription(subscription)},
            message=get_translation("api.subscriptions.payment_method_updated"),
        )

    except (NotFoundError, ValidationError, StateChangedError) as e:
        return _refused(e)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Change payment method error: {e}")
        return internal_error_response(get_translation("api.subscriptions.error.change_payment_failed"))
