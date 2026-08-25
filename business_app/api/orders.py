"""
Orders API endpoints
This file should be placed in business_app/api/orders.py
"""

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime, UTC, timedelta

from business_app.utils.service_factory import (
    get_order_service,
    get_delivery_service,
    get_notification_service,
    get_analytics_service,
    get_cart_service,
    get_payment_service,
)
from business_app.utils.translations import get_translation
from business_app.utils.payment_projection import is_settled_prepayment, order_is_resolved
from business_app.serializers.order_serializers import (
    serialize_order,
    serialize_order_delivery,
    serialize_delivery_slot,
    serialize_order_payment,
    CreateOrderRequest,
    OrderFeedbackRequest,
    CartEstimateRequest,
)
from business_app.utils.decorators import validate_json, rate_limit, require_verification
from business_app.utils.constants import NotificationType
from shared.enums import OrderStatus, PaymentMethod
from shared.status_transitions import order_transitions_as_strings
from business_app.utils.validation_helpers import validate_list_request_params
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import (
    ValidationError,
    NotFoundError,
    ForbiddenError,
    ConflictError,
    TaxCommitteeUnavailableError,
)
from business_app.utils.api_responses import (
    success_response,
    error_response,
    created_response,
    not_found_response,
    validation_error_response,
    forbidden_response,
    internal_error_response,
)
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from business_app import db
from pydantic import ValidationError as PydanticValidationError

orders_bp = Blueprint("orders", __name__)


def _rollback_session():
    db.session.rollback()


@orders_bp.route("/", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_orders():
    """Get user orders with pagination and filtering"""
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=OrderStatus,
        allow_date_filter=True,
        allow_future_dates=True,
    )

    status_value = params.get("status")
    if hasattr(status_value, "value"):
        status_value = status_value.value

    paginated = get_order_service().get_user_orders_paginated(
        user_id=params["user_id"],
        page=params["page"],
        per_page=params["per_page"],
        status=status_value,
        start_date=params.get("start_date"),
        end_date=params.get("end_date"),
    )

    pages = (paginated["total"] + params["per_page"] - 1) // params["per_page"] if params["per_page"] else 0
    pagination_data = {
        "page": params["page"],
        "pages": pages,
        "per_page": params["per_page"],
        "total": paginated["total"],
        "has_next": params["page"] < pages,
        "has_prev": params["page"] > 1,
    }

    return success_response(
        data={
            "orders": [
                serialize_order(order, include_items=True, include_payment=True) for order in paginated["items"]
            ],
            "pagination": pagination_data,
        },
        message=get_translation("api.orders.list_retrieved"),
    )


@orders_bp.route("/<int:order_id>", methods=["GET"])
@jwt_required()
def get_order(order_id):
    """Get specific order details"""
    try:
        current_user_id = get_jwt_identity()
        details = get_order_service().get_order_details_for_user(order_id, current_user_id)
        order = details["order"]
        delivery_info = serialize_order_delivery(details["delivery"]) if details["delivery"] else None
        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()
        payment_timeline = cash_collection_service.get_order_payment_timeline(
            order.id, viewer_user_id=int(current_user_id)
        )

        return success_response(
            data={
                "order": serialize_order(order, include_items=True, include_delivery=True, include_payment=True),
                "delivery": delivery_info,
                "timeline": details["timeline"],
                "payment_timeline": payment_timeline,
            },
            message=get_translation("api.orders.retrieved"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except Exception as e:
        current_app.logger.error(f"Get order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/emergency", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per="user")  # 3 emergency orders per hour per user
def create_emergency_order():
    """Create an emergency order with priority delivery"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            request_data = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        context = get_order_service().validate_user_emergency_order_access(current_user_id)
        user = context["user"]
        _, address = get_order_service().get_user_and_address_for_order(
            current_user_id,
            request_data.delivery_address_id,
        )
        if not address:
            return error_response(
                message=get_translation("api.addresses.not_found"),
                status_code=400,
            )

        # Emergency orders may carry an additional fee (env-driven; 0 by default
        # since delivery is currently free).
        emergency_fee = current_app.config["EMERGENCY_DELIVERY_FEE"]

        # Create order with emergency flag.
        order_data = {
            "items": request_data.items,
            "delivery_address": {
                "delivery_address_id": address.id,
                "street": address.street_address,
                "latitude": address.latitude,
                "longitude": address.longitude,
            },
            "delivery_notes": request_data.delivery_notes,
            "is_urgent": True,
            "payment_method": request_data.payment_method,
            "order_source": request_data.source,
            "emergency_fee": emergency_fee,
        }

        # Set delivery for within 2 hours
        emergency_delivery_time = datetime.now(UTC) + timedelta(hours=2)
        # Deliberately NO delivery_date: an emergency order must reach drivers
        # immediately. Dating it today would put it through the release gate, and
        # an emergency placed at 06:00 would be HELD until the 08:00 shift opens —
        # the exact opposite of what "emergency" means. A dateless order releases
        # at once, which is precisely today's behaviour.
        order_data["delivery_date"] = None

        order = get_order_service().create_order(current_user_id, order_data)

        # Create priority delivery
        delivery = get_delivery_service().create_emergency_delivery(order)

        # Immediate driver assignment
        auto_assign_delivery_task.apply_async(args=[delivery.id], countdown=30)

        # Notification failures should not fail order creation.
        try:
            get_notification_service().send_notification(
                user.id,
                NotificationType.ORDER_UPDATE,
                template_data={
                    "order_number": order.order_number,
                    "estimated_delivery": emergency_delivery_time.strftime("%H:%M"),
                },
            )
        except Exception as notification_error:
            current_app.logger.error(f"Emergency order notification failed: {notification_error}")

        return created_response(
            data={
                "order": serialize_order(order, include_items=True),
                "emergency_fee": emergency_fee,
                "estimated_delivery_time": emergency_delivery_time.isoformat(),
            },
            message=get_translation("api.orders.created"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("error.not_found"))
    except ForbiddenError:
        return forbidden_response(message=get_translation("error.forbidden"))
    except ConflictError:
        return error_response(message=get_translation("error.forbidden"), status_code=429)
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create emergency order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/quick-reorder", methods=["GET"])
@jwt_required()
def get_quick_reorder_suggestions():
    """Get quick reorder suggestions based on order history"""
    try:
        current_user_id = get_jwt_identity()
        limit = min(int(request.args.get("limit", 5)), 10)
        period_days = int(request.args.get("period_days", 90))

        # Use CartService to get quick reorder suggestions
        cart_service = get_cart_service()
        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=current_user_id, limit=limit, period_days=period_days
        )

        return success_response(
            data={"quick_reorder_suggestions": suggestions}, message=get_translation("success.saved")
        )

    except Exception as e:
        current_app.logger.error(f"Get quick reorder suggestions error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/statistics", methods=["GET"])
@jwt_required()
def get_order_statistics():
    """Get user's order statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get("period", "year")  # month, quarter, year, all
        result = get_order_service().get_user_order_statistics(current_user_id, period=period)
        return success_response(data=result, message=get_translation("success.saved"))

    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get order statistics error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/<int:order_id>/feedback", methods=["POST"])
@jwt_required()
def submit_order_feedback(order_id):
    """Submit feedback for a completed order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            feedback_data = OrderFeedbackRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        get_order_service().submit_order_feedback_for_user(
            order_id=order_id,
            user_id=current_user_id,
            rating=feedback_data.rating,
            comment=feedback_data.comment,
        )

        # Track feedback for analytics
        get_analytics_service().track_order_feedback(order_id, feedback_data.rating, feedback_data.comment)

        return success_response(message=get_translation("success.saved"))

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except (ValidationError, ConflictError) as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Submit order feedback error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/", methods=["POST"])
@jwt_required()
@require_verification("phone")
def create_order():
    """Create a new order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            order_request = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        _, address = get_order_service().get_user_and_address_for_order(
            current_user_id,
            order_request.delivery_address_id,
        )
        if not address:
            return error_response(message=get_translation("api.addresses.not_found"), status_code=400)

        # The web checkout posts a date and an open-ended window here. Same
        # helper as the admin create path — the customer-facing surface must not
        # get a second, laxer expression of the same rule.
        from business_app.utils.delivery_window import parse_and_validate_schedule

        delivery_date, window_start, window_end, schedule_errors = parse_and_validate_schedule(
            order_request.delivery_date,
            order_request.delivery_window_start,
            order_request.delivery_window_end,
        )
        if schedule_errors:
            return validation_error_response(schedule_errors)

        # Create order using service
        order_data = {
            "items": order_request.items,
            "delivery_address": {
                "delivery_address_id": order_request.delivery_address_id,
                "street": address.street_address,
                "longitude": address.longitude,
                "latitude": address.latitude,
            },
            "user_id": current_user_id,
            "delivery_date": delivery_date,
            "delivery_window_start": window_start,
            "delivery_window_end": window_end,
            "delivery_notes": order_request.delivery_notes,
            "is_urgent": order_request.is_urgent,
            "payment_method": order_request.payment_method,
            "loyalty_points_used": order_request.loyalty_points_used,
            "promo_code": order_request.promo_code,
            "reward_id": order_request.reward_id,
            "order_source": order_request.source,
        }

        order = get_order_service().create_order(current_user_id, order_data)
        current_app.logger.info(f"CREATE ORDER API: Order created successfully: order={order}")

        # Cart clearing is deferred until after Asl belgisi (Tax Committee)
        # pre-utilisation succeeds for card/click orders — otherwise a 503 from
        # the Tax Committee would leave the user with a cancelled order AND an
        # empty cart, with no way to retry. See clear_cart call below the
        # pre-utilisation block.

        # No delivery row here: a freshly created order is PENDING (unpaid for
        # card/click), and neither the driver broadcast nor the pool-insertion
        # evaluator look at the order's own status — only the delivery's.
        # Delivery creation happens at the CONFIRMED transition, via
        # OrderService._handle_status_change_actions, which already routes
        # through OrderScheduleService.ensure_delivery_if_due. Do not re-add
        # a create-delivery call here.

        current_app.logger.info(
            "CREATE ORDER API: order_number=%s, type=%s, total_amount=%s, type=%s",
            order.order_number,
            type(order.order_number),
            order.total_amount,
            type(order.total_amount),
        )

        current_app.logger.info("CREATE ORDER API: send_notification finished")

        response_data = {"order": serialize_order(order, include_items=True, include_payment=True)}

        payment_method_value = (
            order.payment_method.value if hasattr(order.payment_method, "value") else order.payment_method
        )

        # Pre-utilise marking codes for card/click payments so the Tax Committee
        # utilisation request happens before the user sees (and uses) the payment link.
        # When the proactive pool covers the order, fast_path=True and we skip the wait.
        pre_utilization_at = None
        pre_utilization_fast_path = False
        if payment_method_value in {"click", "card"} and getattr(order, "payment", None):
            from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

            try:
                pre_utilization_result = PaymentFiscalizationService().pre_utilise_marking_codes_for_payment(
                    order.payment
                )
                pre_utilization_at = pre_utilization_result.get("utilised_at")
                pre_utilization_fast_path = bool(pre_utilization_result.get("fast_path"))
            except TaxCommitteeUnavailableError as e:
                current_app.logger.error(f"CREATE ORDER API: Tax Committee unavailable for order {order.id}: {e}")
                # Cancel the order so inventory and marking codes are released cleanly.
                # Cart is intentionally NOT cleared here — the user can either retry
                # the card payment (uses cart again) or rescue with cash via
                # /api/v1/orders/<id>/retry-with-cash (uses this cancelled order_id).
                try:
                    get_order_service().cancel_order(order.id, reason="tax_committee_unavailable")
                except Exception:
                    _rollback_session()
                return error_response(
                    message=get_translation("api.orders.tax_committee_unavailable"),
                    status_code=503,
                    data={
                        "error_code": "ASL_BELGISI_UNAVAILABLE",
                        "cancelled_order_id": order.id,
                    },
                )
            except ValidationError:
                # Bubble up — caught by outer except ValidationError block
                raise

        # All payment-path-specific failures (Asl belgisi) have passed.
        # Safe to clear the cart now. Cash orders skip the pre-utilisation
        # block entirely, so they reach this point directly.
        try:
            get_cart_service().clear_cart(current_user_id)
            current_app.logger.info(f"CREATE ORDER API: Cart cleared for user {current_user_id}")
        except Exception as e:
            current_app.logger.error(f"CREATE ORDER API: Failed to clear cart: {e}")

        if payment_method_value in {"click", "card", "payme"} and getattr(order, "payment", None):
            payment_link = get_payment_service().create_payment_link(order.payment.id)
            response_data["payment_link"] = payment_link
            response_data["payment_url"] = (
                payment_link.get("payment_url") if isinstance(payment_link, dict) else payment_link
            )

        if pre_utilization_at is not None:
            response_data["pre_utilization_at"] = pre_utilization_at.isoformat()
            if pre_utilization_fast_path:
                # Pool covered the order — no wait, the bot delivers the link instantly.
                response_data["payment_ready_at"] = pre_utilization_at.isoformat()
            else:
                wait_seconds = int(current_app.config.get("PRE_PAYMENT_UTILISATION_WAIT_SECONDS", 45) or 45)
                response_data["payment_ready_at"] = (pre_utilization_at + timedelta(seconds=wait_seconds)).isoformat()

        if (order.payment_method.value if hasattr(order.payment_method, "value") else order.payment_method) == "cash":
            from business_app.services.cash_collection_service import CashCollectionService

            # Place-aware: report the cap against the address this order is
            # actually going to, not just the customer's own cluster (spec 5.5).
            response_data["payment_restrictions"] = CashCollectionService().get_cod_restriction_context(
                current_user_id, delivery_address_id=order.delivery_address_id
            )

        return created_response(data=response_data, message=get_translation("api.orders.created"))

    except NotFoundError:
        return not_found_response(message=get_translation("error.not_found"))
    except ValidationError as e:
        _rollback_session()
        return error_response(message=e.message, status_code=400)
    except PydanticValidationError as e:
        _rollback_session()
        return validation_error_response(e.errors())
    except ValueError as e:
        _rollback_session()
        current_app.logger.warning(f"Create order validation error: {e}")
        return error_response(message=get_translation("api.orders.error.invalid_request_data"), status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/<int:order_id>/retry-payment", methods=["POST"])
@jwt_required()
def retry_order_payment(order_id):
    """Create or refresh a payment link for an existing unpaid order."""
    try:
        current_user_id = get_jwt_identity()
        order = get_order_service().get_order(order_id, current_user_id)

        if getattr(order, "is_paid", False):
            return error_response(
                message=get_translation("api.payments.error.already_paid"),
                status_code=409,
            )

        # PAYABILITY GUARD (B3 fix round 1). This endpoint is the WEB twin of the
        # customer bot's Retry, fired by an ungated button on
        # `templates/frontend/payment_cancelled.html` via
        # `static/js/pages/payment-cancelled.js`, and it had no order-status test
        # at all. On a CANCELLED / RETURNED order it did not merely send the
        # customer to a checkout PREPARE would refuse with -9: `create_payment`
        # rewrites `payment.status = PENDING` and `payment.amount =
        # order.total_amount`, undoing the zeroing
        # `_sync_payment_status_for_terminal_order_state` applied when the order
        # died — so a dead order RE-APPEARED as owing money to
        # `open_receivable_amount`, on every debtor list and toward the COD cap.
        #
        # 🔴 The order-side half only, deliberately. `order_is_payable_online` is
        # the fuller authority and `order_is_resolved` is its documented ORDER
        # half (derived from it, not a second copy) — but the payment half also
        # requires the rail to be in FISCALIZED_RAILS, which excludes PAYME BY
        # CONSTRUCTION, while this endpoint's own whitelist below admits payme.
        # Gating on the full predicate would silently delete Payme retry, the
        # same narrowing the bot's cash rail would have suffered. The money bug
        # is entirely the dead-order cell, and this is exactly that cell.
        payment = getattr(order, "payment", None)
        if order_is_resolved(order):
            return error_response(
                message=get_translation("api.payments.error.lifecycle.advice_order_already_cancelled"),
                status_code=409,
            )
        # A settled prepayment whose order flag has not caught up: retrying
        # REWRITES a COMPLETED payment back to PENDING and mints a second link.
        # `is_settled_prepayment` is the same carve-out every read surface uses.
        if payment is not None and is_settled_prepayment(payment):
            return error_response(
                message=get_translation("api.payments.error.already_paid"),
                status_code=409,
            )

        payment_method_value = (
            order.payment_method.value if hasattr(order.payment_method, "value") else order.payment_method
        )
        if payment_method_value not in {"click", "card", "payme"}:
            return validation_error_response("Order does not have a retryable online payment method")

        payment_enum = PaymentMethod(payment_method_value)
        payment = get_payment_service().create_payment(
            order_id=order.id,
            payment_method=payment_enum,
            amount=order.total_amount,
            description=f"Payment for order #{order.order_number}",
        )
        payment_link = get_payment_service().create_payment_link(payment.id)

        return success_response(
            data={
                "payment": serialize_order_payment(payment),
                "payment_link": payment_link,
                "payment_url": payment_link.get("payment_url") if isinstance(payment_link, dict) else payment_link,
            },
            message=get_translation("api.payments.initiated"),
        )
    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Retry order payment error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/<int:order_id>/cancel", methods=["POST"])
@jwt_required()
def cancel_order(order_id):
    """Cancel an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json(silent=True) or {}

        order = get_order_service().cancel_order(
            order_id=order_id,
            user_id=current_user_id,
            reason=data.get("reason"),
        )

        # Send cancellation notification
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.ORDER_UPDATE,
            template_data={
                "order_number": order.order_number,
                "cancellation_reason": data.get("reason", "Customer request"),
            },
        )

        return success_response(data={"order": serialize_order(order)}, message=get_translation("api.orders.cancelled"))

    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except ConflictError:
        return error_response(message=get_translation("api.orders.cannot_cancel"), status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Cancel order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/cart/estimate", methods=["POST"])
@jwt_required()
def estimate_cart():
    """Estimate cart total with discounts and delivery fee"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            cart_request = CartEstimateRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        # Use CartService to calculate cart estimate
        cart_service = get_cart_service()
        estimate = cart_service.calculate_cart_estimate(
            user_id=current_user_id,
            items=[item.dict() for item in cart_request.items],
            delivery_address_id=cart_request.delivery_address_id,
            delivery_date=cart_request.delivery_date,
            loyalty_points_used=cart_request.loyalty_points_used,
            promo_code=cart_request.promo_code,
        )

        return success_response(data=estimate, message=get_translation("success.saved"))

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except NotFoundError:
        return not_found_response(message=get_translation("error.not_found"))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except ValueError as e:
        current_app.logger.warning(f"Estimate cart validation error: {e}")
        return error_response(message=get_translation("api.orders.error.invalid_request_data"), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Estimate cart error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/delivery-slots", methods=["GET"])
@jwt_required()
def get_delivery_slots():
    """Get available delivery time slots"""
    try:
        delivery_date = request.args.get("delivery_date")

        if not delivery_date:
            return error_response(message=get_translation("error.validation.required_field"), status_code=400)

        try:
            target_date = datetime.fromisoformat(delivery_date).date()
        except ValueError:
            return error_response(message=get_translation("error.validation.invalid_date"), status_code=400)

        # Get available time slots
        slots = get_delivery_service().get_available_time_slots(target_date)

        return success_response(
            data={
                "delivery_date": delivery_date,
                "available_slots": [serialize_delivery_slot(slot, target_date) for slot in slots],
            },
            message=get_translation("success.saved"),
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery slots error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/promo-code/validate", methods=["POST"])
@jwt_required()
@validate_json(["promo_code"])
def validate_promo_code():
    """Validate promotional code"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        promo_code = data.get("promo_code").upper()
        cart_total = data.get("cart_total", 0)

        # Use CartService to validate promo code
        cart_service = get_cart_service()
        validation_result = cart_service.validate_promo_code(
            promo_code=promo_code, user_id=current_user_id, cart_total=cart_total
        )

        if not validation_result["valid"]:
            return error_response(message=validation_result["message"], status_code=400)

        return success_response(data=validation_result, message=get_translation("success.saved"))

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Validate promo code error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/<int:order_id>/retry-with-cash", methods=["POST"])
@jwt_required()
def retry_order_with_cash(order_id):
    """Switch a PSP-cancelled order to cash payment.

    Used when the Tax Committee (Asl belgisi) was unavailable during the
    initial card/click order creation, cancelling the order. The bot's
    rescue UI calls this endpoint so the customer can complete the same
    order via cash even if they would normally be COD-restricted (e.g. 2+
    outstanding COD debts). The COD active-debt cap is intentionally
    bypassed here; the rescue is audited at the service layer.

    Implementation: clones the cancelled order's items into a fresh PENDING
    cash order via the canonical create_order pipeline, then mirrors the
    main create_order endpoint's downstream side-effects (cart clear) so the
    new order behaves identically to any other cash order from this point on.
    No delivery row is created here, for the same reason it isn't at the main
    create_order endpoint: the rescued order is PENDING, and delivery
    creation happens at the CONFIRMED transition via
    OrderService._handle_status_change_actions, which already routes through
    OrderScheduleService.ensure_delivery_if_due. Do not re-add a
    create-delivery call here.
    """
    try:
        current_user_id = get_jwt_identity()
        order = get_order_service().rescue_order_after_psp_failure(order_id, current_user_id)

        # Clear cart — cart was preserved by the original 503 path so the user
        # could retry. With a successful rescue, drop it so they don't see
        # stale items next time they open Products.
        try:
            get_cart_service().clear_cart(current_user_id)
        except Exception as e:
            current_app.logger.error(f"retry_order_with_cash: failed to clear cart: {e}")

        response_data = {"order": serialize_order(order, include_items=True, include_payment=True)}
        from business_app.services.cash_collection_service import CashCollectionService

        # Place-aware (spec 5.5). The rescue itself deliberately bypasses the cap
        # (see rescue_order_after_psp_failure), but the payload must still report
        # the real restriction state of the destination place.
        response_data["payment_restrictions"] = CashCollectionService().get_cod_restriction_context(
            current_user_id, delivery_address_id=order.delivery_address_id
        )
        return created_response(data=response_data, message=get_translation("api.orders.created"))
    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except ConflictError as e:
        _rollback_session()
        return error_response(message=e.message, status_code=409)
    except ValidationError as e:
        _rollback_session()
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.exception(f"retry_order_with_cash failed for order {order_id}: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/repeat/<int:order_id>", methods=["POST"])
@jwt_required()
def repeat_order(order_id):
    """Repeat a previous order"""
    try:
        current_user_id = get_jwt_identity()
        new_order = get_order_service().repeat_order_for_user(order_id, current_user_id)

        return created_response(
            data={"order": serialize_order(new_order, include_items=True)},
            message=get_translation("api.orders.created"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except ValidationError as e:
        current_app.logger.warning(f"Repeat order validation error: {e.message}")
        return error_response(message=e.message, status_code=400)
    except ValueError as e:
        current_app.logger.warning(f"Repeat order validation error: {e}")
        return error_response(message=get_translation("api.orders.error.invalid_request_data"), status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Repeat order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/<int:order_id>/track", methods=["GET"])
@jwt_required()
def track_order(order_id):
    """Track order status and delivery"""
    try:
        current_user_id = get_jwt_identity()
        tracking = get_order_service().get_order_tracking_for_user(order_id, current_user_id)
        order = tracking["order"]
        delivery_info = serialize_order_delivery(tracking["delivery"]) if tracking["delivery"] else None
        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()

        return success_response(
            data={
                "order": {
                    "id": order.id,
                    "order_number": order.order_number,
                    "status": order.status.value,
                    "total_amount": order.total_amount,
                    "created_at": order.created_at.isoformat(),
                    "payment_info": serialize_order_payment(order.payment) if getattr(order, "payment", None) else None,
                },
                "delivery": delivery_info,
                "timeline": tracking["timeline"],
                "estimated_time_remaining": tracking["estimated_time_remaining"],
                "payment_timeline": cash_collection_service.get_order_payment_timeline(
                    order.id, viewer_user_id=int(current_user_id)
                ),
            },
            message=get_translation("api.orders.retrieved"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("api.orders.not_found"))
    except Exception as e:
        current_app.logger.error(f"Track order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/bulk-action", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=300, per="user")  # 5 bulk actions per 5 minutes per user
def bulk_order_action():
    """Perform bulk action on multiple orders"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        action = data.get("action")
        order_ids = data.get("order_ids")

        if not isinstance(order_ids, list) or len(order_ids) > 100:
            return error_response(message=get_translation("error.forbidden"), status_code=400)

        valid_actions = ["confirm", "cancel", "mark_priority", "assign_delivery"]
        if action not in valid_actions:
            return error_response(message=get_translation("error.forbidden"), status_code=400)

        # Process bulk action
        results = get_order_service().perform_bulk_action(action, order_ids, current_user_id)

        return success_response(data={"results": results}, message=get_translation("success.updated"))

    except ForbiddenError:
        return forbidden_response(message=get_translation("error.forbidden"))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Bulk order action error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/export", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=600, per="user")  # 3 exports per 10 minutes per user
def export_orders():
    """Export orders to CSV/Excel"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = get_order_service().get_user_or_raise(current_user_id)

        # Regular users can only export their own orders
        if not user.is_admin:
            filters = {"user_id": current_user_id}
        else:
            filters = data.get("filters", {})

        format_type = data.get("format", "csv")  # csv, excel
        start_date = data.get("start_date")
        end_date = data.get("end_date")

        if format_type not in ["csv", "excel"]:
            return error_response(message=get_translation("error.forbidden"), status_code=400)

        # Generate export
        export_result = get_order_service().export_orders(
            format_type=format_type, filters=filters, start_date=start_date, end_date=end_date, user_id=current_user_id
        )

        return success_response(
            data={
                "download_url": export_result["download_url"],
                "file_size": export_result["file_size"],
                "expires_at": export_result["expires_at"].isoformat(),
            },
            message=get_translation("success.saved"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("error.not_found"))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Export orders error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/subscription", methods=["POST"])
@jwt_required()
def create_subscription_order():
    """Create a recurring subscription order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = get_order_service().get_user_or_raise(current_user_id)

        items_data = data.get("items", [])
        frequency = data.get("frequency")  # weekly, biweekly, monthly

        if frequency not in ["weekly", "biweekly", "monthly"]:
            return error_response(message=get_translation("error.forbidden"), status_code=400)

        # Create subscription order
        subscription_data = {
            "user_id": current_user_id,
            "delivery_address_id": data.get("delivery_address_id"),
            "frequency": frequency,
            "start_date": data.get("start_date"),
            "delivery_time_slot": data.get("delivery_time_slot"),
            "delivery_notes": data.get("delivery_notes"),
            "payment_method": data.get("payment_method"),
            "auto_pay": data.get("auto_pay", True),
        }

        subscription = get_order_service().create_subscription_order(subscription_data, items_data)
        subscription_id = subscription.get("id") if isinstance(subscription, dict) else subscription.id
        next_delivery = (
            subscription.get("next_delivery_date")
            if isinstance(subscription, dict)
            else (subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None)
        )
        status_value = subscription.get("status") if isinstance(subscription, dict) else subscription.status
        frequency_value = (
            subscription.get("delivery_frequency")
            if isinstance(subscription, dict)
            else subscription.delivery_frequency
        )

        # Send confirmation notification
        get_notification_service().send_notification(
            user.id,
            NotificationType.SUBSCRIPTION_CREATED,
            template_data={"subscription_id": subscription_id, "frequency": frequency, "next_delivery": next_delivery},
        )

        return created_response(
            data={
                "subscription": {
                    "id": subscription_id,
                    "frequency": frequency_value.value if hasattr(frequency_value, "value") else frequency_value,
                    "status": status_value.value if hasattr(status_value, "value") else status_value,
                    "next_delivery_date": next_delivery,
                    "created_at": (
                        subscription.get("created_at")
                        if isinstance(subscription, dict)
                        else subscription.created_at.isoformat()
                    ),
                }
            },
            message=get_translation("api.subscriptions.created"),
        )

    except NotFoundError:
        return not_found_response(message=get_translation("error.not_found"))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except ValueError as e:
        current_app.logger.warning(f"Create subscription order validation error: {e}")
        return error_response(message=get_translation("api.orders.error.invalid_request_data"), status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create subscription order error: {e}")
        return internal_error_response(message=get_translation("error.server_error"))


@orders_bp.route("/statuses", methods=["GET"])
def get_order_statuses():
    """
    Get all available order statuses and the allowed transitions between them.

    Single source of truth for the admin UI dropdown — sourced from
    `shared.status_transitions` so backend, bots, and UI stay in lockstep.

    Response shape:
        {
          "statuses": [{"value": "pending", "label": "Pending"}, ...],
          "transitions": {"pending": ["confirmed", "cancelled"], ...}
        }
    """
    statuses = [{"value": status.value, "label": status.value.replace("_", " ").title()} for status in OrderStatus]
    return success_response(
        data={
            "statuses": statuses,
            "transitions": order_transitions_as_strings(),
        },
        message=get_translation("api.orders.statuses_retrieved"),
    )


# ------------------------------------------------------------------
# Customer-facing bottle balance endpoints
# ------------------------------------------------------------------


@orders_bp.route("/bottles/my-balances", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_my_bottle_balances():
    """Bottle overview for the customer: all linked accounts' addresses,
    place unions + member breakdowns for grouped addresses (spec §7)."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    user_id = get_jwt_identity()
    overview = BottleTrackingService().get_customer_bottle_overview(int(user_id))
    return success_response(data=overview)


@orders_bp.route("/bottles/my-ledger/<int:address_id>", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_my_bottle_ledger(address_id):
    """Place ledger for an address the customer may see (three-arm gate,
    spec §7). 404 replaces the old silent-empty-200 for foreign addresses."""
    from business_app.serializers.bottle_serializers import serialize_customer_place_ledger_entry
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.services.customer_link_service import CustomerLinkService

    user_id = int(get_jwt_identity())
    if not CustomerLinkService().can_view_address_history(user_id, address_id):
        return not_found_response(message=get_translation("api.orders.not_found"))

    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 50)
    result = BottleTrackingService.get_place_ledger(address_id, page=page, per_page=per_page)
    return success_response(
        data={
            "items": [serialize_customer_place_ledger_entry(e, viewer_user_id=user_id) for e in result["items"]],
            "total": result["total"],
            "page": result["page"],
            "per_page": result["per_page"],
        }
    )
