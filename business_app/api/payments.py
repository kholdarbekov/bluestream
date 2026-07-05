"""
Payments API endpoints for the Water Business Platform
This file should be placed in business_app/api/payments.py
"""

import business_app.models.order as order_models
import business_app.models.payment as payment_models
import business_app.models.subscription as subscription_models
import business_app.models.user as user_models
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime, timedelta

try:
    from datetime import UTC
except ImportError:
    # For Python < 3.11
    from datetime import timezone

    UTC = timezone.utc

from business_app.utils.service_factory import get_payment_service, get_notification_service
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from shared.redis_keyspace import RedisKeyspace
from business_app.serializers.payment_serializers import (
    serialize_payment,
    serialize_credit_card,
    get_available_payment_methods,
)
from business_app.utils.decorators import validate_json, rate_limit
from business_app.utils.constants import PaymeErrors, PaymentMethodType
from shared.enums import PaymentStatus, PaymentMethod
from business_app.utils.validation_helpers import (
    validate_list_request_params,
    FilterValidator,
    PaginationHelper,
    StatusValidator,
)
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import ValidationError, NotFoundError, ProviderUnavailableError
from business_app.utils.prometheus_metrics import (
    record_webhook_received,
    record_webhook_failure,
    record_webhook_duplicate,
)
from business_app.utils.webhook_idempotency import (
    WebhookIdempotencyGuard,
    extract_webhook_request_id,
)
from business_app.tasks.payment_tasks import process_payment_verification, handle_payment_webhook
from business_app.utils.api_responses import (
    success_response,
    error_response,
    created_response,
    not_found_response,
    validation_error_response,
    internal_error_response,
)
from business_app import db

payments_bp = Blueprint("payments", __name__)

Payment = payment_models.Payment
CreditCard = payment_models.CreditCard
Order = order_models.Order
User = user_models.User
Subscription = subscription_models.Subscription


def _get_webhook_idempotency_guard(payment_service):
    """Build a WebhookIdempotencyGuard bound to the payment-service Redis client.

    Uses the nonce-tracking Redis client already initialised on ``PaymentService``
    so the dedup keys live on the same DB as the existing replay-protection
    state. Returns ``None`` if Redis is unavailable — availability of payment
    callbacks wins over strict dedup.
    """
    client = getattr(payment_service, "redis_client", None)
    if client is None:
        return None
    return WebhookIdempotencyGuard(client)


# PAY-006: per-provider global cap. 600/min lets Click/Payme retry aggressively
# during a reconnect storm without swamping the app; crosses into 503 only on
# a clear abuse pattern.
_PROVIDER_WEBHOOK_GLOBAL_LIMIT = 600  # requests per provider
_PROVIDER_WEBHOOK_WINDOW_SECONDS = 60


def _check_provider_webhook_rate_limit(provider_lc: str, redis_client):
    """Enforce a per-provider global webhook rate ceiling.

    Returns a Flask response when the limit is exceeded, otherwise ``None``.
    We use 503 + Retry-After because some payment providers treat 429 as
    permanent failure and stop retrying (losing real webhooks).

    ``redis_client`` is the pooled client owned by ``PaymentService`` (db 3,
    same DB as nonce / idempotency state). Pass ``None`` to skip rate limiting
    when Redis is unavailable — availability of payment callbacks wins over
    strict limiting.
    """
    if not provider_lc or redis_client is None:
        return None
    try:
        key = RedisKeyspace.webhook_provider_rate(provider_lc)
        # Use a sliding-window-ish counter: INCR + EXPIRE on first hit.
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, _PROVIDER_WEBHOOK_WINDOW_SECONDS)
        if count > _PROVIDER_WEBHOOK_GLOBAL_LIMIT:
            ttl = redis_client.ttl(key)
            retry_after = ttl if isinstance(ttl, int) and ttl > 0 else _PROVIDER_WEBHOOK_WINDOW_SECONDS
            current_app.logger.warning(f"Provider webhook rate limit hit (provider={provider_lc}, count={count})")
            response = jsonify(
                {
                    "error": "Provider webhook rate limit exceeded",
                    "provider": provider_lc,
                }
            )
            response.status_code = 503
            response.headers["Retry-After"] = str(retry_after)
            return response
    except Exception as exc:  # pragma: no cover — availability > strict limiting
        current_app.logger.warning(f"Provider webhook rate-limit check failed (provider={provider_lc}): {exc}")
    return None


@payments_bp.route("/methods", methods=["GET"])
@jwt_required()
def get_payment_methods():
    """Get available payment methods"""
    try:
        current_user_id = get_jwt_identity()
        from business_app.services.cash_collection_service import CashCollectionService

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message=get_translation("user_not_found"))

        # Get user's saved payment methods
        saved_cards = CreditCard.query.filter_by(user_id=current_user_id, is_active=True).all()

        available_methods = [
            {
                "method": method["method"],
                "name": method["display_name"],
                "icon_url": method["icon_url"],
                "description": method["description"],
                "is_active": method["is_active"],
                "supported_currencies": method["supported_currencies"],
            }
            for method in get_available_payment_methods()
            if method.get("is_active")
        ]

        cod_context = CashCollectionService().get_cod_restriction_context(current_user_id)
        if cod_context["cod_restricted"]:
            available_methods = [method for method in available_methods if method["method"] != "cash"]

        from business_app.services.corporate_contract_service import CorporateContractService

        if CorporateContractService().get_business_account_balances(user):
            available_methods.append(
                {
                    "method": "business_account",
                    "name": "Business Account",
                    "icon_url": None,
                    "description": "Charge the active corporate prepayment balance",
                    "is_active": True,
                    "is_default": True,
                    "supported_currencies": ["UZS"],
                }
            )

        return success_response(
            data={
                "available_methods": available_methods,
                "payment_restrictions": cod_context,
                "saved_cards": [serialize_credit_card(card) for card in saved_cards],
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get payment methods error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.get_methods_failed"))


@payments_bp.route("/cod/statement", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_cod_statement():
    """Get the authenticated user's COD receivable statement."""
    current_user_id = get_jwt_identity()
    from business_app.services.cash_collection_service import CashCollectionService

    statement = CashCollectionService().get_customer_cod_statement(current_user_id)
    return success_response(data=statement)


@payments_bp.route("/orders/<int:order_id>/timeline", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_order_payment_timeline(order_id):
    """Get payment timeline for one of the authenticated user's orders."""
    current_user_id = get_jwt_identity()
    order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()
    if not order:
        return not_found_response(message=get_translation("api.orders.not_found"))

    from business_app.services.cash_collection_service import CashCollectionService

    timeline = CashCollectionService().get_order_payment_timeline(order_id)
    return success_response(data=timeline)


@payments_bp.route("/create", methods=["POST"])
@jwt_required()
@validate_json(["order_id", "payment_method"])
def create_payment():
    """Create a new payment for an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        order_id = data.get("order_id")
        payment_method = data.get("payment_method")

        # Validate order
        order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()

        if not order:
            return not_found_response(message=get_translation("api.orders.not_found"))

        if order.is_paid:
            return error_response(message=get_translation("api.payments.error.already_paid"))

        # Check for existing pending payment for this order with same payment method
        # This prevents duplicate payments when user retries payment
        requested_payment_method = PaymentMethod(payment_method)
        existing_payment = Payment.query.filter(
            Payment.order_id == order_id,
            Payment.user_id == current_user_id,
            Payment.status == PaymentStatus.PENDING,
            Payment.payment_method == requested_payment_method,
        ).first()

        if existing_payment:
            current_app.logger.info(f"Reusing existing pending payment {existing_payment.id} for order {order_id}")
            # For cash payments, just return the existing payment
            if payment_method == "cash":
                return created_response(
                    data={
                        "payment": serialize_payment(existing_payment),
                        "message": get_translation("api.payments.cash_payment_created"),
                    }
                )
            # For card payments, generate a fresh payment link for the existing payment
            payment_link = get_payment_service().create_payment_link(existing_payment.id)
            return created_response(data={"payment": serialize_payment(existing_payment), "payment_link": payment_link})

        # Create new payment
        payment_data = {
            "order_id": order_id,
            "user_id": current_user_id,
            "amount": order.total_amount,
            "currency": "UZS",
            "payment_method": requested_payment_method,
            "description": f"Payment for order #{order.order_number}",
            "return_url": data.get("return_url"),
            "cancel_url": data.get("cancel_url"),
            "metadata": {"order_number": order.order_number, "customer_phone": order.user.phone},
        }

        # Use saved card if specified
        saved_card_id = data.get("saved_card_id")
        if saved_card_id:
            card = CreditCard.query.filter_by(id=saved_card_id, user_id=current_user_id, is_active=True).first()
            if card:
                payment_data["saved_card_id"] = saved_card_id

        payment = get_payment_service().create_payment(**payment_data)

        # For cash payments, mark as pending
        if payment_method == "cash":
            payment.status = PaymentStatus.PENDING
            db.session.commit()

            return created_response(
                data={
                    "payment": serialize_payment(payment),
                    "message": get_translation("api.payments.cash_payment_created"),
                }
            )

        # For card payments, get payment link
        payment_link = get_payment_service().create_payment_link(payment.id)

        return created_response(data={"payment": serialize_payment(payment), "payment_link": payment_link})

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Create payment error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.create_failed"))


@payments_bp.route("/subscription", methods=["POST"])
@jwt_required()
@validate_json(["subscription_id", "payment_method"])
def create_subscription_payment():
    """Create a payment for subscription billing"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        subscription_id = data.get("subscription_id")
        payment_method = data.get("payment_method")

        # Validate subscription
        subscription = Subscription.query.filter_by(id=subscription_id, user_id=current_user_id).first()

        if not subscription:
            return not_found_response(message=get_translation("api.payments.error.subscription_not_found"))

        # Create subscription payment
        payment_data = {
            "subscription_id": subscription_id,
            "user_id": current_user_id,
            "amount": subscription.billing_amount,
            "currency": "UZS",
            "payment_method": PaymentMethodType(payment_method),
            "description": f'Subscription payment for {subscription.get_translated("name", get_current_language())}',
            "is_recurring": True,
            "return_url": data.get("return_url"),
            "cancel_url": data.get("cancel_url"),
            "metadata": {
                "subscription_name": subscription.get_translated("name", get_current_language()),
                "billing_cycle": subscription.billing_cycle,
            },
        }

        payment = get_payment_service().create_payment(payment_data)
        payment_link = get_payment_service().get_payment_link(payment)

        return created_response(data={"payment": serialize_payment(payment), "payment_link": payment_link})

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Create subscription payment error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.subscription_create_failed"))


@payments_bp.route("/<int:payment_id>/status", methods=["GET"])
@jwt_required()
def get_payment_status(payment_id):
    """Get payment status"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(id=payment_id, user_id=current_user_id).first()

        if not payment:
            return not_found_response(message=get_translation("api.payments.error.payment_not_found"))

        # Update payment status from provider if pending
        if payment.status == PaymentStatus.PENDING:
            get_payment_service().update_payment_status(payment)

        return success_response(data={"payment": serialize_payment(payment)})

    except Exception as e:
        current_app.logger.error(f"Get payment status error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.get_status_failed"))


@payments_bp.route("/", methods=["GET"])
@jwt_required()
@handle_api_exception
def get_payments():
    """Get user payments with pagination"""
    # Validate request parameters using centralized validation
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=PaymentStatus,
        allow_date_filter=True,
        allow_future_dates=True,
    )

    # Validate payment method filter separately
    payment_method_str = request.args.get("payment_method")
    payment_method = StatusValidator.validate_status_enum(payment_method_str, PaymentMethodType, "payment_method")

    # Build query
    query = Payment.query.filter_by(user_id=params["user_id"])

    # Apply filters using centralized filter builders
    query = FilterValidator.build_status_filter_query(query, Payment.status, params.get("status"))

    query = FilterValidator.build_status_filter_query(query, Payment.payment_method, payment_method)

    query = FilterValidator.build_date_filter_query(
        query, Payment.created_at, params.get("start_date"), params.get("end_date")
    )

    # Order by creation date (newest first)
    query = query.order_by(Payment.created_at.desc())

    # Paginate
    pagination = query.paginate(page=params["page"], per_page=params["per_page"], error_out=False)

    # Build standardized pagination response
    response_data = PaginationHelper.build_pagination_response(pagination.items, pagination, serialize_payment)

    return success_response(
        data={"payments": response_data["items"], "pagination": response_data["pagination"]},
        message=get_translation("api.payments.retrieved"),
    )


@payments_bp.route("/<int:payment_id>/cancel", methods=["POST"])
@jwt_required()
def cancel_payment(payment_id):
    """Cancel a pending payment"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(id=payment_id, user_id=current_user_id).first()

        if not payment:
            return not_found_response(message=get_translation("api.payments.error.payment_not_found"))

        if payment.status != PaymentStatus.PENDING:
            return error_response(message=get_translation("api.payments.error.only_pending_cancellable"))

        # Cancel payment
        success = get_payment_service().cancel_payment(payment)

        if success:
            return success_response(
                data={"message": get_translation("api.payments.cancelled"), "payment": serialize_payment(payment)}
            )
        else:
            return internal_error_response(message=get_translation("api.payments.error.cancel_failed"))

    except Exception as e:
        current_app.logger.error(f"Cancel payment error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.cancel_failed"))


@payments_bp.route("/cards", methods=["GET"])
@jwt_required()
def get_saved_cards():
    """Get user's saved credit cards"""
    try:
        current_user_id = get_jwt_identity()

        cards = (
            CreditCard.query.filter_by(user_id=current_user_id, is_active=True)
            .order_by(CreditCard.created_at.desc())
            .all()
        )

        return success_response(data={"cards": [serialize_credit_card(card) for card in cards]})

    except Exception as e:
        current_app.logger.error(f"Get saved cards error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.get_cards_failed"))


@payments_bp.route("/tokenize", methods=["POST"])
@jwt_required()
@validate_json(["card_number", "expiry"])
@rate_limit(10, 60)
def tokenize_card():
    """
    Tokenize card via Payme and trigger SMS verification if needed.

    This endpoint creates a card token and automatically requests SMS verification
    if the card requires it (most cards do on first use).

    Request body:
        card_number: Card number (16 digits, spaces allowed)
        expiry: Expiry date in MM/YY or MMYY format
        save: (optional) Whether to save for recurring payments, default true

    Response:
        token: Card token for subsequent operations
        masked_number: Masked card number (e.g., "860006******6311")
        expire: Expiry in MM/YY format
        needs_verification: True if SMS verification is required
        masked_phone: Phone number where SMS was sent (if verification needed)
        wait_seconds: Seconds until verification code expires
        verification_sent: True if SMS was successfully sent
    """
    try:
        data = request.get_json()
        card_number = data.get("card_number", "").replace(" ", "")
        expiry = data.get("expiry", "").replace("/", "")  # Expecting MM/YY -> MMYY
        save = data.get("save", True)  # Default to saving card

        if len(expiry) != 4:
            return error_response(message=get_translation("error.validation.invalid_expiry_format_mm_yy"))

        payment_service = get_payment_service()
        token_data = payment_service.create_card_token_with_verification(card_number, expiry, save=save)

        return success_response(
            data={
                "token": token_data.get("token"),
                "masked_number": token_data.get("masked_number"),
                "expire": token_data.get("expire"),
                "needs_verification": token_data.get("needs_verification", True),
                "masked_phone": token_data.get("masked_phone"),
                "wait_seconds": token_data.get("wait_seconds", 60),
                "verification_sent": token_data.get("verification_sent", False),
                "recurrent": token_data.get("recurrent", False),
                "type": "payme",
            }
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Tokenization error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route("/cards", methods=["POST"])
@jwt_required()
@validate_json(["cardholder_name"])  # Removed card_number requirement here as we might use token
@rate_limit(5, 60)  # Limit card saves to 5 per minute per user
def save_card():
    """Save a new credit card with comprehensive validation"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        # Comprehensive input validation
        card_number = data.get("card_number", "").strip()
        card_token = data.get("card_token")
        expiry_month = data.get("expiry_month")
        expiry_year = data.get("expiry_year")
        cardholder_name = data.get("cardholder_name", "").strip()
        cvv = data.get("cvv", "").strip()  # Optional for validation
        is_default = data.get("is_default", False)

        # Validate required fields
        # If we have a token, card_number is treated as masked/optional or last 4 digits
        if not card_token and not card_number:
            return error_response(message=get_translation("error.validation.card_number_required"))

        if not cardholder_name:
            return error_response(message=get_translation("error.validation.cardholder_name_required"))

        # Validate expiry month and year data types
        try:
            expiry_month = int(expiry_month)
            expiry_year = int(expiry_year)
        except (ValueError, TypeError):
            return error_response(message=get_translation("error.validation.invalid_card_expiry"))

        # Validate boolean type for is_default
        if not isinstance(is_default, bool):
            return error_response(message=get_translation("error.validation.invalid_boolean"))

        # Build card data for validation and saving
        card_data = {
            "user_id": current_user_id,
            "card_token": card_token,
            "card_number": card_number,
            "expiry_month": expiry_month,
            "expiry_year": expiry_year,
            "cardholder_name": cardholder_name,
            "is_default": is_default,
        }

        # Add CVV for validation if provided (not stored)
        if cvv:
            card_data["cvv"] = cvv

        # Save card using payment service with comprehensive validation
        payment_service = get_payment_service()
        card = payment_service.save_card(card_data)

        # Log successful card save
        current_app.logger.info(f"Credit card saved successfully for user {current_user_id}")

        return created_response(
            data={"message": get_translation("api.payments.card_saved"), "card": serialize_credit_card(card)}
        )

    except ValidationError as e:
        # Handle validation errors from our card validation
        current_app.logger.warning(f"Card validation failed for user {current_user_id}: {e}")
        return validation_error_response(errors=str(e))
    except ValueError as e:
        # Handle other value errors
        current_app.logger.warning(f"Invalid card data for user {current_user_id}: {e}")
        return error_response(message=str(e))
    except Exception as e:
        # Handle unexpected errors
        current_app.logger.error(f"Unexpected error saving card for user {current_user_id}: {e}")
        return internal_error_response(message=get_translation("api.payments.error.save_card_failed"))


@payments_bp.route("/cards/<int:card_id>", methods=["DELETE"])
@jwt_required()
def delete_card(card_id):
    """Delete a saved credit card"""
    try:
        current_user_id = get_jwt_identity()

        # Use payment service to delete card with proper validation
        payment_service = get_payment_service()
        success = payment_service.delete_card(card_id, current_user_id)

        if success:
            return success_response(data={"message": get_translation("api.payments.card_deleted")})
        else:
            return internal_error_response(message=get_translation("api.payments.error.delete_card_failed"))

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Delete card error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.delete_card_failed"))


@payments_bp.route("/statistics", methods=["GET"])
@jwt_required()
def get_payment_statistics():
    """Get user's payment statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get("period", "year")  # month, quarter, year, all

        # Calculate date range
        now = datetime.now(UTC)
        if period == "month":
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "quarter":
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # all time
            start_date = None

        # Base query
        query = Payment.query.filter_by(user_id=current_user_id)
        if start_date:
            query = query.filter(Payment.created_at >= start_date)

        payments = query.all()

        # Calculate statistics
        total_payments = len(payments)
        successful_payments = len([p for p in payments if p.status == PaymentStatus.COMPLETED])
        failed_payments = len([p for p in payments if p.status == PaymentStatus.FAILED])
        total_amount = sum(p.amount for p in payments if p.status == PaymentStatus.COMPLETED)

        # Payment methods breakdown
        method_stats = {}
        for method in PaymentMethodType:
            method_payments = [p for p in payments if p.payment_method == method]
            method_stats[method.value] = {
                "count": len(method_payments),
                "total_amount": sum(p.amount for p in method_payments if p.status == PaymentStatus.COMPLETED),
                "success_rate": (
                    (
                        len([p for p in method_payments if p.status == PaymentStatus.COMPLETED])
                        / len(method_payments)
                        * 100
                    )
                    if method_payments
                    else 0
                ),
            }

        # Monthly spending trend
        monthly_spending = {}
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            month_end = (
                month_start.replace(month=month_start.month % 12 + 1)
                if month_start.month < 12
                else month_start.replace(year=month_start.year + 1, month=1)
            )

            month_payments = [
                p for p in payments if month_start <= p.created_at < month_end and p.status == PaymentStatus.COMPLETED
            ]
            month_total = sum(p.amount for p in month_payments)

            monthly_spending[month_start.strftime("%Y-%m")] = month_total

        return success_response(
            data={
                "period": period,
                "statistics": {
                    "total_payments": total_payments,
                    "successful_payments": successful_payments,
                    "failed_payments": failed_payments,
                    "success_rate": round((successful_payments / total_payments * 100), 2) if total_payments > 0 else 0,
                    "total_amount": total_amount,
                    "average_payment": round(total_amount / successful_payments, 2) if successful_payments > 0 else 0,
                    "payment_methods": method_stats,
                    "monthly_spending_trend": monthly_spending,
                },
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get payment statistics error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.get_stats_failed"))


@payments_bp.route("/webhook/<provider>", methods=["POST"])
@rate_limit(60, 60)  # PAY-006: 60 webhook requests per minute per IP
def payment_webhook(provider):
    """Handle payment webhooks from providers with replay protection"""
    # Note: This endpoint must use jsonify() directly for provider-specific response formats
    # as payment providers (Payme, Click) expect specific JSON structures
    current_app.logger.info(f"/webhook/{provider} started with payload: {request.get_json(silent=True)}")
    provider_lc = provider.lower()
    idempotency_guard = None
    idempotency_request_id = None

    payment_service = get_payment_service()

    # PAY-006: per-provider global rate limit. Protects against a single
    # gateway retry storm exhausting worker capacity. We prefer 503 with
    # Retry-After over 429 because some gateways (Click, Payme) interpret
    # 429 as permanent failure and stop retrying legitimate webhooks.
    provider_limit_rejection = _check_provider_webhook_rate_limit(provider_lc, payment_service.redis_client)
    if provider_limit_rejection is not None:
        record_webhook_failure(provider_lc, "rate_limited")
        return provider_limit_rejection

    try:
        # Comprehensive webhook validation with replay protection
        if not payment_service.validate_webhook_signature(provider, request):
            record_webhook_failure(provider_lc, "signature_invalid")
            if provider_lc == "payme":
                try:
                    json_data = request.get_json(silent=True)
                    request_id = json_data.get("id") if isinstance(json_data, dict) else None
                except Exception:
                    request_id = None

                return (
                    jsonify(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32504, "message": "Insufficient privileges"},
                        }
                    ),
                    200,
                )
            if provider_lc == "click":
                return jsonify({"error": -1, "error_note": "Sign check failed"}), 200

            return jsonify({"error": "Invalid signature or replay detected"}), 401

        # Extract webhook data based on provider
        if provider_lc == "payme":
            webhook_data = request.get_json() or {}
        elif provider_lc == "click":
            webhook_data = dict(request.form) if request.form else request.get_json() or {}
        else:
            current_app.logger.error(f"Unsupported webhook provider: {provider}")
            record_webhook_failure(provider_lc, "unsupported")
            return jsonify({"error": "Unsupported provider"}), 400

        # PAY-002: endpoint-level idempotency keyed on gateway transaction id.
        # Protects against duplicate side effects (notifications, audit rows) from
        # concurrent or replayed webhook deliveries. The service methods below are
        # naturally idempotent by protocol, so caching the response also preserves
        # the exact reply expected by the gateway across retries.
        raw_body = request.get_data(cache=True) or b""
        idempotency_request_id = extract_webhook_request_id(provider_lc, webhook_data, raw_body)
        idempotency_guard = _get_webhook_idempotency_guard(payment_service)
        if idempotency_guard is not None:
            verdict = idempotency_guard.check(provider_lc, idempotency_request_id)
            if verdict.is_duplicate:
                record_webhook_duplicate(provider_lc)
                current_app.logger.info(
                    "Duplicate webhook suppressed " f"(provider={provider_lc}, request_id={idempotency_request_id})"
                )
                if verdict.cached_response is not None:
                    return jsonify(verdict.cached_response), 200
                # No cached response yet (first run still in flight or un-cached path):
                # return a minimal 200 so the gateway treats it as accepted.
                if provider_lc == "payme":
                    rpc_id = webhook_data.get("id") if isinstance(webhook_data, dict) else None
                    return jsonify({"jsonrpc": "2.0", "id": rpc_id, "result": {}}), 200
                if provider_lc == "click":
                    return jsonify({"error": 0, "error_note": "Already processed"}), 200
                return jsonify({"status": "duplicate"}), 200

        if provider_lc == "payme":
            # Payme REQUIRES synchronous response with JSON-RPC result
            response_data = payment_service.handle_payme_webhook(webhook_data)
            if idempotency_guard is not None and idempotency_request_id is not None:
                idempotency_guard.store_response(provider_lc, idempotency_request_id, response_data)
            record_webhook_received(provider_lc)
            return jsonify(response_data)

        if provider_lc == "click":
            # Click Prepare/Complete requires an immediate provider-formatted response.
            response_data = payment_service.handle_click_webhook(webhook_data)
            if idempotency_guard is not None and idempotency_request_id is not None:
                idempotency_guard.store_response(provider_lc, idempotency_request_id, response_data)
            record_webhook_received(provider_lc)
            return jsonify(response_data), 200

        webhook_metadata = {
            "provider": provider_lc,
            "webhook_data": webhook_data,
            "headers": dict(request.headers),
            "remote_addr": request.remote_addr,
            "received_at": datetime.now(UTC).isoformat(),
            "content_type": request.content_type,
            "request_id": idempotency_request_id,
        }
        handle_payment_webhook.delay(webhook_metadata, provider_lc)
        record_webhook_received(provider_lc)
        return jsonify({"status": "received"}), 200

    except ProviderUnavailableError as exc:
        # PAY-003: upstream provider unreachable (timeout/network/circuit open).
        # Surface as 503 + Retry-After so Click/Payme retry — never as 500/200,
        # which they treat as terminal. Release the idempotency claim so the
        # retry can re-enter the processing path on a fresh request id.
        record_webhook_failure(provider_lc, "exception")
        current_app.logger.warning(f"Provider unavailable on /webhook/{provider}: {exc}")
        if idempotency_guard is not None and idempotency_request_id is not None:
            idempotency_guard.release(provider_lc, idempotency_request_id)

        retry_after = exc.retry_after_seconds or 30
        if provider_lc == "payme":
            try:
                json_data = request.get_json(silent=True)
                request_id = json_data.get("id") if isinstance(json_data, dict) else None
            except Exception:
                request_id = None
            response = jsonify(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": PaymeErrors.INTERNAL_ERROR, "message": "Provider temporarily unavailable"},
                }
            )
        elif provider_lc == "click":
            response = jsonify({"error": -1, "error_note": "Provider temporarily unavailable"})
        else:
            response = jsonify(
                {
                    "error": "Provider temporarily unavailable",
                    "provider": provider_lc,
                }
            )
        response.status_code = 503
        response.headers["Retry-After"] = str(retry_after)
        return response

    except Exception as e:
        record_webhook_failure(provider_lc, "exception")
        current_app.logger.error(f"Payment webhook error for {provider}: {e}")

        # Release the idempotency claim so the gateway's retry can succeed.
        # We never stored a cached response for this request id, so re-running is safe.
        if idempotency_guard is not None and idempotency_request_id is not None:
            idempotency_guard.release(provider_lc, idempotency_request_id)

        # Log security incident
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
            action="webhook_processing_error",
            severity=AuditSeverity.HIGH,
            resource_type="payment_webhook",
            description=f"Webhook processing error for {provider}: {str(e)}",
            additional_data={"provider": provider, "error": str(e), "remote_addr": request.remote_addr},
        )

        # Return provider-specific error format
        if provider.lower() == "payme":
            try:
                json_data = request.get_json(silent=True)
                request_id = json_data.get("id") if isinstance(json_data, dict) else None
            except Exception:
                request_id = None

            return (
                jsonify(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": PaymeErrors.INTERNAL_ERROR, "message": "Server error"},
                    }
                ),
                200,
            )

        elif provider.lower() == "click":
            return jsonify({"error": -1, "error_note": "Internal server error"}), 500
        else:
            return jsonify({"error": "Webhook processing failed"}), 500


@payments_bp.route("/<int:payment_id>/verify", methods=["POST"])
@jwt_required()
def verify_payment(payment_id):
    """Manually verify payment status"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(id=payment_id, user_id=current_user_id).first()

        if not payment:
            return not_found_response(message=get_translation("api.payments.error.payment_not_found"))

        # Trigger payment verification
        process_payment_verification.delay(payment_id)

        return success_response(
            data={"message": get_translation("api.payments.verification_initiated"), "payment_id": payment_id}
        )

    except Exception as e:
        current_app.logger.error(f"Verify payment error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.verify_failed"))


@payments_bp.route("/refund", methods=["POST"])
@jwt_required()
@validate_json(["payment_id"])
def request_refund():
    """Request payment refund"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        payment_id = data.get("payment_id")
        reason = data.get("reason", get_translation("api.payments.refund_reason_customer_request"))

        payment = Payment.query.filter_by(id=payment_id, user_id=current_user_id).first()

        if not payment:
            return not_found_response(message=get_translation("api.payments.error.payment_not_found"))

        if payment.status != PaymentStatus.COMPLETED:
            return error_response(message=get_translation("api.payments.error.only_completed_refundable"))

        # Request refund
        refund = get_payment_service().request_refund(payment, reason)

        return created_response(
            data={
                "message": get_translation("api.payments.refund_requested"),
                "refund_id": refund.id,
                "status": refund.status.value if hasattr(refund.status, "value") else refund.status,
            }
        )

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Request refund error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.refund_failed"))


@payments_bp.route("/exchange-rates", methods=["GET"])
def get_exchange_rates():
    """Get current exchange rates (if supporting multiple currencies)"""
    try:
        # For now, only UZS is supported, but this endpoint can be extended
        rates = {
            "UZS": {
                "name": "Uzbek Som",
                "symbol": "AC<",
                "rate": 1.0,  # Base currency
                "updated_at": datetime.now(UTC).isoformat(),
            }
        }

        return success_response(data={"base_currency": "UZS", "rates": rates})

    except Exception as e:
        current_app.logger.error(f"Get exchange rates error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.get_rates_failed"))


# =============================================================================
# PAYME SUBSCRIBE API ENDPOINTS (Card verification and payment flow)
# =============================================================================


@payments_bp.route("/cards/create-token", methods=["POST"])
@jwt_required()
@validate_json(["card_number", "expiry"])
@rate_limit(5, 60)  # 5 requests per minute
def create_card_token():
    """
    Create card token via Payme and trigger SMS verification.

    This is an alias for /tokenize with a more RESTful path.
    Creates a card token and automatically requests SMS verification
    if the card requires it.

    Request body:
        card_number: Card number (16 digits, spaces allowed)
        expiry: Expiry date in MM/YY or MMYY format
        cardholder_name: (optional) Cardholder name
        save: (optional) Whether to save for recurring payments, default true

    Response:
        token: Card token for subsequent operations
        masked_number: Masked card number
        expire: Expiry in MM/YY format
        needs_verification: True if SMS verification is required
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until verification code expires
        verification_sent: True if SMS was successfully sent
    """
    try:
        data = request.get_json()
        card_number = data.get("card_number", "").replace(" ", "").replace("-", "")
        expiry = data.get("expiry", "").replace("/", "")
        save = data.get("save", False)

        if len(expiry) != 4:
            return error_response(message=get_translation("error.validation.invalid_expiry_format_mm_yy_or_mmyy"))

        payment_service = get_payment_service()
        token_data = payment_service.create_card_token_with_verification(card_number, expiry, save=save)

        return success_response(
            data={
                "token": token_data.get("token"),
                "masked_number": token_data.get("masked_number"),
                "expire": token_data.get("expire"),
                "needs_verification": token_data.get("needs_verification", True),
                "masked_phone": token_data.get("masked_phone"),
                "wait_seconds": token_data.get("wait_seconds", 60),
                "verification_sent": token_data.get("verification_sent", False),
                "recurrent": token_data.get("recurrent", False),
            }
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Create card token error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.create_card_token_failed"))


@payments_bp.route("/cards/send-verification", methods=["POST"])
@jwt_required()
@validate_json(["token", "order_id"])
@rate_limit(3, 60)  # 3 requests per minute (prevent SMS spam)
def send_verification_code():
    """
    Send SMS verification code for a card token after order creation.

    This endpoint should be called AFTER:
    1. cards.create (via /cards/create-token) - tokenizes the card
    2. Order creation (via /orders/) - creates order and payment record

    This triggers Payme's cards.get_verify_code to send SMS to cardholder.

    Request body:
        token: Card token from create-token endpoint
        order_id: Order ID to associate with this verification

    Response:
        sent: True if SMS was sent successfully
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until verification code expires
    """
    try:
        data = request.get_json()
        token = data.get("token")
        order_id = data.get("order_id")

        if not token:
            return error_response(message=get_translation("error.validation.card_token_required"))

        if not order_id:
            return error_response(message=get_translation("error.validation.order_id_required"))

        # Verify order exists and belongs to current user
        current_user_id = get_jwt_identity()
        order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()
        if not order:
            return not_found_response(message=get_translation("api.orders.not_found"))

        payment_service = get_payment_service()
        result = payment_service.request_card_verification_code(token)

        current_app.logger.info(f"Verification code sent for order {order_id}: {result.get('phone')}")

        return success_response(
            data={
                "sent": result.get("sent", False),
                "masked_phone": result.get("phone", ""),
                "wait_seconds": result.get("wait", 60000) // 1000,  # Convert ms to seconds
            }
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Send verification code error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.send_verification_failed"))


@payments_bp.route("/cards/resend-code", methods=["POST"])
@jwt_required()
@validate_json(["token"])
@rate_limit(3, 60)  # 3 requests per minute (prevent SMS spam)
def resend_verification_code():
    """
    Resend SMS verification code for a card token.

    Use this when the original code expires or user didn't receive it.
    Rate limited to 3 requests per minute to prevent SMS spam.

    Request body:
        token: Card token from create-token endpoint

    Response:
        sent: True if SMS was sent successfully
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until new code expires
    """
    try:
        data = request.get_json()
        token = data.get("token")

        if not token:
            return error_response(message=get_translation("error.validation.card_token_required"))

        payment_service = get_payment_service()
        result = payment_service.request_card_verification_code(token)

        return success_response(
            data={
                "sent": result.get("sent", False),
                "masked_phone": result.get("phone", ""),
                "wait_seconds": result.get("wait", 60000) // 1000,  # Convert ms to seconds
            }
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Resend verification code error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.resend_verification_failed"))


@payments_bp.route("/cards/verify", methods=["POST"])
@jwt_required()
@validate_json(["token", "code"])
@rate_limit(10, 60)  # 10 requests per minute
def verify_card_code():
    """
    Verify card with SMS code.

    After receiving the SMS code, call this endpoint to verify the card.
    Maximum 3 attempts per token - after that, request a new code.

    Request body:
        token: Card token from create-token endpoint
        code: Verification code from SMS (4-8 alphanumeric characters)

    Response (Success):
        verified: True
        card: Object with masked_number, expire

    Response (Wrong Code):
        success: false
        message: "Invalid verification code"
        data.attempts_remaining: Number of attempts left

    Response (Max Attempts):
        success: false
        message: "Too many failed attempts"
        data.request_new_code: true
    """
    try:
        get_jwt_identity()
        data = request.get_json()
        token = data.get("token")
        code = data.get("code")

        if not token:
            return error_response(message=get_translation("error.validation.card_token_required"))
        if not code:
            return error_response(message=get_translation("error.validation.verification_code_required"))

        payment_service = get_payment_service()

        try:
            result = payment_service.verify_card(token, code)

            return success_response(data={"verified": True, "card": result.get("card", {})})

        except ValidationError as e:
            error_msg = str(e)

            # Get actual attempts remaining from service (tracked in Redis)
            attempts_remaining = payment_service.get_verification_attempts_remaining(token)

            # Check if user needs to request a new code
            request_new_code = attempts_remaining <= 0 or "request a new code" in error_msg.lower()

            return error_response(
                message=error_msg,
                status_code=400,
                data={"attempts_remaining": attempts_remaining, "request_new_code": request_new_code},
            )

    except Exception as e:
        current_app.logger.error(f"Verify card error: {e}")
        return internal_error_response(message=get_translation("api.payments.error.verify_card_failed"))


@payments_bp.route("/process-card-payment", methods=["POST"])
@jwt_required()
@validate_json(["order_id", "token"])
@rate_limit(5, 60)  # 5 payment attempts per minute
def process_card_payment():
    """
    Process payment with a verified card token.

    This is the final step in the payment flow. The card must be verified
    (via /cards/verify) before calling this endpoint.

    Request body:
        order_id: Order ID to pay for
        token: Verified card token
        save_card: (optional) Whether to save card for future use, default true
        card_metadata: (optional) Card details for saving
            - masked_number: Masked card number
            - expire: Expiry date
            - cardholder_name: Cardholder name

    Response (Success):
        success: true
        payment_id: Our payment record ID
        order_id: Order ID
        receipt_id: Payme receipt ID
        amount: Amount paid
        redirect_url: URL to redirect user

    Response (Failure):
        success: false
        message: Error description
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        order_id = data.get("order_id")
        token = data.get("token")
        save_card = data.get("save_card", True)
        card_metadata = data.get("card_metadata", {})

        if not order_id:
            return error_response(message=get_translation("error.validation.order_id_required"))
        if not token:
            return error_response(message=get_translation("error.validation.card_token_required"))

        payment_service = get_payment_service()

        result = payment_service.process_payme_payment_full(
            order_id=order_id,
            card_token=token,
            user_id=current_user_id,
            save_card=save_card,
            card_metadata=card_metadata,
        )

        # Send notification on successful payment
        try:
            notification_service = get_notification_service()
            notification_service.send_payment_notification(result["payment_id"])
        except Exception as notify_error:
            current_app.logger.warning(f"Failed to send payment notification: {notify_error}")

        return success_response(
            data={
                "success": True,
                "payment_id": result["payment_id"],
                "order_id": result["order_id"],
                "receipt_id": result["receipt_id"],
                "amount": result.get("amount"),
                "redirect_url": result.get("redirect_url", f"/my-orders?order_id={order_id}&payment=success"),
            }
        )

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Process card payment error: {e}")
        return error_response(
            message=get_translation("api.payments.error.process_card_payment_failed"),
            status_code=500,
            data={
                "order_id": data.get("order_id") if "data" in dir() else None,
                "redirect_url": f"/checkout?error=payment_failed",  # noqa: F541
            },
        )
