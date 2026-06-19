"""
Admin API endpoints for the Water Business Platform
This file should be placed in business_app/api/admin.py
"""

from typing import List

from flask import Blueprint, request, current_app, g, Response
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func, text, cast, String
from sqlalchemy.exc import IntegrityError
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from shared.constants import DISPLAY_TIMEZONE, is_within_tashkent
from shared.redis_keyspace import RedisKeyspace

from business_app.models.user import User, UserAddress
from business_app.models.order import Order, OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.product import Product, ProductCategory, ProductSizeEnum
from business_app.models.payment import (
    CashCollectionEvent,
    Payment,
    PaymentFiscalization,
    PaymentTransaction,
)
from business_app.models.delivery import (
    Delivery,
    DeliveryPerson,
    DeliveryRoute,
    DeliveryTimeSlot,
)
from business_app.models.subscription import Subscription
from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyPoints, LoyaltyTransaction
from business_app.models.notification import NotificationTemplate
from business_app.models.analytics import PromotionalCampaign
from business_app.models.review import Review
from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity

# TranslatableContent replaced by unified Translation system
from business_app.models.translation import Translation

# from business_app.services.admin_service import AdminService
from business_app.utils.service_factory import get_notification_service, get_corporate_contract_service
from business_app.services.subscription_service import SubscriptionService
from business_app.services.payment_service import PaymentService
from business_app.services.review_service import ReviewService
from business_app.services.admin_report_service import AdminReportService
from business_app.services.admin_bulk_action_service import AdminBulkActionService
from business_app.services.admin_delivery_service import AdminDeliveryService
from business_app.services.admin_loyalty_service import AdminLoyaltyService
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.serializers.admin_serializers import (
    serialize_user_admin,
    serialize_order_admin,
    serialize_product_admin,
    serialize_delivery_person_admin,
    serialize_category_admin,
    InactiveCustomersQuerySchema,
)
from business_app.utils.service_factory import get_analytics_service
from pydantic import ValidationError as PydanticValidationError

# from business_app.services.file_storage_service import FileStorageService
from business_app.utils.decorators import (
    validate_json,
    admin_required,
    super_admin_required,
    manager_or_higher_required,
    staff_or_higher_required,
    validate_admin_action,
    rate_limit,
)
from business_app.utils.query_optimization import (
    get_users_with_stats,
    get_orders_with_details,
    get_payments_optimized,
    PaginationOptimizer,
    AggregationOptimizer,
)
from business_app.services.inventory_service import get_inventory_service, InventoryOperationType
from shared.enums import (
    UserRole,
    SubscriptionStatus,
    OrderStatus,
    DeliveryStatus,
    UserStatus,
    PaymentStatus,
)
from business_app import db
from business_app.utils.helpers import get_current_language
from business_app.utils.user_types import normalize_user_type
from business_app.utils.translations import get_translation
from business_app.utils.exceptions import ValidationError, ConflictError, NotFoundError, ForbiddenError
from business_app.utils.api_responses import (
    success_response,
    error_response,
    paginated_response,
    created_response,
    not_found_response,
    validation_error_response,
    internal_error_response,
    forbidden_response,
)
from business_app.utils.bot_webhook import trigger_translation_reload

admin_bp = Blueprint("admin", __name__)


def _is_operator_staff_member():
    """Return SQL filter for users that can operate via role or staff_roles."""
    return or_(User.role == UserRole.OPERATOR, cast(User.staff_roles, String).ilike('%"operator"%'))


def _serialize_corporate_contract(contract):
    payload = contract.to_dict()
    user = getattr(contract, "user", None)
    if user:
        payload["user"] = {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "email": user.email,
            "user_type": normalize_user_type(
                getattr(user, "user_type", None),
                role=getattr(user, "role", None),
                staff_roles=getattr(user, "staff_roles", None),
            ),
            "company_name": getattr(user, "company_name", None),
            "tax_id": getattr(user, "tax_id", None),
        }
    else:
        payload["user"] = None
    if contract.prepayment_account:
        payload["prepayment_account"] = contract.prepayment_account.to_dict()
    else:
        payload["prepayment_account"] = None
    payload["prices"] = [price.to_dict() for price in contract.product_prices]
    return payload


def _parse_iso_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    raise ValidationError("Invalid datetime format")


def _commit_db_session():
    getattr(db, "session").commit()


def _rollback_db_session():
    getattr(db, "session").rollback()


@admin_bp.route("/corporate/contracts", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def list_corporate_contracts():
    """List corporate contracts."""
    try:
        service = get_corporate_contract_service()
        user_id = request.args.get("user_id", type=int)
        status = request.args.get("status")
        search = request.args.get("search")
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)

        result = service.list_contracts(
            user_id=user_id,
            status=status,
            page=page,
            per_page=per_page,
            search=search,
        )
        summary = service.get_contracts_summary(user_id=user_id, status=status, search=search)
        return paginated_response(
            items=[_serialize_corporate_contract(item) for item in result["items"]],
            page=result["page"],
            per_page=result["per_page"],
            total=result["total"],
            additional_meta={"summary": summary},
        )
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"List corporate contracts error: {e}")
        return internal_error_response("Failed to list corporate contracts")


@admin_bp.route("/corporate/contracts", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def create_corporate_contract():
    """Create corporate contract."""
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}
        service = get_corporate_contract_service()

        contract = service.create_contract(payload, actor_user_id=current_user_id)
        _commit_db_session()

        return created_response(
            data={"contract": _serialize_corporate_contract(contract)},
            message="Corporate contract created successfully",
        )
    except ValidationError as e:
        _rollback_db_session()
        return validation_error_response(str(e))
    except NotFoundError as e:
        _rollback_db_session()
        return not_found_response(message=str(e))
    except Exception as e:
        _rollback_db_session()
        current_app.logger.error(f"Create corporate contract error: {e}")
        return internal_error_response("Failed to create corporate contract")


@admin_bp.route("/corporate/contracts/<int:contract_id>", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_corporate_contract(contract_id):
    """Get corporate contract details."""
    try:
        service = get_corporate_contract_service()
        contract = service.get_contract_by_id(contract_id)
        return success_response(data={"contract": _serialize_corporate_contract(contract)})
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get corporate contract error: {e}")
        return internal_error_response("Failed to get corporate contract")


@admin_bp.route("/corporate/contracts/<int:contract_id>", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
def update_corporate_contract(contract_id):
    """Update corporate contract."""
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}
        service = get_corporate_contract_service()

        contract = service.update_contract(
            contract_id=contract_id,
            payload=payload,
            actor_user_id=current_user_id,
        )
        _commit_db_session()

        return success_response(
            data={"contract": _serialize_corporate_contract(contract)},
            message="Corporate contract updated successfully",
        )
    except ValidationError as e:
        _rollback_db_session()
        return validation_error_response(str(e))
    except NotFoundError as e:
        _rollback_db_session()
        return not_found_response(message=str(e))
    except Exception as e:
        _rollback_db_session()
        current_app.logger.error(f"Update corporate contract error: {e}")
        return internal_error_response("Failed to update corporate contract")


@admin_bp.route("/corporate/contracts/<int:contract_id>/prices", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
def upsert_corporate_contract_prices(contract_id):
    """Bulk upsert corporate contract product prices."""
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}
        prices = payload if isinstance(payload, list) else payload.get("prices", [])
        if not isinstance(prices, list):
            return validation_error_response("prices must be a list")

        service = get_corporate_contract_service()
        updated_rows = service.upsert_contract_prices(
            contract_id=contract_id,
            prices=prices,
            actor_user_id=current_user_id,
        )
        _commit_db_session()

        return success_response(
            data={"prices": [row.to_dict() for row in updated_rows]},
            message="Corporate contract prices updated successfully",
        )
    except ValidationError as e:
        _rollback_db_session()
        return validation_error_response(str(e))
    except NotFoundError as e:
        _rollback_db_session()
        return not_found_response(message=str(e))
    except Exception as e:
        _rollback_db_session()
        current_app.logger.error(f"Upsert corporate contract prices error: {e}")
        return internal_error_response("Failed to update contract prices")


@admin_bp.route("/corporate/contracts/overlap-preview", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def preview_corporate_contract_overlaps():
    """Preview active contract overlap conflicts for contract dates/products."""
    try:
        payload = request.get_json() or {}
        service = get_corporate_contract_service()
        preview = service.preview_contract_price_overlaps(
            contract_id=payload.get("contract_id"),
            user_id=payload.get("user_id"),
            start_date=_parse_iso_datetime(payload.get("start_date")),
            end_date=_parse_iso_datetime(payload.get("end_date")),
            status=payload.get("status"),
            is_active=payload.get("is_active"),
            prices=payload.get("prices", []),
            contract_number=payload.get("contract_number"),
            contract_name=payload.get("name"),
        )
        return success_response(data={"preview": preview})
    except ValidationError as e:
        return validation_error_response(str(e))
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Corporate overlap preview error: {e}")
        return internal_error_response("Failed to preview contract overlaps")


@admin_bp.route("/corporate/contracts/<int:contract_id>/prepayments/topup", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def topup_corporate_contract(contract_id):
    """Top up corporate prepayment units for a specific product."""
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}

        product_id = payload.get("product_id")
        if product_id is None:
            return validation_error_response("product_id is required")

        units = payload.get("units")
        if units is None:
            return validation_error_response("units is required")

        service = get_corporate_contract_service()
        ledger_entry = service.topup_contract(
            contract_id=contract_id,
            product_id=int(product_id),
            units=Decimal(str(units)),
            amount=Decimal(str(payload["amount"])) if payload.get("amount") is not None else None,
            transfer_ref=payload.get("transfer_ref"),
            actor_user_id=current_user_id,
            notes=payload.get("notes"),
        )
        balance = service.get_balance(contract_id)
        _commit_db_session()

        return created_response(
            data={
                "ledger_entry": ledger_entry.to_dict(),
                "balance": balance,
            },
            message="Corporate prepayment topup applied",
        )
    except ValidationError as e:
        _rollback_db_session()
        return validation_error_response(str(e))
    except NotFoundError as e:
        _rollback_db_session()
        return not_found_response(message=str(e))
    except Exception as e:
        _rollback_db_session()
        current_app.logger.error(f"Corporate topup error: {e}")
        return internal_error_response("Failed to top up corporate contract")


@admin_bp.route("/corporate/contracts/<int:contract_id>/adjustments", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def adjust_corporate_contract_amount(contract_id):
    """Post a manual money-mode adjustment to a grocery-store contract.

    Payload: {"amount": <decimal>, "reason": "<required text>"}
    Positive `amount` increases customer debt; negative decreases it (write-off
    or correction). Reason is required and is stored on the ledger entry.
    """
    try:
        current_user_id = get_jwt_identity()
        payload = request.get_json() or {}

        amount = payload.get("amount")
        reason = (payload.get("reason") or "").strip()
        if amount is None:
            return validation_error_response("amount is required")
        if not reason:
            return validation_error_response("reason is required")

        service = get_corporate_contract_service()
        contract = service.get_contract_by_id(contract_id)
        ledger_entry = service.post_money_adjustment(
            contract=contract,
            amount=Decimal(str(amount)),
            actor_user_id=current_user_id,
            reason=reason,
        )
        balance = service.get_balance(contract_id)
        _commit_db_session()

        return created_response(
            data={
                "ledger_entry": ledger_entry.to_dict(),
                "balance": balance,
            },
            message="Adjustment recorded",
        )
    except ValidationError as e:
        _rollback_db_session()
        return validation_error_response(str(e))
    except NotFoundError as e:
        _rollback_db_session()
        return not_found_response(message=str(e))
    except Exception as e:
        _rollback_db_session()
        current_app.logger.error(f"Corporate amount adjustment error: {e}")
        return internal_error_response("Failed to record adjustment")


@admin_bp.route("/corporate/contracts/<int:contract_id>/balance", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_corporate_contract_balance(contract_id):
    """Get corporate prepayment balance."""
    try:
        service = get_corporate_contract_service()
        balance = service.get_balance(contract_id)
        return success_response(data={"balance": balance})
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get corporate balance error: {e}")
        return internal_error_response("Failed to get corporate contract balance")


@admin_bp.route("/corporate/contracts/<int:contract_id>/ledger", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_corporate_contract_ledger(contract_id):
    """Get corporate prepayment ledger events."""
    try:
        service = get_corporate_contract_service()
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 50, type=int)
        event_type = request.args.get("event_type")
        product_id = request.args.get("product_id", type=int)
        start_date = _parse_iso_datetime(request.args.get("start_date"))
        end_date = _parse_iso_datetime(request.args.get("end_date"))

        result = service.get_ledger(
            contract_id=contract_id,
            page=page,
            per_page=per_page,
            event_type=event_type,
            product_id=product_id,
            start_date=start_date,
            end_date=end_date,
        )
        return success_response(
            data={"items": result["items"]},
            meta={
                "page": result["page"],
                "per_page": result["per_page"],
                "total": result["total"],
            },
        )
    except ValidationError as e:
        return validation_error_response(str(e))
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Get corporate ledger error: {e}")
        return internal_error_response("Failed to get corporate contract ledger")


@admin_bp.route("/dashboard", methods=["GET"])
@jwt_required()
@staff_or_higher_required
def get_admin_dashboard():
    """Get comprehensive admin dashboard with analytics and chart data"""
    try:
        # Parse query parameters for date range
        period = request.args.get("period", "month")  # day, week, month, year

        now = datetime.now(UTC)
        today = now.date()
        yesterday = today - timedelta(days=1)

        # Calculate date ranges based on period
        if period == "day":
            current_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=UTC)
            previous_start = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=UTC)
            previous_end = current_start
            chart_days = 24  # Last 24 hours
        elif period == "week":
            current_start = now - timedelta(days=7)
            previous_start = now - timedelta(days=14)
            previous_end = current_start
            chart_days = 7
        elif period == "year":
            current_start = now - timedelta(days=365)
            previous_start = now - timedelta(days=730)
            previous_end = current_start
            chart_days = 12  # 12 months
        else:  # month (default)
            current_start = now - timedelta(days=30)
            previous_start = now - timedelta(days=60)
            previous_end = current_start
            chart_days = 30

        # ======================
        # OVERVIEW METRICS
        # ======================

        # User metrics - current period
        total_users = User.query.count()
        new_users_current = User.query.filter(User.created_at >= current_start).count()
        new_users_previous = User.query.filter(
            and_(User.created_at >= previous_start, User.created_at < previous_end)
        ).count()
        active_users = User.query.filter_by(status=UserStatus.ACTIVE.value).count()

        user_growth = (
            ((new_users_current - new_users_previous) / new_users_previous * 100) if new_users_previous > 0 else 0
        )

        # Order metrics - current period
        total_orders = Order.query.count()
        orders_current = Order.query.filter(Order.created_at >= current_start).count()
        orders_previous = Order.query.filter(
            and_(Order.created_at >= previous_start, Order.created_at < previous_end)
        ).count()
        pending_orders = Order.query.filter_by(status=OrderStatus.PENDING.value).count()

        orders_growth = ((orders_current - orders_previous) / orders_previous * 100) if orders_previous > 0 else 0

        # Revenue metrics - current period
        revenue_current = (
            getattr(db, "session")
            .query(func.sum(Order.total_amount))
            .filter(Order.created_at >= current_start, Order.status != OrderStatus.CANCELLED.value)
            .scalar()
            or 0
        )

        revenue_previous = (
            db.session.query(func.sum(Order.total_amount))
            .filter(
                and_(
                    Order.created_at >= previous_start,
                    Order.created_at < previous_end,
                    Order.status != OrderStatus.CANCELLED.value,
                )
            )
            .scalar()
            or 0
        )

        revenue_growth = ((revenue_current - revenue_previous) / revenue_previous * 100) if revenue_previous > 0 else 0

        avg_order_value_current = revenue_current / orders_current if orders_current > 0 else 0
        avg_order_value_previous = revenue_previous / orders_previous if orders_previous > 0 else 0

        # Product metrics
        total_products = Product.query.filter_by(is_active=True).count()
        low_stock_products = Product.query.filter(
            and_(Product.stock_quantity <= Product.min_stock_level, Product.track_inventory == True)  # noqa: E712
        ).count()
        out_of_stock = Product.query.filter(
            and_(Product.stock_quantity == 0, Product.track_inventory == True)  # noqa: E712
        ).count()  # noqa: E501,E712

        # Delivery metrics
        active_deliveries = Delivery.query.filter(
            Delivery.status.in_(
                [DeliveryStatus.ASSIGNED.value, DeliveryStatus.PICKED_UP.value, DeliveryStatus.IN_TRANSIT.value]
            )
        ).count()

        completed_deliveries_current = Delivery.query.filter(
            and_(Delivery.status == DeliveryStatus.DELIVERED.value, Delivery.updated_at >= current_start)
        ).count()

        failed_deliveries_current = Delivery.query.filter(
            and_(Delivery.status == DeliveryStatus.FAILED.value, Delivery.updated_at >= current_start)
        ).count()

        delivery_success_rate = (
            (completed_deliveries_current / (completed_deliveries_current + failed_deliveries_current) * 100)
            if (completed_deliveries_current + failed_deliveries_current) > 0
            else 0
        )

        # Subscription metrics
        active_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE.value).count()
        paused_subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.PAUSED.value).count()

        new_subscriptions_current = Subscription.query.filter(Subscription.created_at >= current_start).count()

        cancelled_subscriptions_current = Subscription.query.filter(
            and_(Subscription.status == SubscriptionStatus.CANCELLED.value, Subscription.updated_at >= current_start)
        ).count()

        subscription_revenue_current = (
            db.session.query(func.sum(Subscription.billing_amount))
            .filter(Subscription.status == SubscriptionStatus.ACTIVE.value)
            .scalar()
            or 0
        )

        # Loyalty metrics
        total_loyalty_members = LoyaltyPoints.query.count()
        points_in_circulation = db.session.query(func.sum(LoyaltyPoints.current_balance)).scalar() or 0

        points_earned_current = (
            db.session.query(func.sum(LoyaltyTransaction.points))
            .filter(and_(LoyaltyTransaction.created_at >= current_start, LoyaltyTransaction.points > 0))
            .scalar()
            or 0
        )

        points_redeemed_current = abs(
            db.session.query(func.sum(LoyaltyTransaction.points))
            .filter(and_(LoyaltyTransaction.created_at >= current_start, LoyaltyTransaction.points < 0))
            .scalar()
            or 0
        )

        # ======================
        # CHART DATA
        # ======================

        # Revenue trend chart (daily for month/week, hourly for day, monthly for year)
        revenue_chart = []
        orders_chart = []
        users_chart = []

        if period == "day":
            # Hourly data for last 24 hours
            for i in range(24):
                hour_start = now - timedelta(hours=24 - i)
                hour_end = hour_start + timedelta(hours=1)

                hourly_revenue = (
                    db.session.query(func.sum(Order.total_amount))
                    .filter(
                        and_(
                            Order.created_at >= hour_start,
                            Order.created_at < hour_end,
                            Order.status != OrderStatus.CANCELLED.value,
                        )
                    )
                    .scalar()
                    or 0
                )

                hourly_orders = Order.query.filter(
                    and_(Order.created_at >= hour_start, Order.created_at < hour_end)
                ).count()

                hourly_users = User.query.filter(
                    and_(User.created_at >= hour_start, User.created_at < hour_end)
                ).count()

                revenue_chart.append({"label": hour_start.strftime("%H:00"), "value": float(hourly_revenue)})
                orders_chart.append({"label": hour_start.strftime("%H:00"), "value": hourly_orders})
                users_chart.append({"label": hour_start.strftime("%H:00"), "value": hourly_users})

        elif period == "year":
            # Monthly data for last 12 months
            for i in range(12):
                month_start = (now - timedelta(days=365)) + timedelta(days=30 * i)
                month_end = month_start + timedelta(days=30)

                monthly_revenue = (
                    db.session.query(func.sum(Order.total_amount))
                    .filter(
                        and_(
                            Order.created_at >= month_start,
                            Order.created_at < month_end,
                            Order.status != OrderStatus.CANCELLED.value,
                        )
                    )
                    .scalar()
                    or 0
                )

                monthly_orders = Order.query.filter(
                    and_(Order.created_at >= month_start, Order.created_at < month_end)
                ).count()

                monthly_users = User.query.filter(
                    and_(User.created_at >= month_start, User.created_at < month_end)
                ).count()

                revenue_chart.append({"label": month_start.strftime("%b %Y"), "value": float(monthly_revenue)})
                orders_chart.append({"label": month_start.strftime("%b %Y"), "value": monthly_orders})
                users_chart.append({"label": month_start.strftime("%b %Y"), "value": monthly_users})

        else:
            # Daily data for week/month
            for i in range(chart_days):
                day_start = datetime.combine((today - timedelta(days=chart_days - 1 - i)), datetime.min.time()).replace(
                    tzinfo=UTC
                )
                day_end = day_start + timedelta(days=1)

                daily_revenue = (
                    db.session.query(func.sum(Order.total_amount))
                    .filter(
                        and_(
                            Order.created_at >= day_start,
                            Order.created_at < day_end,
                            Order.status != OrderStatus.CANCELLED.value,
                        )
                    )
                    .scalar()
                    or 0
                )

                daily_orders = Order.query.filter(
                    and_(Order.created_at >= day_start, Order.created_at < day_end)
                ).count()

                daily_users = User.query.filter(and_(User.created_at >= day_start, User.created_at < day_end)).count()

                revenue_chart.append({"label": day_start.strftime("%b %d"), "value": float(daily_revenue)})
                orders_chart.append({"label": day_start.strftime("%b %d"), "value": daily_orders})
                users_chart.append({"label": day_start.strftime("%b %d"), "value": daily_users})

        # Order status distribution (pie chart data)
        order_status_distribution = (
            db.session.query(Order.status, func.count(Order.id).label("count"))
            .filter(Order.created_at >= current_start)
            .group_by(Order.status)
            .all()
        )

        order_status_chart = [{"label": status.value, "value": count} for status, count in order_status_distribution]

        # Payment method distribution
        payment_method_distribution = (
            db.session.query(
                Payment.payment_method, func.count(Payment.id).label("count"), func.sum(Payment.amount).label("total")
            )
            .filter(Payment.created_at >= current_start)
            .group_by(Payment.payment_method)
            .all()
        )

        payment_method_chart = [
            {
                "label": method.value if hasattr(method, "value") else str(method),
                "count": count,
                "total": float(total or 0),
            }
            for method, count, total in payment_method_distribution
        ]

        # ======================
        # TOP PERFORMERS
        # ======================

        # Top 10 products by revenue
        top_products = (
            db.session.query(
                Product.id,
                Product.name,
                func.sum(OrderItem.quantity).label("units_sold"),
                func.sum(OrderItem.unit_price * OrderItem.quantity).label("revenue"),
            )
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .filter(and_(Order.created_at >= current_start, Order.status != OrderStatus.CANCELLED.value))
            .group_by(Product.id, Product.name)
            .order_by(func.sum(OrderItem.unit_price * OrderItem.quantity).desc())
            .limit(10)
            .all()
        )

        top_products_list = [
            {"product_id": p.id, "product_name": p.name, "units_sold": p.units_sold, "revenue": float(p.revenue)}
            for p in top_products
        ]

        # Top 10 customers by spending
        top_customers = (
            db.session.query(
                User.id,
                func.concat(User.first_name, " ", User.last_name).label("full_name"),
                User.phone,
                func.count(Order.id).label("order_count"),
                func.sum(Order.total_amount).label("total_spent"),
            )
            .join(Order, Order.user_id == User.id)
            .filter(and_(Order.created_at >= current_start, Order.status != OrderStatus.CANCELLED.value))
            .group_by(User.id, func.concat(User.first_name, " ", User.last_name), User.phone)
            .order_by(func.sum(Order.total_amount).desc())
            .limit(10)
            .all()
        )

        top_customers_list = [
            {
                "user_id": c.id,
                "name": c.full_name,
                "phone": c.phone,
                "order_count": c.order_count,
                "total_spent": float(c.total_spent),
            }
            for c in top_customers
        ]

        # ======================
        # RECENT ACTIVITY
        # ======================

        recent_orders = Order.query.order_by(Order.created_at.desc()).limit(5).all()
        recent_orders_list = [
            {
                "id": o.id,
                "user_name": o.user.full_name if o.user else "Unknown",
                "total_amount": float(o.total_amount),
                "status": o.status.value,
                "created_at": o.created_at.isoformat(),
            }
            for o in recent_orders
        ]

        recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
        recent_users_list = [
            {"id": u.id, "full_name": u.full_name, "phone": u.phone, "created_at": u.created_at.isoformat()}
            for u in recent_users
        ]

        # ======================
        # ALERTS & NOTIFICATIONS
        # ======================

        alerts = []

        if low_stock_products > 0:
            alerts.append(
                {
                    "type": "warning",
                    "category": "inventory",
                    "message": f"{low_stock_products} products are running low on stock",
                    "action_url": "/admin/products?filter=low_stock",
                }
            )

        if out_of_stock > 0:
            alerts.append(
                {
                    "type": "error",
                    "category": "inventory",
                    "message": f"{out_of_stock} products are out of stock",
                    "action_url": "/admin/products?filter=out_of_stock",
                }
            )

        if pending_orders > 10:
            alerts.append(
                {
                    "type": "warning",
                    "category": "orders",
                    "message": f"{pending_orders} orders are pending processing",
                    "action_url": "/admin/orders?status=pending",
                }
            )

        if failed_deliveries_current > 0:
            alerts.append(
                {
                    "type": "error",
                    "category": "delivery",
                    "message": f"{failed_deliveries_current} deliveries failed in the current period",
                    "action_url": "/admin/deliveries?status=failed",
                }
            )

        # Check for failed payments
        failed_payments = Payment.query.filter(
            and_(Payment.status == PaymentStatus.FAILED, Payment.created_at >= current_start)
        ).count()

        if failed_payments > 0:
            alerts.append(
                {
                    "type": "warning",
                    "category": "payments",
                    "message": f"{failed_payments} payments failed in the current period",
                    "action_url": "/admin/payments?status=failed",
                }
            )

        # ======================
        # COMPILE DASHBOARD DATA
        # ======================

        dashboard_data = {
            "overview": {
                "users": {
                    "total": total_users,
                    "new_current_period": new_users_current,
                    "new_previous_period": new_users_previous,
                    "growth_percentage": round(user_growth, 2),
                    "active": active_users,
                },
                "orders": {
                    "total": total_orders,
                    "current_period": orders_current,
                    "previous_period": orders_previous,
                    "growth_percentage": round(orders_growth, 2),
                    "pending": pending_orders,
                    "avg_order_value_current": float(avg_order_value_current),
                    "avg_order_value_previous": float(avg_order_value_previous),
                },
                "revenue": {
                    "current_period": float(revenue_current),
                    "previous_period": float(revenue_previous),
                    "growth_percentage": round(revenue_growth, 2),
                    "currency": "UZS",
                },
                "products": {
                    "total_active": total_products,
                    "low_stock": low_stock_products,
                    "out_of_stock": out_of_stock,
                },
                "deliveries": {
                    "active": active_deliveries,
                    "completed_current_period": completed_deliveries_current,
                    "failed_current_period": failed_deliveries_current,
                    "success_rate": round(delivery_success_rate, 2),
                },
                "subscriptions": {
                    "active": active_subscriptions,
                    "paused": paused_subscriptions,
                    "new_current_period": new_subscriptions_current,
                    "cancelled_current_period": cancelled_subscriptions_current,
                    "monthly_revenue": float(subscription_revenue_current),
                },
                "loyalty": {
                    "total_members": total_loyalty_members,
                    "points_in_circulation": points_in_circulation,
                    "points_earned_current_period": points_earned_current,
                    "points_redeemed_current_period": points_redeemed_current,
                },
            },
            "charts": {
                "revenue_trend": revenue_chart,
                "orders_trend": orders_chart,
                "users_trend": users_chart,
                "order_status_distribution": order_status_chart,
                "payment_methods": payment_method_chart,
            },
            "top_performers": {"products": top_products_list, "customers": top_customers_list},
            "recent_activity": {"orders": recent_orders_list, "users": recent_users_list},
            "alerts": alerts,
            "period": period,
            "date_range": {"start": current_start.isoformat(), "end": now.isoformat()},
        }

        current_app.logger.info(f"Get admin dashboard result: {dashboard_data}")

        return success_response(data={"dashboard": dashboard_data, "timestamp": now.isoformat()})

    except Exception as e:
        current_app.logger.error(f"Get admin dashboard error: {e}")
        return internal_error_response("Failed to get admin dashboard")


@admin_bp.route("/users", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_users"])
def get_users():
    """Get users with filtering and pagination"""
    try:
        # Get query parameters
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        role = request.args.get("role")
        status = request.args.get("status")
        sort_by = request.args.get("sort_by", "created_at")
        sort_order = request.args.get("sort_order", "desc")

        # Build query
        query = User.query

        # Apply search filter
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    User.first_name.ilike(search_term),
                    User.last_name.ilike(search_term),
                    User.email.ilike(search_term),
                    User.phone.ilike(search_term),
                    User.company_name.ilike(search_term),
                )
            )

        # Apply role filter
        if role:
            try:
                user_role = UserRole(role)
                query = query.filter_by(role=user_role)
            except ValueError:
                return validation_error_response("Invalid role value")

        # Apply status filter
        if status:
            try:
                user_status = UserStatus(status)
                query = query.filter_by(status=user_status.value)
            except ValueError:
                return validation_error_response("Invalid status value")

        # Apply registration_method filter
        registration_method = request.args.get("registration_method")
        if registration_method:
            valid_methods = ["email", "phone", "telegram"]
            if registration_method in valid_methods:
                query = query.filter(User.registration_method == registration_method)
            else:
                return validation_error_response("Invalid registration_method value")

        # Apply sorting
        if sort_by == "name":
            order_field = User.first_name
        elif sort_by == "email":
            order_field = User.email
        elif sort_by == "created_at":
            order_field = User.created_at
        elif sort_by == "last_login":
            order_field = User.last_login
        else:
            order_field = User.created_at

        if sort_order == "desc":
            order_field = order_field.desc()

        query = query.order_by(order_field)

        # Apply eager loading for user list optimization
        query = get_users_with_stats(query)

        # Paginate with optimized query
        pagination = PaginationOptimizer.optimize_paginated_query(
            query, page, per_page, eager_load_strategy="user_admin_list"
        )

        # Get user statistics efficiently
        user_ids = [user.id for user in pagination.items]
        user_statistics = AggregationOptimizer.get_user_statistics(user_ids)

        # Serialize users with statistics
        users_data = []
        for user in pagination.items:
            user_data = serialize_user_admin(user, include_statistics=True)
            user_stats = user_statistics.get(user.id, {})
            user_data.update(
                {
                    "order_count": user_stats.get("order_count", 0),
                    "total_spent": user_stats.get("total_spent", 0),
                    "last_order_date": user_stats.get("last_order_date"),
                    "delivery_success_rate": (
                        user_stats.get("successful_deliveries", 0) / max(user_stats.get("delivery_count", 1), 1) * 100
                        if user_stats.get("delivery_count", 0) > 0
                        else 0
                    ),
                }
            )
            users_data.append(user_data)

        return paginated_response(items=users_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get users error: {e}")
        return internal_error_response("Failed to get users")


@admin_bp.route("/users/<int:user_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_users"])
def get_user_details(user_id):
    """Get detailed user information"""
    try:
        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        # Get user's orders
        recent_orders = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).limit(10).all()

        # Get user's addresses
        addresses: List[UserAddress] = UserAddress.query.filter_by(user_id=user_id).all()

        # Get user statistics
        total_orders = Order.query.filter_by(user_id=user_id).count()
        total_spent = db.session.query(func.sum(Order.total_amount)).filter_by(user_id=user_id).scalar() or 0

        user_details = {
            "user": serialize_user_admin(user),
            "statistics": {
                "total_orders": total_orders,
                "total_spent": total_spent,
                "avg_order_value": total_spent / total_orders if total_orders > 0 else 0,
            },
            "recent_orders": [serialize_order_admin(order) for order in recent_orders],
            "addresses": [
                {
                    "id": addr.id,
                    "title": addr.title,
                    "full_address": addr.full_address,
                    "city": addr.city,
                    "is_default": addr.is_default,
                }
                for addr in addresses
            ],
        }

        return success_response(data=user_details)

    except Exception as e:
        current_app.logger.error(f"Get user details error: {e}")
        return internal_error_response("Failed to get user details")


@admin_bp.route("/users/<int:user_id>/payment-methods", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_user_payment_methods_admin(user_id):
    """Get debt-aware payment methods for admin-created orders."""
    try:
        from business_app.services.staff_service import StaffService

        payload = StaffService.get_client_payment_methods(user_id)
        return success_response(data=payload)
    except NotFoundError:
        return not_found_response(resource_type="User")
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Get admin user payment methods error: {e}")
        return internal_error_response("Failed to get user payment methods")


@admin_bp.route("/users/<int:user_id>/notification-settings", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_users"])
def get_user_notification_settings(user_id):
    """Get customer notification settings manageable by support/admin staff."""
    try:
        settings = get_notification_service().get_delivery_telegram_status_updates_setting(user_id)
        return success_response(data={"notification_settings": settings})
    except NotFoundError:
        return not_found_response(resource_type="User")
    except Exception as e:
        current_app.logger.error(f"Get user notification settings error: {e}")
        return internal_error_response("Failed to get user notification settings")


@admin_bp.route("/users/<int:user_id>/notification-settings", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
@validate_json(["delivery_telegram_status_updates_enabled", "reason"])
def update_user_notification_settings(user_id):
    """Update customer notification settings on behalf of the customer."""
    try:
        actor_user_id = int(get_jwt_identity())
        payload = request.get_json() or {}
        enabled = payload.get("delivery_telegram_status_updates_enabled")
        reason = (payload.get("reason") or "").strip()

        if not isinstance(enabled, bool):
            return validation_error_response(
                get_translation("api.notifications.validation.delivery_telegram_toggle_boolean")
            )
        if not reason:
            return validation_error_response(get_translation("api.notifications.validation.reason_required"))

        settings = get_notification_service().set_delivery_telegram_status_updates_setting(
            user_id=user_id,
            enabled=enabled,
            source="admin",
            actor_user_id=actor_user_id,
            reason=reason,
        )
        return success_response(
            data={"notification_settings": settings},
            message=get_translation("api.notifications.success.user_notification_settings_updated"),
        )
    except NotFoundError:
        return not_found_response(resource_type="User")
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Update user notification settings error: {e}")
        return internal_error_response("Failed to update user notification settings")


@admin_bp.route("/users/<int:user_id>/status", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
@validate_json(["status"])
def update_user_status(user_id):
    """Update user status"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        new_status = data.get("status")
        reason = data.get("reason", "")

        # Prevent privilege escalation - operators cannot modify admin/manager accounts
        current_user = g.current_user
        current_role_value = current_user.role.value if hasattr(current_user.role, "value") else current_user.role
        user_role_value = user.role.value if hasattr(user.role, "value") else user.role
        if current_role_value == UserRole.OPERATOR.value and user_role_value in [
            UserRole.ADMIN.value,
            UserRole.MANAGER.value,
        ]:
            return forbidden_response("Insufficient permissions to modify this user")

        # Prevent self-modification of critical status
        if current_user_id == user_id and new_status in ["banned", "suspended"]:
            return validation_error_response("Cannot suspend or ban your own account")

        try:
            user_status = UserStatus(new_status)
        except ValueError:
            return validation_error_response("Invalid status value")

        user.status
        user.status = user_status.value
        user.updated_at = datetime.now(UTC)

        # Log the status change (placeholder until admin_service is implemented)
        # admin_service.log_admin_action(
        #     admin_id=current_user_id,
        #     action='user_status_changed',
        #     target_type='user',
        #     target_id=user_id,
        #     details=f"Status changed from {old_status.value} to {new_status}. Reason: {reason}"
        # )

        db.session.commit()

        # Send notification to user if status changed to suspended/banned
        if user_status.value in [UserStatus.BANNED.value]:
            get_notification_service().send_notification(
                user_id, "account_status_changed", template_data={"status": new_status, "reason": reason}
            )

        return success_response(data={"user": serialize_user_admin(user)}, message="User status updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update user status error: {e}")
        return internal_error_response("Failed to update user status")


@admin_bp.route("/users/<int:user_id>/unlock", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def unlock_user_account(user_id):
    """
    Unlock a locked user account.

    This endpoint clears both the Redis lockout keys and the database
    account_locked_until field. The action is logged to the audit trail.

    Only managers and admins can perform this action.

    Returns:
        Success message with updated user data
    """
    try:
        current_user_id = get_jwt_identity()

        # Prevent self-unlock (though this shouldn't normally happen)
        if current_user_id == user_id:
            return validation_error_response("Cannot unlock your own account")

        from business_app.services.auth_service import AuthService

        auth_service = AuthService()

        auth_service.unlock_user_account(user_id, current_user_id)

        # Get updated user data
        user = User.query.get(user_id)

        return success_response(data={"user": serialize_user_admin(user)}, message="User account unlocked successfully")

    except NotFoundError:
        return not_found_response(resource_type="User")
    except Exception as e:
        current_app.logger.error(f"Unlock user account error: {e}")
        return internal_error_response("Failed to unlock user account")


@admin_bp.route("/users", methods=["POST"])
@jwt_required()
@staff_or_higher_required
@validate_json(["phone", "first_name"])
def create_user():
    """
    Create a new user from admin panel (for call center operations).

    This endpoint allows staff to create customer accounts for people who
    order by phone (e.g., elderly customers who don't use the app).

    Users created this way:
    - Cannot login to the web cabinet (no password access)
    - Can have orders placed on their behalf
    - Are marked with registration_source='admin_created'

    Required fields:
    - phone: User phone number (must be unique, international format)
    - first_name: User first name

    Optional fields:
    - last_name: User last name
    - email: User email (must be unique if provided)
    - company_name: Legal entity name for entity clients
    - tax_id: Tax identifier
    - user_type: User classification, use `entity` for legal-entity clients
    - notes: Admin notes about the user (logged, not stored)

    Returns:
        Created user data with success message
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        phone = data.get("phone", "").strip()
        first_name = data.get("first_name", "").strip()
        last_name = data.get("last_name", "").strip() if data.get("last_name") else None
        email = data.get("email", "").strip() if data.get("email") else None
        company_name = data.get("company_name", "").strip() if data.get("company_name") else None
        tax_id = data.get("tax_id", "").strip() if data.get("tax_id") else None
        user_type = data.get("user_type", "").strip() if data.get("user_type") else None
        entity_subtype = data.get("entity_subtype", "").strip() if data.get("entity_subtype") else None
        notes = data.get("notes", "").strip() if data.get("notes") else None

        # Validate required fields
        if not phone:
            return validation_error_response("Phone number is required")
        if not first_name:
            return validation_error_response("First name is required")

        # Use AuthService to create user
        from business_app.services.auth_service import AuthService

        auth_service = AuthService()

        user = auth_service.create_user_by_admin(
            phone=phone,
            first_name=first_name,
            created_by_admin_id=current_user_id,
            last_name=last_name,
            email=email,
            company_name=company_name,
            tax_id=tax_id,
            user_type=user_type,
            entity_subtype=entity_subtype,
            notes=notes,
        )

        current_app.logger.info(f"Admin {current_user_id} created user {user.id} with phone {user.phone}")

        return created_response(data={"user": serialize_user_admin(user)}, message="User created successfully")

    except ConflictError as e:
        return error_response(str(e), status_code=409)
    except ValidationError as e:
        return validation_error_response(e.errors)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create user error: {e}")
        return internal_error_response("Failed to create user")


@admin_bp.route("/users/<int:user_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["edit_users"])
@validate_json(["phone", "first_name"])
def update_user(user_id):
    """Update a user from the admin panel."""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        from business_app.services.auth_service import AuthService

        auth_service = AuthService()

        # entity_subtype: only forward when client explicitly sent a value (the
        # service uses a sentinel to distinguish "not provided" from "set to NULL").
        update_kwargs = dict(
            user_id=user_id,
            updated_by_admin_id=current_user_id,
            phone=data.get("phone", "").strip(),
            first_name=data.get("first_name", "").strip(),
            last_name=data.get("last_name", "").strip() if data.get("last_name") else None,
            email=data.get("email", "").strip() if data.get("email") else None,
            company_name=data.get("company_name", "").strip() if data.get("company_name") else None,
            tax_id=data.get("tax_id", "").strip() if data.get("tax_id") else None,
            user_type=data.get("user_type", "").strip() if data.get("user_type") else None,
        )
        if "entity_subtype" in data:
            raw_subtype = data.get("entity_subtype")
            update_kwargs["entity_subtype"] = (
                raw_subtype.strip() if isinstance(raw_subtype, str) and raw_subtype.strip() else None
            )

        # Only forward the COD-exemption flag when the client explicitly sent
        # it; absence means "leave unchanged" (service treats None that way).
        if "cod_debt_check_exempt" in data:
            update_kwargs["cod_debt_check_exempt"] = bool(data.get("cod_debt_check_exempt"))

        user = auth_service.update_user_by_admin(**update_kwargs)

        return success_response(data={"user": serialize_user_admin(user)}, message="User updated successfully")
    except ConflictError as e:
        return error_response(str(e), status_code=409)
    except ValidationError as e:
        return validation_error_response(e.errors)
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update user error: {e}")
        return internal_error_response("Failed to update user")


# ============================================================================
# User Address Management Endpoints (Admin)
# ============================================================================


@admin_bp.route("/users/<int:user_id>/addresses", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_users"])
def get_user_addresses(user_id):
    """Get all addresses for a specific user"""
    try:
        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        addresses = (
            UserAddress.query.filter_by(user_id=user_id)
            .order_by(UserAddress.is_default.desc(), UserAddress.created_at.desc())
            .all()
        )

        return success_response(
            data={"addresses": [addr.to_dict() for addr in addresses], "user_id": user_id, "total": len(addresses)}
        )

    except Exception as e:
        current_app.logger.error(f"Get user addresses error: {e}")
        return internal_error_response("Failed to get user addresses")


@admin_bp.route("/users/<int:user_id>/addresses", methods=["POST"])
@jwt_required()
@staff_or_higher_required
@validate_json(["full_address"])
def create_user_address(user_id):
    """
    Create address for a user (admin operation).

    Required fields:
    - full_address: Complete address string

    Optional fields:
    - title: Address label (Home, Work, etc.)
    - street_address: Street name and number
    - city: City name (default: Tashkent)
    - district: District name
    - latitude/longitude: GPS coordinates
    - delivery_instructions: Special delivery instructions
    - landmark: Nearby landmark
    - floor_number: Building floor
    - apartment_number: Apartment/unit number
    - is_default: Set as default address
    """
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        data = request.get_json()

        full_address = data.get("full_address", "").strip()
        if not full_address:
            return validation_error_response("Full address is required")

        # Enforce the delivery-zone SSOT before persisting any coordinate.
        latitude, longitude = data.get("latitude"), data.get("longitude")
        if latitude is not None and longitude is not None and not is_within_tashkent(latitude, longitude):
            return validation_error_response(get_translation("api.addresses.error.coordinates_outside_supported_area"))

        # Check if this should be default
        is_default = data.get("is_default", False)

        # If setting as default, unset other defaults
        if is_default:
            UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})

        # If user has no addresses, make this the default
        existing_count = UserAddress.query.filter_by(user_id=user_id).count()
        if existing_count == 0:
            is_default = True

        # Create address
        address = UserAddress(
            user_id=user_id,
            title=data.get("title"),
            full_address=full_address,
            street_address=data.get("street_address"),
            city=data.get("city", "Tashkent"),
            district=data.get("district"),
            postal_code=data.get("postal_code"),
            country=data.get("country", "Uzbekistan"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            is_default=is_default,
            is_business=data.get("is_business", False),
            delivery_instructions=data.get("delivery_instructions"),
            landmark=data.get("landmark"),
            floor_number=data.get("floor_number"),
            apartment_number=data.get("apartment_number"),
        )

        db.session.add(address)
        db.session.commit()

        current_app.logger.info(f"Admin {current_user_id} created address {address.id} for user {user_id}")

        return created_response(data={"address": address.to_dict()}, message="Address created successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create user address error: {e}")
        return internal_error_response("Failed to create address")


@admin_bp.route("/users/<int:user_id>/addresses/<int:address_id>", methods=["PUT"])
@jwt_required()
@staff_or_higher_required
def update_user_address(user_id, address_id):
    """Update an existing user address (admin operation)"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            return not_found_response(resource_type="Address")

        data = request.get_json()

        # Enforce the delivery-zone SSOT when coordinates are being changed.
        if "latitude" in data or "longitude" in data:
            new_lat = data["latitude"] if "latitude" in data else address.latitude
            new_lng = data["longitude"] if "longitude" in data else address.longitude
            if new_lat is not None and new_lng is not None and not is_within_tashkent(new_lat, new_lng):
                return validation_error_response(
                    get_translation("api.addresses.error.coordinates_outside_supported_area")
                )

        # Update fields if provided
        if "title" in data:
            address.title = data["title"]
        if "full_address" in data:
            address.full_address = data["full_address"]
        if "street_address" in data:
            address.street_address = data["street_address"]
        if "city" in data:
            address.city = data["city"]
        if "district" in data:
            address.district = data["district"]
        if "postal_code" in data:
            address.postal_code = data["postal_code"]
        if "country" in data:
            address.country = data["country"]
        if "latitude" in data:
            address.latitude = data["latitude"]
        if "longitude" in data:
            address.longitude = data["longitude"]
        if "is_business" in data:
            address.is_business = data["is_business"]
        if "delivery_instructions" in data:
            address.delivery_instructions = data["delivery_instructions"]
        if "landmark" in data:
            address.landmark = data["landmark"]
        if "floor_number" in data:
            address.floor_number = data["floor_number"]
        if "apartment_number" in data:
            address.apartment_number = data["apartment_number"]

        # Handle default flag
        if data.get("is_default"):
            UserAddress.query.filter_by(user_id=user_id, is_default=True).update({"is_default": False})
            address.is_default = True

        db.session.commit()

        current_app.logger.info(f"Admin {current_user_id} updated address {address_id} for user {user_id}")

        return success_response(data={"address": address.to_dict()}, message="Address updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update user address error: {e}")
        return internal_error_response("Failed to update address")


@admin_bp.route("/users/<int:user_id>/addresses/<int:address_id>", methods=["DELETE"])
@jwt_required()
@manager_or_higher_required
def delete_user_address(user_id, address_id):
    """Delete a user address (manager+ only)"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
        if not address:
            return not_found_response(resource_type="Address")

        # Check if this is the only address
        address_count = UserAddress.query.filter_by(user_id=user_id).count()
        if address_count == 1:
            return validation_error_response("Cannot delete the only address for this user")

        has_subscription_reference = (
            Subscription.query.filter_by(
                user_id=user_id,
                delivery_address_id=address_id,
            ).first()
            is not None
        )
        if has_subscription_reference:
            message = get_translation("api.addresses.error.in_use_by_subscription")
            if message == "api.addresses.error.in_use_by_subscription":
                message = "Cannot delete an address used by subscriptions"
            return validation_error_response(message)

        # If deleting default address, set another as default
        if address.is_default:
            other_address = UserAddress.query.filter(
                UserAddress.user_id == user_id, UserAddress.id != address_id
            ).first()
            if other_address:
                other_address.is_default = True

        db.session.delete(address)
        db.session.commit()

        current_app.logger.info(f"Admin {current_user_id} deleted address {address_id} for user {user_id}")

        return success_response(message="Address deleted successfully")

    except IntegrityError:
        db.session.rollback()
        return validation_error_response("Cannot delete an address referenced by existing records")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete user address error: {e}")
        return internal_error_response("Failed to delete address")


@admin_bp.route("/orders", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_orders():
    """Get orders with filtering and pagination"""
    try:
        # Get query parameters
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        status = request.args.get("status")
        search = request.args.get("search", "").strip()
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        sort_by = request.args.get("sort_by", "created_at")
        sort_order = request.args.get("sort_order", "desc")

        # Build query
        query = Order.query

        # Apply filters
        if status:
            try:
                order_status = OrderStatus(status)
                query = query.filter_by(status=order_status.value)
            except ValueError:
                return validation_error_response("Invalid status value")

        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(
                or_(
                    Order.order_number.ilike(search_term),
                    User.first_name.ilike(search_term),
                    User.last_name.ilike(search_term),
                    User.phone.ilike(search_term),
                )
            )

        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
                query = query.filter(Order.created_at >= start_dt)
            except ValueError:
                return validation_error_response("Invalid start_date format")

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(Order.created_at <= end_dt)
            except ValueError:
                return validation_error_response("Invalid end_date format")

        if request.args.get("fiscalization_failed", "").lower() in ("1", "true", "yes"):
            query = (
                query.join(Payment, Payment.order_id == Order.id)
                .join(PaymentFiscalization, PaymentFiscalization.payment_id == Payment.id)
                .filter(PaymentFiscalization.retries_exhausted_at.isnot(None))
            )

        # Apply sorting
        if sort_by == "total_amount":
            order_field = Order.total_amount
        elif sort_by == "customer":
            order_field = User.first_name
            query = query.join(User)
        else:
            order_field = Order.created_at

        if sort_order == "desc":
            order_field = order_field.desc()

        query = query.order_by(order_field)

        # Apply eager loading for orders with full details
        query = get_orders_with_details(query)

        # Paginate with optimized query
        pagination = PaginationOptimizer.optimize_paginated_query(
            query, page, per_page, eager_load_strategy="order_admin_detail"
        )

        # Get order statistics efficiently
        order_ids = [order.id for order in pagination.items]
        order_statistics = AggregationOptimizer.get_order_statistics(order_ids)

        # Serialize orders with statistics
        orders_data = []
        for order in pagination.items:
            current_app.logger.info(
                f"Order: {order}, status: {order.status}, staus.type: {type(order.status)}, status.value: {order.status.value}, status.value.type: {type(order.status.value)}"  # noqa: E501
            )
            order_data = serialize_order_admin(order)
            current_app.logger.info(f"order_data: {order_data}")
            order_stats = order_statistics.get(order.id, {})
            order_data.update(
                {
                    "item_count": order_stats.get("item_count", 0),
                    "total_quantity": order_stats.get("total_quantity", 0),
                    "payment_count": order_stats.get("payment_count", 0),
                    "last_payment_date": order_stats.get("last_payment_date"),
                }
            )
            orders_data.append(order_data)

        return paginated_response(items=orders_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get orders error: {e}")
        return internal_error_response("Failed to get orders")


@admin_bp.route("/orders", methods=["POST"])
@jwt_required()
@staff_or_higher_required
@validate_json(["user_id", "items", "delivery_address_id"])
def create_order_for_user():
    """
    Create an order on behalf of a user (for call center operations).

    This allows admin/operators to place orders for customers who call in.

    Required fields:
    - user_id: ID of the user placing the order
    - items: List of {product_id, quantity}
    - delivery_address_id: ID of delivery address

    Optional fields:
    - payment_method: cash, payme, click, business_account (default: cash)
    - delivery_notes: Special delivery instructions
    - consume_marking_codes: only applies to business_account orders; default false
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user_id = data.get("user_id")
        items = data.get("items", [])
        delivery_address_id = data.get("delivery_address_id")
        payment_method = data.get("payment_method", "cash")
        delivery_notes = data.get("delivery_notes", "")
        consume_marking_codes = bool(data.get("consume_marking_codes", False))

        # Validate user exists
        user = User.query.get(user_id)
        if not user:
            return not_found_response(resource_type="User")

        # Validate user status
        status_value = user.status.value if hasattr(user.status, "value") else user.status
        if status_value != UserStatus.ACTIVE.value:
            return validation_error_response("Cannot create order for inactive user")

        # Validate address exists and belongs to user
        address: UserAddress = UserAddress.query.filter_by(id=delivery_address_id, user_id=user_id).first()
        if not address:
            return validation_error_response("Invalid delivery address for this user")

        # Validate items
        if not items or len(items) == 0:
            return validation_error_response("At least one item is required")

        # Use OrderService to create the order
        from business_app.services.order_service import OrderService

        order_service = OrderService()

        order_data = {
            "items": items,
            "delivery_address": {
                "delivery_address_id": delivery_address_id,
                "street": address.street_address,
                "latitude": address.latitude,
                "longitude": address.longitude,
            },
            "payment_method": payment_method,
            "consume_marking_codes": consume_marking_codes,
            "delivery_notes": delivery_notes,
            "order_source": "admin",
            "created_by_staff_id": current_user_id,
        }

        order = order_service.create_order(user_id, order_data)

        current_app.logger.info(f"Admin {current_user_id} created order {order.id} for user {user_id}")

        response_data = {"order": serialize_order_admin(order)}
        if getattr(order, "payment", None) and payment_method in {"click", "card", "payme"}:
            payment_link = PaymentService().create_payment_link(order.payment.id)
            response_data["payment_link"] = payment_link
            response_data["payment_url"] = (
                payment_link.get("payment_url") if isinstance(payment_link, dict) else payment_link
            )

        return created_response(data=response_data, message="Order created successfully")

    except ValidationError as e:
        return validation_error_response(e.errors)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create order for user error: {e}")
        return internal_error_response(f"Failed to create order: {str(e)}")


@admin_bp.route("/orders/<int:order_id>/status", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_orders", "update_orders"])
@validate_json(["status"])
def update_order_status(order_id):
    """Update order status"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        order = Order.query.get(order_id)
        if not order:
            return not_found_response(resource_type="Order")

        new_status = data.get("status")
        notes = data.get("notes", "")
        bottles_returned = data.get("bottles_returned")

        try:
            order_status = OrderStatus(new_status)
        except ValueError:
            return validation_error_response("Invalid status value")

        # Use OrderService to properly handle status transitions and inventory
        from business_app.services.order_service import OrderService

        order_service = OrderService()

        try:
            if order_status == OrderStatus.CANCELLED:
                # Route cancellation through cancel_order so the FULL cascade
                # runs (stock restoration, inventory-reservation release,
                # payment refund, corporate prepayment release, and delivery
                # cancellation) — not just the bare status flip that
                # update_order_status performs.
                order = order_service.cancel_order(
                    order_id=order_id,
                    actor_user_id=current_user_id,
                    reason=notes or None,
                )
            else:
                order = order_service.update_order_status(
                    order_id=order_id,
                    new_status=order_status,
                    updated_by=current_user_id,
                    notes=notes,
                    bottles_returned=bottles_returned,
                )

            return success_response(
                data={"order": serialize_order_admin(order)}, message="Order status updated successfully"
            )
        except Exception as e:
            current_app.logger.error(f"Failed to update order status: {e}")
            return validation_error_response(str(e))

    except Exception as e:
        current_app.logger.error(f"Update order status error: {e}")
        return internal_error_response("Failed to update order status")


@admin_bp.route("/orders/<int:order_id>/edit-preview", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders", "edit_orders"])
def preview_order_edit(order_id):
    """Dry-run an order edit: returns the cascade plan + impacts.

    Body: { items: [{order_item_id?, product_id, quantity}], reason: str }
    Response: { blocking_reasons, warnings, items_before, items_after,
                totals_before, totals_after, cascade_summary, is_post_delivery }
    """
    try:
        from business_app.serializers.order_serializers import OrderEditRequest
        from business_app.services.order_edit_service import (
            OrderEditItemSpec as _OrderEditItemSpec,
            OrderEditService,
        )
        from pydantic import ValidationError as PydanticValidationError

        payload = request.get_json(silent=True) or {}
        try:
            req = OrderEditRequest.model_validate(payload)
        except PydanticValidationError as exc:
            return validation_error_response(exc.errors())

        service = OrderEditService()
        plan = service.preview(
            order_id=order_id,
            items=[
                _OrderEditItemSpec(
                    order_item_id=item.order_item_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in req.items
            ],
        )
        return success_response(
            data={
                "blocking_reasons": plan.blocking_reasons,
                "warnings": plan.warnings,
                "items_before": plan.items_before,
                "items_after": plan.items_after,
                "totals_before": plan.totals_before,
                "totals_after": plan.totals_after,
                "cascade_summary": plan.cascade_summary,
                "is_post_delivery": plan.is_post_delivery,
            }
        )
    except NotFoundError as e:
        return not_found_response(resource_type="Order", message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Order edit preview error: {e}", exc_info=True)
        return internal_error_response("Failed to preview order edit")


@admin_bp.route("/orders/<int:order_id>/edit", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders", "edit_orders"])
def apply_order_edit(order_id):
    """Apply an admin order edit and cascade side-effects atomically.

    On success, dispatches the ORDER_EDITED notification post-commit.
    """
    try:
        from business_app.serializers.order_serializers import OrderEditRequest
        from business_app.services.order_edit_service import (
            OrderEditItemSpec as _OrderEditItemSpec,
            OrderEditService,
        )
        from business_app.utils.audit_logger import (
            AuditEventType as _AuditEventType,
            AuditSeverity as _AuditSeverity,
            audit_logger as _audit_logger,
        )
        from pydantic import ValidationError as PydanticValidationError

        current_user_id = get_jwt_identity()
        payload = request.get_json(silent=True) or {}
        try:
            req = OrderEditRequest.model_validate(payload)
        except PydanticValidationError as exc:
            return validation_error_response(exc.errors())

        service = OrderEditService()
        result = service.apply_edit(
            order_id=order_id,
            items=[
                _OrderEditItemSpec(
                    order_item_id=item.order_item_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in req.items
            ],
            reason=req.reason,
            actor_user_id=int(current_user_id),
        )

        # Post-commit dispatch: audit log + customer telegram notification.
        # These run only after the apply_edit transaction commits successfully.
        _audit_logger.log_event(
            event_type=_AuditEventType.ORDER_EDITED,
            action="admin_order_edit_applied",
            severity=_AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(order_id),
            additional_data={
                "actor_user_id": current_user_id,
                "history_id": result.history_id,
                "cascade_summary": result.cascade_summary,
                "warnings": result.warnings,
            },
        )
        for task_name, args, kwargs in result.post_commit_dispatch:
            if task_name == "send_order_notification_task":
                from business_app.tasks.notification_tasks import (
                    send_order_notification_task,
                )

                send_order_notification_task.delay(*args, **kwargs)
            elif task_name == "notify_driver_session_reopened":
                # Best-effort driver Telegram message — failures must not
                # roll back the (already-committed) edit.
                try:
                    from business_app.models.user import User as _User
                    from business_app.services.notification_service import (
                        NotificationService as _NotificationService,
                    )
                    from business_app.utils.translations import get_translation as _gt

                    driver_user_id, session_id, edited_order_id = args
                    driver = _User.query.get(driver_user_id)
                    if driver is not None:
                        lang = getattr(driver, "preferred_language", None) or "en"
                        body = _gt(
                            "staff.notification.bottle_session_reopened",
                            language=lang,
                            session_id=session_id,
                            order_id=edited_order_id,
                        )
                        if body and body != "staff.notification.bottle_session_reopened":
                            fallback_body = body
                        else:
                            fallback_body = (
                                f"Your bottle session #{session_id} was reopened by "
                                f"admin because order #{edited_order_id} was edited "
                                "after delivery. Please re-close the session when ready."
                            )
                        _NotificationService().send_staff_telegram_message(driver, fallback_body, language=lang)
                except Exception as notify_exc:  # noqa: BLE001
                    current_app.logger.warning("Driver reopen-notification skipped: %s", notify_exc)

        return success_response(
            data={
                "order_id": result.order_id,
                "history_id": result.history_id,
                "cascade_summary": result.cascade_summary,
                "warnings": result.warnings,
            },
            message="Order edit applied successfully",
        )
    except NotFoundError as e:
        return not_found_response(resource_type="Order", message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Order edit apply error: {e}", exc_info=True)
        return internal_error_response("Failed to apply order edit")


@admin_bp.route("/orders/<int:order_id>/edit-history", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def list_order_edit_history(order_id):
    """List all admin edits applied to a given order."""
    try:
        from business_app.services.order_edit_service import OrderEditService

        data = OrderEditService().get_edit_history(order_id)
        return success_response(data=data)
    except NotFoundError as e:
        return not_found_response(resource_type="Order", message=str(e))
    except Exception as e:
        current_app.logger.error(f"Order edit history error: {e}")
        return internal_error_response("Failed to fetch order edit history")


@admin_bp.route("/orders/<int:order_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_order_details(order_id):
    """Get detailed information about a specific order including all items"""
    try:
        from business_app.utils.payment_projection import get_payment_projection

        # Get order with all related data
        order = Order.query.options(
            db.joinedload(Order.user),
            db.joinedload(Order.order_items).joinedload(OrderItem.product),
            db.joinedload(Order.delivery_address),
            db.joinedload(Order.delivery),
            db.joinedload(Order.payment).joinedload(Payment.transactions),
            db.joinedload(Order.payment).joinedload(Payment.fiscalization),
            db.joinedload(Order.marking_code_allocations).joinedload(OrderItemMarkingCodeAllocation.marking_code),
        ).get(order_id)

        if not order:
            return not_found_response(resource_type="Order")

        payment_projection = get_payment_projection(order.payment) if order.payment else None
        fiscalization_service = PaymentFiscalizationService()

        # Build response with full order details
        order_data = {
            "id": order.id,
            "order_number": order.order_number,
            "user_id": order.user_id,
            "status": order.status.value if order.status else None,
            "total_amount": float(order.total_amount),
            "subtotal": float(getattr(order, "subtotal", order.total_amount)),
            "tax_amount": float(getattr(order, "tax_amount", 0)),
            "discount_amount": float(getattr(order, "discount_amount", 0)),
            "delivery_fee": float(getattr(order, "delivery_fee", 0)),
            "payment_method": order.payment_method.value if order.payment_method else None,
            "payment_status": (
                order.payment.status.value
                if order.payment and hasattr(order.payment.status, "value")
                else ("pending" if not order.payment else str(order.payment.status))
            ),
            "payment_id": order.payment.id if order.payment else None,
            "payment_provider": getattr(order.payment, "payment_provider", None) if order.payment else None,
            "payment_link": getattr(order.payment, "payment_link", None) if order.payment else None,
            "provider_transaction_id": (
                getattr(order.payment, "provider_transaction_id", None) if order.payment else None
            ),
            "consume_marking_codes": (
                bool(getattr(order.payment, "consume_marking_codes", False)) if order.payment else False
            ),
            "amount_collected": float(payment_projection["amount_collected"]) if payment_projection else 0,
            "outstanding_amount": float(payment_projection["outstanding_amount"]) if payment_projection else 0,
            "collection_events_count": (
                len(getattr(order.payment, "cash_collection_allocations", []) or []) if order.payment else 0
            ),
            "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
            "special_instructions": getattr(order, "special_instructions", None),
            "admin_notes": getattr(order, "admin_notes", None),
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }

        # Add customer information
        if order.user:
            order_data["customer"] = {
                "id": order.user.id,
                "name": f"{order.user.first_name} {order.user.last_name}".strip(),
                "email": order.user.email,
                "phone": order.user.phone,
            }
            # Backward compatibility
            order_data["customer_name"] = order_data["customer"]["name"]
            order_data["customer_email"] = order_data["customer"]["email"]
            order_data["customer_phone"] = order_data["customer"]["phone"]

        # Add delivery address
        if order.delivery_address:
            order_data["delivery_address"] = order.delivery_address.to_dict()

        # Add ALL order items (not limited to 5)
        order_data["items"] = []
        order_data["items_summary"] = []  # For backward compatibility
        if order.order_items:
            from business_app.serializers.order_serializers import is_free_reward_item

            for item in order.order_items:
                item_data = {
                    "id": item.id,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else "Unknown",
                    "product_sku": item.product.sku if item.product else None,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "total_price": float(item.total_price),
                    "is_reward": is_free_reward_item(item),
                }
                order_data["items"].append(item_data)
                order_data["items_summary"].append(item_data)

        order_data["item_count"] = len(order_data["items"])
        order_data["loyalty_discount"] = float(getattr(order, "loyalty_discount", 0) or 0)
        order_data["has_loyalty_reward"] = order_data["loyalty_discount"] > 0 or any(
            it.get("is_reward") for it in order_data["items"]
        )

        # Add delivery information
        if order.delivery:
            order_data["delivery"] = {
                "id": order.delivery.id,
                "tracking_number": order.delivery.tracking_number,
                "status": order.delivery.status.value if order.delivery.status else None,
                "estimated_delivery": (
                    order.delivery.estimated_delivery.isoformat()
                    if hasattr(order.delivery, "estimated_delivery") and order.delivery.estimated_delivery
                    else None
                ),
                "actual_delivery": (
                    order.delivery.actual_delivery.isoformat()
                    if hasattr(order.delivery, "actual_delivery") and order.delivery.actual_delivery
                    else None
                ),
            }
            if order.delivery.delivery_person:
                order_data["delivery"]["delivery_person"] = {
                    "id": order.delivery.delivery_person.id,
                    "name": order.delivery.delivery_person.full_name,
                    "phone": order.delivery.delivery_person.phone,
                }

        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()
        order_data["payment_timeline"] = cash_collection_service.get_order_payment_timeline(order.id)
        if order.payment:
            order_data["fiscalization"] = (
                order.payment.fiscalization.to_dict() if getattr(order.payment, "fiscalization", None) else None
            )
            if getattr(order.payment, "fiscalization", None) and hasattr(order.payment.fiscalization.status, "value"):
                order_data["fiscalization_status"] = order.payment.fiscalization.status.value
            elif fiscalization_service.payment_requires_click_fiscalization(order.payment):
                order_data["fiscalization_status"] = "pending"
            else:
                order_data["fiscalization_status"] = "not_required"
            order_data["marking_code_summary"] = fiscalization_service.marking_code_allocation_summary(order)
            order_data["payment_transactions"] = [
                {
                    "id": txn.id,
                    "transaction_type": txn.transaction_type,
                    "amount": float(txn.amount),
                    "currency": txn.currency,
                    "status": txn.status,
                    "provider_transaction_id": txn.provider_transaction_id,
                    "provider_reference": txn.provider_reference,
                    "success": txn.success,
                    "failure_reason": txn.failure_reason,
                    "notes": txn.notes,
                    "created_at": txn.created_at.isoformat() if txn.created_at else None,
                    "processed_at": txn.processed_at.isoformat() if txn.processed_at else None,
                }
                for txn in sorted(
                    order.payment.transactions or [],
                    key=lambda txn: txn.created_at or datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )
            ]
            click_callbacks = ((order.payment.provider_data or {}).get("click") or {}).get("callbacks") or []
            order_data["click_callback_history"] = list(reversed(click_callbacks))
            order_data["fiscalization_audit_trail"] = list(
                reversed((order.payment.provider_data or {}).get("fiscalization_audit_trail") or [])
            )
        else:
            order_data["fiscalization"] = None
            order_data["fiscalization_status"] = "not_required"
            order_data["marking_code_summary"] = {"events": {}, "codes_by_order_item": {}}
            order_data["payment_transactions"] = []
            order_data["click_callback_history"] = []
            order_data["fiscalization_audit_trail"] = []

        order_data["marking_code_activity"] = [
            {
                "id": allocation.id,
                "order_item_id": allocation.order_item_id,
                "action": allocation.action.value if hasattr(allocation.action, "value") else allocation.action,
                "code": allocation.marking_code.code if allocation.marking_code else None,
                "actor_user_id": allocation.actor_user_id,
                "notes": allocation.notes,
                "payment_id": allocation.payment_id,
                "payment_fiscalization_id": allocation.payment_fiscalization_id,
                "event_metadata": allocation.event_metadata or {},
                "occurred_at": allocation.occurred_at.isoformat() if allocation.occurred_at else None,
            }
            for allocation in sorted(
                order.marking_code_allocations or [],
                key=lambda allocation: allocation.occurred_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        ]

        # Order edit feature: surface whether the order is editable right now,
        # the remaining window for delivered orders, and a count of past edits
        # so the admin UI can decide whether to render the "Edit Items" CTA.
        from business_app.services.order_edit_service import OrderEditService

        order_data.update(OrderEditService().get_edit_metadata(order))

        return success_response(data={"order": order_data})

    except Exception as e:
        current_app.logger.error(f"Get order details error: {e}")
        return internal_error_response("Failed to get order details")


@admin_bp.route("/products", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_products_admin():
    """Get products for admin management"""
    try:
        # Get query parameters
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        category_id = request.args.get("category_id", type=int)
        is_active = request.args.get("is_active", type=bool)
        low_stock_only = request.args.get("low_stock_only", type=bool, default=False)
        pricing_user_id = request.args.get("pricing_user_id", type=int)

        # Build query
        query = Product.query

        # Apply filters
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    Product.name.ilike(search_term),
                    Product.sku.ilike(search_term),
                    Product.description.ilike(search_term),
                )
            )

        if category_id:
            query = query.filter_by(category_id=category_id)

        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        if low_stock_only:
            query = query.filter(
                and_(Product.track_inventory == True, Product.stock_quantity <= Product.min_stock_level)  # noqa: E712
            )

        # Order by name
        query = query.order_by(Product.name)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        pricing_map = {}
        if pricing_user_id and pagination.items:
            product_ids = [product.id for product in pagination.items]
            fallback_prices = {
                product.id: Decimal(str(product.calculate_price(quantity=1))) for product in pagination.items
            }
            pricing_map = get_corporate_contract_service().resolve_pricing_for_user_products(
                user_id=pricing_user_id,
                product_ids=product_ids,
                fallback_prices=fallback_prices,
            )

        serialized_items = []
        for product in pagination.items:
            serialized = serialize_product_admin(product)
            if pricing_user_id:
                resolved = pricing_map.get(product.id)
                if resolved:
                    serialized["effective_unit_price"] = float(resolved["unit_price"])
                    serialized["pricing_source"] = resolved["pricing_source"]
                    serialized["pricing_contract_id"] = resolved["contract"].id if resolved.get("contract") else None
                    serialized["pricing_contract_product_price_id"] = (
                        resolved["contract_price_row"].id if resolved.get("contract_price_row") else None
                    )
                else:
                    serialized["effective_unit_price"] = float(product.calculate_price(quantity=1))
                    serialized["pricing_source"] = "fallback"
                    serialized["pricing_contract_id"] = None
                    serialized["pricing_contract_product_price_id"] = None
            serialized_items.append(serialized)

        return paginated_response(items=serialized_items, page=page, per_page=per_page, total=pagination.total)

    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get products admin error: {e}")
        return internal_error_response("Failed to get products")


@admin_bp.route("/products", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def create_product():
    """Create a new product"""
    try:
        data = request.get_json()
        current_app.logger.info(f"/admin/products POST data: {data}")
        # Validate required fields
        required_fields = ["name"]
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return validation_error_response(errors={"missing_fields": missing_fields})

        # Map frontend field names to backend field names
        # Frontend sends 'price', backend uses 'base_price'
        base_price = data.get("price") or data.get("base_price")
        if not base_price:
            return validation_error_response(errors={"missing_fields": ["price"]})

        # Handle category - frontend may send category name or id
        category_id = data.get("category_id")
        if not category_id:
            return validation_error_response(errors={"missing_fields": ["category"]})

        # Handle size - derive from product name or use default
        size = data.get("size")
        if not size:
            # Try to extract size from product name
            name_lower = data["name"].lower()
            if "19" in name_lower or "19л" in name_lower:
                size = ProductSizeEnum.SIZE_19L
            elif "10" in name_lower or "10л" in name_lower:
                size = ProductSizeEnum.SIZE_10L
            elif "5" in name_lower or "5л" in name_lower:
                size = ProductSizeEnum.SIZE_5L
            else:
                # Default to 1L
                size = ProductSizeEnum.SIZE_19L
        elif isinstance(size, str):
            # Convert string to enum
            try:
                size = ProductSizeEnum(size)
            except ValueError:
                return validation_error_response(
                    errors={"size": f"Size must be one of: {[e.value for e in ProductSizeEnum]}"}
                )

        # Handle status - frontend sends 'active'/'inactive', backend uses is_active boolean
        is_active = data.get("is_active", True)
        if "status" in data:
            is_active = data["status"] in ["active", True, "true", 1]

        current_app.logger.info(f"/admin/products POST data.volume: {data.get('volume')}")
        # Create new product
        product = Product(
            name=data["name"],
            description=data.get("description"),
            short_description=data.get("short_description"),
            sku=data.get("sku"),
            base_price=base_price,
            discount_price=data.get("discount_price"),
            category_id=category_id,
            size=size,
            volume=data.get("volume"),
            volume_unit=data.get("volume_unit", "L"),
            weight=data.get("weight"),
            weight_unit=data.get("weight_unit", "kg"),
            is_active=is_active,
            is_featured=data.get("is_featured", False),
            requires_prescription=data.get("requires_prescription", False),
            track_inventory=data.get("track_inventory", True),
            is_tryout_eligible=data.get("is_tryout_eligible", True),
            tracks_returnable_bottles=data.get("tracks_returnable_bottles", False),
            returnable_bottles_per_unit=data.get("returnable_bottles_per_unit", 0),
            stock_quantity=data.get("stock_quantity", 0),
            min_stock_level=data.get("min_stock_level", 0),
            max_stock_level=data.get("max_stock_level", 1000),
            min_order_quantity=max(int(data.get("min_order_quantity", 1) or 1), 1),
            images=data.get("images", []),
            nutrition_facts=data.get("nutrition_facts", {}),
            ingredients=data.get("ingredients"),
            barcode=data.get("barcode"),
            slug=data.get("slug"),
            meta_title=data.get("meta_title"),
            meta_description=data.get("meta_description"),
            expire_days=data.get("expire_days"),
        )

        db.session.add(product)
        db.session.flush()

        fiscal_payload_keys = {
            "barcode",
            "spic",
            "package_code",
            "units",
            "vat_percent",
            "fiscalization_enabled",
            "requires_marking_codes",
            "fiscal_extra_data",
        }
        if any(key in data for key in fiscal_payload_keys):
            ProductFiscalService().update_product_fiscal_profile(
                product,
                data,
                actor_user_id=get_jwt_identity(),
            )

        db.session.commit()

        # Handle translations if provided
        if "translations" in data:
            product.set_translations(data["translations"])
            db.session.commit()

        return success_response(
            data={"product": serialize_product_admin(product)}, message="Product created successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create product error: {e}")
        return internal_error_response("Failed to create product")


@admin_bp.route("/products/<int:product_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
def update_product(product_id):
    """Update an existing product"""
    try:
        data = request.get_json()

        product = Product.query.get(product_id)
        if not product:
            return not_found_response(resource_type="Product")

        # Update fields
        if "name" in data:
            product.name = data["name"]
        if "description" in data:
            product.description = data["description"]
        if "short_description" in data:
            product.short_description = data["short_description"]
        if "sku" in data:
            product.sku = data["sku"]

        # Handle price field mapping (frontend sends 'price', backend uses 'base_price')
        if "price" in data:
            product.base_price = data["price"]
        elif "base_price" in data:
            product.base_price = data["base_price"]

        if "discount_price" in data:
            product.discount_price = data["discount_price"]

        # Handle category field mapping (frontend may send category name)
        if "category" in data and not "category_id" in data:  # noqa: E713
            from business_app.models.product import ProductCategory

            category = ProductCategory.query.filter(
                (ProductCategory.name.ilike(data["category"])) | (ProductCategory.id == data["category"])
                if isinstance(data["category"], int)
                else False
            ).first()
            if category:
                product.category_id = category.id
        elif "category_id" in data:
            product.category_id = data["category_id"]

        # Handle size field
        if "size" in data:
            from business_app.models.product import ProductSizeEnum

            if isinstance(data["size"], str):
                try:
                    product.size = ProductSizeEnum(data["size"])
                except ValueError:
                    pass  # Keep existing size if invalid
            else:
                product.size = data["size"]

        if "volume" in data:
            product.volume = data["volume"]
        if "volume_unit" in data:
            product.volume_unit = data["volume_unit"]
        if "weight" in data:
            product.weight = data["weight"]
        if "weight_unit" in data:
            product.weight_unit = data["weight_unit"]

        # Handle status field mapping (frontend sends 'status': 'active'/'inactive')
        if "status" in data:
            product.is_active = data["status"] in ["active", True, "true", 1]
        elif "is_active" in data:
            product.is_active = data["is_active"]

        if "is_featured" in data:
            product.is_featured = data["is_featured"]
        if "requires_prescription" in data:
            product.requires_prescription = data["requires_prescription"]
        if "track_inventory" in data:
            product.track_inventory = data["track_inventory"]
        if "is_tryout_eligible" in data:
            product.is_tryout_eligible = data["is_tryout_eligible"]
        if "tracks_returnable_bottles" in data:
            product.tracks_returnable_bottles = data["tracks_returnable_bottles"]
        if "returnable_bottles_per_unit" in data:
            product.returnable_bottles_per_unit = data["returnable_bottles_per_unit"]
        if "stock_quantity" in data and not product.requires_marking_codes:
            product.stock_quantity = data["stock_quantity"]
        if "expire_days" in data:
            product.expire_days = data["expire_days"]
        if "min_stock_level" in data:
            product.min_stock_level = data["min_stock_level"]
        if "max_stock_level" in data:
            product.max_stock_level = data["max_stock_level"]
        if "min_order_quantity" in data and data["min_order_quantity"] is not None:
            min_qty = int(data["min_order_quantity"])
            if min_qty < 1:
                return validation_error_response(errors={"min_order_quantity": "Must be at least 1"})
            product.min_order_quantity = min_qty
        if "images" in data:
            product.images = data["images"]
        if "nutrition_facts" in data:
            product.nutrition_facts = data["nutrition_facts"]
        if "ingredients" in data:
            product.ingredients = data["ingredients"]
        if "barcode" in data:
            product.barcode = data["barcode"]
        if "slug" in data:
            product.slug = data["slug"]
        if "meta_title" in data:
            product.meta_title = data["meta_title"]
        if "meta_description" in data:
            product.meta_description = data["meta_description"]

        # Handle translations if provided
        if "translations" in data:
            product.set_translations(data["translations"])

        fiscal_payload_keys = {
            "barcode",
            "spic",
            "package_code",
            "units",
            "vat_percent",
            "fiscalization_enabled",
            "requires_marking_codes",
            "fiscal_extra_data",
        }
        if any(key in data for key in fiscal_payload_keys):
            ProductFiscalService().update_product_fiscal_profile(
                product,
                data,
                actor_user_id=get_jwt_identity(),
            )

        product.updated_at = datetime.now(UTC)
        db.session.commit()

        return success_response(
            data={"product": serialize_product_admin(product)}, message="Product updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product error: {e}")
        return internal_error_response("Failed to update product")


@admin_bp.route("/products/<int:product_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_products"])
def delete_product(product_id):
    """Delete a product"""
    try:
        product = Product.query.get(product_id)
        if not product:
            return not_found_response(resource_type="Product")

        # Soft delete by setting is_active to False
        product.is_active = False
        product.updated_at = datetime.now(UTC)
        db.session.commit()

        return success_response(message="Product deleted successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete product error: {e}")
        return internal_error_response("Failed to delete product")


@admin_bp.route("/products/<int:product_id>/marking-codes", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def list_product_marking_codes(product_id):
    """List marking codes for a product."""
    try:
        payload = ProductFiscalService().list_marking_codes(
            product_id,
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 50, type=int),
            search=request.args.get("search", "").strip(),
            status=request.args.get("status"),
        )
        return success_response(data=payload)
    except ValidationError as e:
        return validation_error_response(str(e))
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"List product marking codes error: {e}")
        return internal_error_response("Failed to load product marking codes")


@admin_bp.route("/products/<int:product_id>/marking-codes", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def create_product_marking_codes(product_id):
    """Create marking codes manually for a product."""
    try:
        payload = request.get_json() or {}
        codes = payload.get("codes")
        if codes is None and payload.get("code"):
            codes = [payload.get("code")]
        result = ProductFiscalService().create_marking_codes(
            product_id,
            codes or [],
            actor_user_id=get_jwt_identity(),
            notes=payload.get("notes"),
        )
        db.session.commit()
        return created_response(data=result, message="Marking codes created successfully")
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(message=str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create product marking codes error: {e}")
        return internal_error_response("Failed to create product marking codes")


@admin_bp.route("/products/<int:product_id>/marking-codes/import", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def import_product_marking_codes(product_id):
    """Import marking codes from CSV for a product."""
    try:
        csv_content = None
        if request.files.get("file"):
            csv_content = request.files["file"].read().decode("utf-8")
        else:
            payload = request.get_json() or {}
            csv_content = payload.get("csv_content") or ""

        result = ProductFiscalService().import_marking_codes_csv(
            product_id,
            csv_content,
            actor_user_id=get_jwt_identity(),
        )
        db.session.commit()
        return created_response(data=result, message="Marking codes imported successfully")
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(message=str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Import product marking codes error: {e}")
        return internal_error_response("Failed to import product marking codes")


@admin_bp.route("/products/<int:product_id>/marking-codes/export", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def export_product_marking_codes(product_id):
    """Export product marking codes as CSV."""
    try:
        csv_payload = ProductFiscalService().export_marking_codes_csv(
            product_id,
            status=request.args.get("status"),
        )
        return Response(
            csv_payload,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=product-{product_id}-marking-codes.csv"},
        )
    except ValidationError as e:
        return validation_error_response(str(e))
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Export product marking codes error: {e}")
        return internal_error_response("Failed to export product marking codes")


@admin_bp.route("/products/<int:product_id>/marking-codes/<int:marking_code_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
def update_product_marking_code(product_id, marking_code_id):
    """Update one marking code record."""
    try:
        payload = request.get_json() or {}
        marking_code = ProductFiscalService().update_marking_code(
            product_id,
            marking_code_id,
            payload,
            actor_user_id=get_jwt_identity(),
        )
        db.session.commit()
        return success_response(
            data={"marking_code": marking_code.to_dict()},
            message="Marking code updated successfully",
        )
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(message=str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product marking code error: {e}")
        return internal_error_response("Failed to update product marking code")


@admin_bp.route("/products/<int:product_id>/stock", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
@validate_json(["stock_quantity"])
def update_product_stock(product_id):
    """Update product stock"""
    try:
        get_jwt_identity()
        data = request.get_json()

        product = Product.query.get(product_id)
        if not product:
            return not_found_response(resource_type="Product")

        new_stock = data.get("stock_quantity")
        data.get("reason", "Manual adjustment")

        if not isinstance(new_stock, int) or new_stock < 0:
            return validation_error_response("Invalid stock quantity")

        if product.requires_marking_codes:
            return validation_error_response(
                "Stock quantity is derived from available marking codes for this product. "
                "Add or manage marking codes instead of setting stock directly."
            )

        product.stock_quantity
        product.stock_quantity = new_stock
        product.updated_at = datetime.now(UTC)

        # Log stock adjustment (placeholder until admin_service is implemented)
        # admin_service.log_admin_action(
        #     admin_id=current_user_id,
        #     action='stock_adjusted',
        #     target_type='product',
        #     target_id=product_id,
        #     details=f"Stock changed from {old_stock} to {new_stock}. Reason: {adjustment_reason}"
        # )

        db.session.commit()

        return success_response(
            data={"product": serialize_product_admin(product)}, message="Product stock updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update product stock error: {e}")
        return internal_error_response("Failed to update product stock")


# ============================================================================
# PRODUCT CATEGORY MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/categories", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_categories():
    """Get all product categories with filtering and search"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        is_active = request.args.get("is_active", type=bool)
        sort_by = request.args.get("sort_by", "sort_order")  # sort_order, name, created_at

        # Build query
        query = ProductCategory.query

        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(ProductCategory.name.ilike(search_term), ProductCategory.description.ilike(search_term))
            )

        # Apply sorting
        if sort_by == "name":
            query = query.order_by(ProductCategory.name)
        elif sort_by == "created_at":
            query = query.order_by(ProductCategory.created_at.desc())
        else:  # Default: sort_order
            query = query.order_by(ProductCategory.sort_order, ProductCategory.name)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize categories with product count
        categories_data = []
        for category in pagination.items:
            product_count = Product.query.filter_by(category_id=category.id, is_active=True).count()

            # Use serialize_category_admin
            category_dict = serialize_category_admin(category)

            # Add computed fields
            category_dict["product_count"] = product_count

            categories_data.append(category_dict)

        return paginated_response(items=categories_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get categories error: {e}")
        return internal_error_response("Failed to get categories")


@admin_bp.route("/categories/<int:category_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_category(category_id):
    """Get specific category details"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response("Category not found")

        # Get products in this category
        products = Product.query.filter_by(category_id=category_id).all()

        # Use serialize_category_admin
        category_data = serialize_category_admin(category)

        # Add computed fields
        category_data.update(
            {
                "product_count": len(products),
                "active_product_count": len([p for p in products if p.is_active]),
                "products": [
                    {"id": p.id, "name": p.name, "sku": p.sku, "is_active": p.is_active} for p in products[:10]
                ],  # First 10 products
            }
        )

        return success_response(data={"category": category_data})

    except Exception as e:
        current_app.logger.error(f"Get category error: {e}")
        return internal_error_response("Failed to get category")


@admin_bp.route("/categories", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def create_category():
    """Create a new product category"""
    try:
        data = request.get_json()

        # Validate required fields
        if not data.get("name"):
            return validation_error_response("Category name is required")

        # Check if category with same name already exists
        existing = ProductCategory.query.filter_by(name=data["name"]).first()
        if existing:
            return validation_error_response("Category with this name already exists")

        # Create new category
        category = ProductCategory(
            name=data["name"],
            description=data.get("description"),
            is_active=data.get("is_active", True),
            sort_order=data.get("sort_order", 0),
            icon_url=data.get("icon_url"),
        )

        db.session.add(category)
        db.session.commit()

        # Handle translations if provided
        if "translations" in data:
            category.set_translations(data["translations"])
            db.session.commit()  # Commit translations

        current_app.logger.info(f"Category created: {category.name} (ID: {category.id})")

        return success_response(
            data={"category": serialize_category_admin(category)},
            message="Category created successfully",
            status_code=201,
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create category error: {e}")
        return internal_error_response("Failed to create category")


@admin_bp.route("/categories/<int:category_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
def update_category(category_id):
    """Update a product category"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response("Category not found")

        data = request.get_json()

        # Check if name is being changed to an existing name
        if "name" in data and data["name"] != category.name:
            existing = ProductCategory.query.filter_by(name=data["name"]).first()
            if existing:
                return validation_error_response("Category with this name already exists")

        # Update fields
        if "name" in data:
            category.name = data["name"]
        if "description" in data:
            category.description = data["description"]
        if "is_active" in data:
            category.is_active = data["is_active"]
        if "sort_order" in data:
            category.sort_order = data["sort_order"]
        if "icon_url" in data:
            category.icon_url = data["icon_url"]

        # Handle translations if provided
        if "translations" in data:
            category.set_translations(data["translations"])

        db.session.commit()

        current_app.logger.info(f"Category updated: {category.name} (ID: {category.id})")

        return success_response(
            data={"category": serialize_category_admin(category)}, message="Category updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update category error: {e}")
        return internal_error_response("Failed to update category")


@admin_bp.route("/categories/<int:category_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_products"])
def delete_category(category_id):
    """Delete a product category (soft delete by setting inactive)"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response("Category not found")

        # Check if category has products
        product_count = Product.query.filter_by(category_id=category_id).count()

        force_delete = request.args.get("force", "false").lower() == "true"

        if product_count > 0 and not force_delete:
            return validation_error_response(
                f"Cannot delete category with {product_count} products. Set force=true to deactivate instead."
            )

        if force_delete or product_count > 0:
            # Soft delete: just deactivate
            category.is_active = False
            db.session.commit()
            current_app.logger.info(f"Category deactivated: {category.name} (ID: {category.id})")
            return success_response(message=f"Category deactivated (has {product_count} products)")
        else:
            # Hard delete if no products
            category_name = category.name
            db.session.delete(category)
            db.session.commit()
            current_app.logger.info(f"Category deleted: {category_name} (ID: {category_id})")
            return success_response(message=get_translation("api.admin.success.category_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete category error: {e}")
        return internal_error_response("Failed to delete category")


@admin_bp.route("/categories/<int:category_id>/reorder", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
def reorder_category(category_id):
    """Update category sort order"""
    try:
        category = ProductCategory.query.get(category_id)

        if not category:
            return not_found_response("Category not found")

        data = request.get_json()
        new_sort_order = data.get("sort_order")

        if new_sort_order is None:
            return validation_error_response("sort_order is required")

        category.sort_order = new_sort_order
        db.session.commit()

        return success_response(message=get_translation("api.admin.success.category_order_updated"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Reorder category error: {e}")
        return internal_error_response("Failed to reorder category")


# ==================== DELIVERY TIME SLOT MANAGEMENT ====================


@admin_bp.route("/delivery/time-slots", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_time_slots_admin():
    """Get all delivery time slots with filtering"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        is_active = request.args.get("is_active", type=bool)

        # Build query
        query = DeliveryTimeSlot.query

        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        # Order by start time
        query = query.order_by(DeliveryTimeSlot.start_time)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize time slots
        items = []
        for slot in pagination.items:
            items.append(
                {
                    "id": slot.id,
                    "name": slot.name,
                    "start_time": slot.start_time,
                    "end_time": slot.end_time,
                    "time_range": f"{slot.start_time}-{slot.end_time}",
                    "is_active": slot.is_active,
                    "max_orders": slot.max_orders,
                    "delivery_fee": float(slot.delivery_fee),
                    "is_premium": slot.is_premium,
                    "premium_fee": float(slot.premium_fee),
                    "available_days": slot.available_days,
                }
            )

        return paginated_response(items=items, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get time slots error: {e}")
        return internal_error_response("Failed to get time slots")


@admin_bp.route("/delivery/time-slots/<int:slot_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_time_slot_admin(slot_id):
    """Get a specific time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response("Time slot not found")

        return success_response(
            data={
                "id": slot.id,
                "name": slot.name,
                "start_time": slot.start_time,
                "end_time": slot.end_time,
                "is_active": slot.is_active,
                "max_orders": slot.max_orders,
                "delivery_fee": float(slot.delivery_fee),
                "is_premium": slot.is_premium,
                "premium_fee": float(slot.premium_fee),
                "available_days": slot.available_days,
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get time slot error: {e}")
        return internal_error_response("Failed to get time slot")


@admin_bp.route("/delivery/time-slots", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
def create_time_slot():
    """Create a new delivery time slot"""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ["name", "start_time", "end_time", "max_orders", "delivery_fee"]
        for field in required_fields:
            if field not in data:
                return validation_error_response(f"{field} is required")

        # Create time slot
        time_slot = DeliveryTimeSlot(
            name=data["name"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            is_active=data.get("is_active", True),
            max_orders=data["max_orders"],
            delivery_fee=data["delivery_fee"],
            is_premium=data.get("is_premium", False),
            premium_fee=data.get("premium_fee", 0),
            available_days=data.get("available_days", [0, 1, 2, 3, 4, 5, 6]),
        )

        db.session.add(time_slot)
        db.session.commit()

        current_app.logger.info(f"Time slot created: {time_slot.name} (ID: {time_slot.id})")

        return created_response(message="Time slot created successfully", data={"id": time_slot.id})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create time slot error: {e}")
        return internal_error_response("Failed to create time slot")


@admin_bp.route("/delivery/time-slots/<int:slot_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
def update_time_slot(slot_id):
    """Update a delivery time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response("Time slot not found")

        data = request.get_json()

        # Update fields
        if "name" in data:
            slot.name = data["name"]
        if "start_time" in data:
            slot.start_time = data["start_time"]
        if "end_time" in data:
            slot.end_time = data["end_time"]
        if "is_active" in data:
            slot.is_active = data["is_active"]
        if "max_orders" in data:
            slot.max_orders = data["max_orders"]
        if "delivery_fee" in data:
            slot.delivery_fee = data["delivery_fee"]
        if "is_premium" in data:
            slot.is_premium = data["is_premium"]
        if "premium_fee" in data:
            slot.premium_fee = data["premium_fee"]
        if "available_days" in data:
            slot.available_days = data["available_days"]

        db.session.commit()

        current_app.logger.info(f"Time slot updated: {slot.name} (ID: {slot_id})")

        return success_response(message=get_translation("api.admin.success.time_slot_updated"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update time slot error: {e}")
        return internal_error_response("Failed to update time slot")


@admin_bp.route("/delivery/time-slots/<int:slot_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
def delete_time_slot(slot_id):
    """Delete a delivery time slot"""
    try:
        slot = DeliveryTimeSlot.query.get(slot_id)

        if not slot:
            return not_found_response("Time slot not found")

        slot_name = slot.name
        db.session.delete(slot)
        db.session.commit()

        current_app.logger.info(f"Time slot deleted: {slot_name} (ID: {slot_id})")

        return success_response(message=get_translation("api.admin.success.time_slot_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete time slot error: {e}")
        return internal_error_response("Failed to delete time slot")


@admin_bp.route("/deliveries", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_admin_deliveries():
    """List deliveries for the admin deliveries management UI."""
    try:
        result = AdminDeliveryService.list_deliveries(
            page=int(request.args.get("page", 1)),
            per_page=int(request.args.get("per_page", 20)),
            search=request.args.get("search", ""),
            status=request.args.get("status"),
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
        )

        return paginated_response(
            items=result["items"],
            page=result["page"],
            per_page=result["per_page"],
            total=result["total"],
            additional_meta={"summary": result["summary"]},
        )
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get admin deliveries error: {e}")
        return internal_error_response("Failed to get deliveries")


@admin_bp.route("/deliveries/<int:delivery_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
@validate_json()
def update_admin_delivery(delivery_id):
    """Update delivery notes/status from the admin deliveries page."""
    try:
        delivery = AdminDeliveryService.update_delivery(
            delivery_id,
            request.get_json() or {},
            int(get_jwt_identity()),
        )
        return success_response(
            data={"delivery": delivery},
            message="Delivery updated successfully",
        )
    except NotFoundError as e:
        return not_found_response(str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Update admin delivery error: {e}")
        return internal_error_response("Failed to update delivery")


@admin_bp.route("/deliveries/<int:delivery_id>/redispatch", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
def redispatch_admin_delivery(delivery_id):
    """Re-dispatch a failed delivery back to the unassigned pool so it can be
    re-claimed by a driver. Only valid for deliveries in FAILED status."""
    try:
        payload = request.get_json(silent=True) or {}
        reason = (payload.get("reason") or "").strip() or None
        delivery = AdminDeliveryService.redispatch_delivery(
            delivery_id,
            int(get_jwt_identity()),
            reason=reason,
        )
        return success_response(
            data={"delivery": delivery},
            message="Delivery re-dispatched to pool",
        )
    except NotFoundError as e:
        return not_found_response(str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Redispatch admin delivery error: {e}")
        return internal_error_response("Failed to re-dispatch delivery")


@admin_bp.route("/delivery-personnel", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_delivery_personnel():
    """Get delivery personnel"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        is_active = request.args.get("is_active", type=bool)
        search = request.args.get("search", "").strip()

        # Build query
        query = DeliveryPerson.query

        # Apply filters
        if is_active is not None:
            query = query.filter_by(is_active=is_active)

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    DeliveryPerson.full_name.ilike(search_term),
                    DeliveryPerson.phone.ilike(search_term),
                    DeliveryPerson.vehicle_number.ilike(search_term),
                )
            )

        # Order by name
        query = query.order_by(DeliveryPerson.full_name)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return paginated_response(
            items=_serialize_delivery_person_admin_items(pagination.items),
            page=page,
            per_page=per_page,
            total=pagination.total,
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery personnel error: {e}")
        return internal_error_response("Failed to get delivery personnel")


# ============================================================================
# DELIVERY ROUTE MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/delivery-routes", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_delivery_routes():
    """Get delivery routes with filtering and search"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)

        # Filters
        status = request.args.get("status")  # planned, in_progress, completed, cancelled
        delivery_person_id = request.args.get("delivery_person_id", type=int)
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        search = request.args.get("search", "").strip()

        # Build query
        query = DeliveryRoute.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if delivery_person_id:
            query = query.filter_by(delivery_person_id=delivery_person_id)

        if date_from:
            try:
                date_from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
                query = query.filter(DeliveryRoute.route_date >= date_from_dt)
            except ValueError:
                return validation_error_response("Invalid date_from format")

        if date_to:
            try:
                date_to_dt = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
                query = query.filter(DeliveryRoute.route_date <= date_to_dt)
            except ValueError:
                return validation_error_response("Invalid date_to format")

        if search:
            search_term = f"%{search}%"
            query = query.join(User, DeliveryRoute.delivery_person_id == User.id).filter(
                or_(
                    DeliveryRoute.name.ilike(search_term),
                    User.first_name.ilike(search_term),
                    User.last_name.ilike(search_term),
                )
            )

        # Order by date descending
        query = query.order_by(DeliveryRoute.route_date.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize routes
        routes_data = []
        for route in pagination.items:
            route_dict = route.to_dict()

            # Add order count
            route_dict["order_count"] = len(route.optimized_order) if route.optimized_order else 0

            routes_data.append(route_dict)

        return paginated_response(items=routes_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get delivery routes error: {e}")
        return internal_error_response("Failed to get delivery routes")


@admin_bp.route("/delivery-routes/<int:route_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery", "manage_delivery"])
def get_delivery_route(route_id):
    """Get detailed delivery route information"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response("Delivery route not found")

        route_dict = route.to_dict()

        # Get deliveries for this route
        if route.optimized_order:
            deliveries = Delivery.query.filter(Delivery.order_id.in_(route.optimized_order)).all()

            route_dict["deliveries"] = [
                {
                    "id": d.id,
                    "order_id": d.order_id,
                    "tracking_number": d.tracking_number,
                    "status": d.status.value,
                    "scheduled_time_slot": d.scheduled_time_slot,
                    "delivery_address": d.order.delivery_address if d.order else None,
                    "customer_name": d.order.user.full_name if d.order and d.order.user else None,
                    "customer_phone": d.order.user.phone if d.order and d.order.user else None,
                    "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
                }
                for d in deliveries
            ]
        else:
            route_dict["deliveries"] = []

        return success_response(data={"route": route_dict})

    except Exception as e:
        current_app.logger.error(f"Get delivery route error: {e}")
        return internal_error_response("Failed to get delivery route")


@admin_bp.route("/delivery-routes", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
@validate_json()
def create_delivery_route():
    """Create new delivery route"""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ["name", "delivery_person_id", "route_date", "start_location_lat", "start_location_lng"]
        for field in required_fields:
            if field not in data:
                return validation_error_response(f"Missing required field: {field}")

        # Validate delivery person exists
        delivery_person = User.query.get(data["delivery_person_id"])
        if not delivery_person:
            return not_found_response("Delivery person not found")

        # Parse route date
        try:
            route_date = datetime.fromisoformat(data["route_date"].replace("Z", "+00:00"))
        except ValueError:
            return validation_error_response("Invalid route_date format")

        # Create route
        route = DeliveryRoute(
            name=data["name"],
            delivery_person_id=data["delivery_person_id"],
            route_date=route_date,
            start_location_lat=data["start_location_lat"],
            start_location_lng=data["start_location_lng"],
            optimized_order=data.get("optimized_order", []),
            total_distance_km=data.get("total_distance_km"),
            estimated_duration_minutes=data.get("estimated_duration_minutes"),
            notes=data.get("notes"),
        )

        db.session.add(route)
        db.session.commit()

        return success_response(
            data={"route": route.to_dict()}, message="Delivery route created successfully", status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create delivery route error: {e}")
        return internal_error_response("Failed to create delivery route")


@admin_bp.route("/delivery-routes/<int:route_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
@validate_json()
def update_delivery_route(route_id):
    """Update delivery route"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response("Delivery route not found")

        # Don't allow updates to completed routes
        if route.status == "completed":
            return validation_error_response("Cannot update completed routes")

        data = request.get_json()

        # Update basic fields
        if "name" in data:
            route.name = data["name"]

        if "delivery_person_id" in data:
            delivery_person = User.query.get(data["delivery_person_id"])
            if not delivery_person:
                return not_found_response("Delivery person not found")
            route.delivery_person_id = data["delivery_person_id"]

        if "route_date" in data:
            try:
                route.route_date = datetime.fromisoformat(data["route_date"].replace("Z", "+00:00"))
            except ValueError:
                return validation_error_response("Invalid route_date format")

        if "start_location_lat" in data:
            route.start_location_lat = data["start_location_lat"]

        if "start_location_lng" in data:
            route.start_location_lng = data["start_location_lng"]

        if "optimized_order" in data:
            route.optimized_order = data["optimized_order"]

        if "total_distance_km" in data:
            route.total_distance_km = data["total_distance_km"]

        if "estimated_duration_minutes" in data:
            route.estimated_duration_minutes = data["estimated_duration_minutes"]

        if "notes" in data:
            route.notes = data["notes"]

        if "extra_data" in data:
            route.extra_data = data["extra_data"]

        db.session.commit()

        return success_response(data={"route": route.to_dict()}, message="Delivery route updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update delivery route error: {e}")
        return internal_error_response("Failed to update delivery route")


@admin_bp.route("/delivery-routes/<int:route_id>/status", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
@validate_json()
def update_route_status(route_id):
    """Update delivery route status"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response("Delivery route not found")

        data = request.get_json()
        new_status = data.get("status")

        if not new_status:
            return validation_error_response("Status is required")

        valid_statuses = ["planned", "in_progress", "completed", "cancelled"]
        if new_status not in valid_statuses:
            return validation_error_response(f'Invalid status. Must be one of: {", ".join(valid_statuses)}')

        old_status = route.status

        # Validate status transitions
        if old_status == "completed" and new_status != "completed":
            return validation_error_response("Cannot change status of completed route")

        if old_status == "cancelled" and new_status not in ["planned", "cancelled"]:
            return validation_error_response("Cancelled route can only be set to planned")

        # Update status and timestamps
        route.status = new_status

        if new_status == "in_progress" and not route.started_at:
            route.started_at = datetime.now(UTC)

        if new_status == "completed":
            route.completed_at = datetime.now(UTC)

            # Update actual metrics if provided
            if "actual_distance_km" in data:
                route.actual_distance_km = data["actual_distance_km"]

            if "actual_duration_minutes" in data:
                route.actual_duration_minutes = data["actual_duration_minutes"]

            if "deliveries_completed" in data:
                route.deliveries_completed = data["deliveries_completed"]

            if "deliveries_failed" in data:
                route.deliveries_failed = data["deliveries_failed"]

        db.session.commit()

        return success_response(data={"route": route.to_dict()}, message=f"Route status updated to {new_status}")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update route status error: {e}")
        return internal_error_response("Failed to update route status")


@admin_bp.route("/delivery-routes/<int:route_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_delivery"])
def delete_delivery_route(route_id):
    """Delete delivery route"""
    try:
        route = DeliveryRoute.query.get(route_id)

        if not route:
            return not_found_response("Delivery route not found")

        # Don't allow deletion of in-progress or completed routes
        if route.status in ["in_progress", "completed"]:
            return validation_error_response(f"Cannot delete {route.status} routes")

        db.session.delete(route)
        db.session.commit()

        return success_response(message=get_translation("api.admin.success.delivery_route_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete delivery route error: {e}")
        return internal_error_response("Failed to delete delivery route")


@admin_bp.route("/delivery-routes/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_delivery"])
def get_delivery_routes_analytics():
    """Get delivery routes analytics"""
    try:
        # Date range
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        # Default to last 30 days
        now = datetime.now(UTC)
        start_date = now - timedelta(days=30)
        end_date = now

        if date_from:
            try:
                start_date = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
            except ValueError:
                return validation_error_response("Invalid date_from format")

        if date_to:
            try:
                end_date = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
            except ValueError:
                return validation_error_response("Invalid date_to format")

        # Total routes
        total_routes = DeliveryRoute.query.filter(
            and_(DeliveryRoute.route_date >= start_date, DeliveryRoute.route_date <= end_date)
        ).count()

        # Routes by status
        routes_by_status = (
            db.session.query(DeliveryRoute.status, func.count(DeliveryRoute.id).label("count"))
            .filter(and_(DeliveryRoute.route_date >= start_date, DeliveryRoute.route_date <= end_date))
            .group_by(DeliveryRoute.status)
            .all()
        )

        status_breakdown = {status: count for status, count in routes_by_status}

        # Average metrics for completed routes
        completed_routes = DeliveryRoute.query.filter(
            and_(
                DeliveryRoute.route_date >= start_date,
                DeliveryRoute.route_date <= end_date,
                DeliveryRoute.status == "completed",
            )
        ).all()

        total_distance = sum(r.actual_distance_km or 0 for r in completed_routes)
        total_duration = sum(r.actual_duration_minutes or 0 for r in completed_routes)
        total_deliveries = sum(r.deliveries_completed or 0 for r in completed_routes)
        total_failed = sum(r.deliveries_failed or 0 for r in completed_routes)

        avg_distance = total_distance / len(completed_routes) if completed_routes else 0
        avg_duration = total_duration / len(completed_routes) if completed_routes else 0
        avg_deliveries_per_route = total_deliveries / len(completed_routes) if completed_routes else 0
        success_rate = (
            (total_deliveries / (total_deliveries + total_failed) * 100) if (total_deliveries + total_failed) > 0 else 0
        )

        # Top performing delivery personnel
        top_delivery_persons = (
            db.session.query(
                User.id,
                func.concat(User.first_name, " ", User.last_name).label("full_name"),
                func.count(DeliveryRoute.id).label("route_count"),
                func.sum(DeliveryRoute.deliveries_completed).label("total_deliveries"),
                func.sum(DeliveryRoute.actual_distance_km).label("total_distance"),
            )
            .join(DeliveryRoute, DeliveryRoute.delivery_person_id == User.id)
            .filter(
                and_(
                    DeliveryRoute.route_date >= start_date,
                    DeliveryRoute.route_date <= end_date,
                    DeliveryRoute.status == "completed",
                )
            )
            .group_by(User.id, func.concat(User.first_name, " ", User.last_name))
            .order_by(func.sum(DeliveryRoute.deliveries_completed).desc())
            .limit(10)
            .all()
        )

        top_performers = [
            {
                "person_id": p.id,
                "person_name": p.full_name,
                "route_count": p.route_count,
                "total_deliveries": p.total_deliveries or 0,
                "total_distance_km": float(p.total_distance or 0),
            }
            for p in top_delivery_persons
        ]

        # Daily route completion trend
        daily_trend = (
            db.session.query(
                func.date(DeliveryRoute.route_date).label("date"),
                func.count(DeliveryRoute.id).label("routes"),
                func.sum(DeliveryRoute.deliveries_completed).label("deliveries"),
            )
            .filter(
                and_(
                    DeliveryRoute.route_date >= start_date,
                    DeliveryRoute.route_date <= end_date,
                    DeliveryRoute.status == "completed",
                )
            )
            .group_by(func.date(DeliveryRoute.route_date))
            .order_by(func.date(DeliveryRoute.route_date))
            .all()
        )

        daily_data = [
            {"date": d.date.isoformat(), "routes": d.routes, "deliveries": d.deliveries or 0} for d in daily_trend
        ]

        analytics = {
            "summary": {
                "total_routes": total_routes,
                "completed_routes": status_breakdown.get("completed", 0),
                "in_progress_routes": status_breakdown.get("in_progress", 0),
                "planned_routes": status_breakdown.get("planned", 0),
                "cancelled_routes": status_breakdown.get("cancelled", 0),
            },
            "performance": {
                "avg_distance_km": round(avg_distance, 2),
                "avg_duration_minutes": round(avg_duration, 2),
                "avg_deliveries_per_route": round(avg_deliveries_per_route, 2),
                "success_rate": round(success_rate, 2),
                "total_deliveries_completed": total_deliveries,
                "total_deliveries_failed": total_failed,
            },
            "status_breakdown": status_breakdown,
            "top_performers": top_performers,
            "daily_trend": daily_data,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        }

        return success_response(data=analytics)

    except Exception as e:
        current_app.logger.error(f"Get delivery routes analytics error: {e}")
        return internal_error_response("Failed to get delivery routes analytics")


@admin_bp.route("/analytics/inactive-customers", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_users"])
def get_inactive_customers():
    """List customers inactive for at least N days, sorted most-inactive-first."""
    try:
        params = InactiveCustomersQuerySchema.model_validate(request.args.to_dict())
    except PydanticValidationError as e:
        return validation_error_response(str(e))

    try:
        result = get_analytics_service().get_inactive_customers(**params.model_dump())
    except Exception as e:
        current_app.logger.error(f"Get inactive customers error: {e}")
        return internal_error_response("Failed to get inactive customers")

    return paginated_response(
        items=result["items"],
        page=params.page,
        per_page=params.per_page,
        total=result["total"],
    )


# ============================================================================
# SUBSCRIPTION MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/subscriptions", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_subscriptions():
    """Get all subscriptions with filtering and search"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        status = request.args.get("status")  # active, paused, cancelled, expired
        user_id = request.args.get("user_id", type=int)
        sort_by = request.args.get("sort_by", "created_at")  # created_at, next_billing_date, billing_amount

        # Build query
        query = Subscription.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if user_id:
            query = query.filter_by(user_id=user_id)

        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(
                or_(
                    Subscription.subscription_number.ilike(search_term),
                    Subscription.name.ilike(search_term),
                    User.first_name.ilike(search_term),
                    User.last_name.ilike(search_term),
                    User.email.ilike(search_term),
                )
            )

        # Apply sorting
        if sort_by == "next_billing_date":
            query = query.order_by(Subscription.next_billing_date.desc())
        elif sort_by == "billing_amount":
            query = query.order_by(Subscription.billing_amount.desc())
        else:  # Default: created_at
            query = query.order_by(Subscription.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize subscriptions
        subscriptions_data = []
        for sub in pagination.items:
            sub_dict = {
                "id": sub.id,
                "subscription_number": sub.subscription_number,
                "user_id": sub.user_id,
                "user_name": f"{sub.user.first_name} {sub.user.last_name or ''}".strip(),
                "user_email": sub.user.email,
                "status": sub.status,
                "name": sub.name,
                "description": sub.description,
                "billing_cycle": (
                    sub.billing_cycle.value if hasattr(sub.billing_cycle, "value") else str(sub.billing_cycle)
                ),
                "billing_amount": float(sub.billing_amount),
                "next_billing_date": sub.next_billing_date.isoformat() if sub.next_billing_date else None,
                "delivery_frequency": (
                    sub.delivery_frequency.value
                    if hasattr(sub.delivery_frequency, "value")
                    else str(sub.delivery_frequency)
                ),
                "auto_renew": sub.auto_renew,
                "paused_at": sub.paused_at.isoformat() if sub.paused_at else None,
                "pause_reason": sub.pause_reason,
                "resume_date": sub.resume_date.isoformat() if sub.resume_date else None,
                "total_orders_generated": sub.total_orders_generated,
                "total_amount_billed": float(sub.total_amount_billed),
                "items_count": len(sub.subscription_items),
                "created_at": sub.created_at.isoformat() if sub.created_at else None,
            }
            subscriptions_data.append(sub_dict)

        return paginated_response(items=subscriptions_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get subscriptions error: {e}")
        return internal_error_response("Failed to get subscriptions")


@admin_bp.route("/subscriptions/<int:subscription_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_subscription(subscription_id):
    """Get detailed subscription information"""
    try:
        subscription = Subscription.query.get(subscription_id)

        if not subscription:
            return not_found_response("Subscription not found")

        # Get subscription items
        items = []
        for item in subscription.subscription_items:
            items.append(
                {
                    "id": item.id,
                    "product_id": item.product_id,
                    "product_name": item.product.name if item.product else None,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price),
                    "subtotal": float(item.subtotal),
                }
            )

        # Get recent orders
        recent_orders = (
            Order.query.filter_by(subscription_id=subscription_id).order_by(Order.created_at.desc()).limit(10).all()
        )
        orders = [
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status,
                "total_amount": float(o.total_amount),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in recent_orders
        ]

        # Get subscription logs
        logs = []
        for log in subscription.subscription_logs[-10:]:  # Last 10 logs
            logs.append(
                {
                    "id": log.id,
                    "action": log.action,
                    "details": log.details,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
            )

        subscription_data = {
            "id": subscription.id,
            "subscription_number": subscription.subscription_number,
            "user": {
                "id": subscription.user.id,
                "name": f"{subscription.user.first_name} {subscription.user.last_name or ''}".strip(),
                "email": subscription.user.email,
                "phone": subscription.user.phone,
            },
            "status": subscription.status,
            "name": subscription.name,
            "description": subscription.description,
            "billing_cycle": (
                subscription.billing_cycle.value
                if hasattr(subscription.billing_cycle, "value")
                else str(subscription.billing_cycle)
            ),
            "billing_amount": float(subscription.billing_amount),
            "next_billing_date": subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
            "last_billing_date": subscription.last_billing_date.isoformat() if subscription.last_billing_date else None,
            "delivery_frequency": (
                subscription.delivery_frequency.value
                if hasattr(subscription.delivery_frequency, "value")
                else str(subscription.delivery_frequency)
            ),
            "delivery_day_of_week": subscription.delivery_day_of_week,
            "delivery_day_of_month": subscription.delivery_day_of_month,
            "delivery_time_slot": subscription.delivery_time_slot,
            "delivery_address_id": subscription.delivery_address_id,
            "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
            "end_date": subscription.end_date.isoformat() if subscription.end_date else None,
            "auto_renew": subscription.auto_renew,
            "payment_method": subscription.payment_method,
            "auto_payment": subscription.auto_payment,
            "paused_at": subscription.paused_at.isoformat() if subscription.paused_at else None,
            "pause_reason": subscription.pause_reason,
            "resume_date": subscription.resume_date.isoformat() if subscription.resume_date else None,
            "total_orders_generated": subscription.total_orders_generated,
            "total_amount_billed": float(subscription.total_amount_billed),
            "failed_billing_attempts": subscription.failed_billing_attempts,
            "last_successful_billing": (
                subscription.last_successful_billing.isoformat() if subscription.last_successful_billing else None
            ),
            "discount_percentage": subscription.discount_percentage,
            "loyalty_points_multiplier": subscription.loyalty_points_multiplier,
            "items": items,
            "recent_orders": orders,
            "recent_logs": logs,
            "created_at": subscription.created_at.isoformat() if subscription.created_at else None,
            "updated_at": subscription.updated_at.isoformat() if subscription.updated_at else None,
        }

        return success_response(data={"subscription": subscription_data})

    except Exception as e:
        current_app.logger.error(f"Get subscription error: {e}")
        return internal_error_response("Failed to get subscription")


@admin_bp.route("/subscriptions/<int:subscription_id>/pause", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def pause_subscription_admin(subscription_id):
    """Pause a subscription (admin action)"""
    try:
        data = request.get_json() or {}
        pause_reason = data.get("pause_reason", "Paused by administrator")
        resume_date = data.get("resume_date")

        subscription_service = SubscriptionService()

        # Convert resume_date string to datetime if provided
        resume_dt = None
        if resume_date:
            try:
                resume_dt = datetime.fromisoformat(resume_date.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid resume_date format. Use ISO format.")

        # Pause subscription (service handles user_id validation, we pass None for admin override)
        paused_sub = subscription_service.pause_subscription(
            subscription_id=subscription_id,
            user_id=None,  # Admin can pause any subscription
            reason=pause_reason,
            resume_date=resume_dt,
        )

        current_app.logger.info(f"Subscription paused by admin: {subscription_id}")

        return success_response(
            data={"subscription_number": paused_sub.subscription_number, "status": paused_sub.status},
            message="Subscription paused successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Pause subscription error: {e}")
        return internal_error_response(f"Failed to pause subscription: {str(e)}")


@admin_bp.route("/subscriptions/<int:subscription_id>/resume", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def resume_subscription_admin(subscription_id):
    """Resume a paused subscription (admin action)"""
    try:
        subscription_service = SubscriptionService()

        # Resume subscription
        resumed_sub = subscription_service.resume_subscription(
            subscription_id=subscription_id, user_id=None  # Admin can resume any subscription
        )

        current_app.logger.info(f"Subscription resumed by admin: {subscription_id}")

        return success_response(
            data={"subscription_number": resumed_sub.subscription_number, "status": resumed_sub.status},
            message="Subscription resumed successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Resume subscription error: {e}")
        return internal_error_response(f"Failed to resume subscription: {str(e)}")


@admin_bp.route("/subscriptions/<int:subscription_id>/cancel", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def cancel_subscription_admin(subscription_id):
    """Cancel a subscription (admin action)"""
    try:
        data = request.get_json() or {}
        cancellation_reason = data.get("cancellation_reason", "Cancelled by administrator")

        subscription_service = SubscriptionService()

        # Cancel subscription
        cancelled_sub = subscription_service.cancel_subscription(
            subscription_id=subscription_id,
            user_id=None,  # Admin can cancel any subscription
            reason=cancellation_reason,
        )

        current_app.logger.info(f"Subscription cancelled by admin: {subscription_id}")

        return success_response(
            data={"subscription_number": cancelled_sub.subscription_number, "status": cancelled_sub.status},
            message="Subscription cancelled successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Cancel subscription error: {e}")
        return internal_error_response(f"Failed to cancel subscription: {str(e)}")


@admin_bp.route("/subscriptions/<int:subscription_id>/billing/process", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def process_subscription_billing_admin(subscription_id):
    """Manually trigger billing for a subscription"""
    try:
        subscription_service = SubscriptionService()

        # Process billing
        result = subscription_service.process_subscription_billing(subscription_id)

        current_app.logger.info(f"Subscription billing processed by admin: {subscription_id}")

        return success_response(data=result, message="Billing processed successfully")

    except Exception as e:
        current_app.logger.error(f"Process subscription billing error: {e}")
        return internal_error_response(f"Failed to process billing: {str(e)}")


@admin_bp.route("/subscriptions/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders"])
def get_subscription_analytics():
    """Get subscription analytics"""
    try:
        # Parse date range
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        start_date = None
        end_date = None

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid start_date format")

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid end_date format")

        subscription_service = SubscriptionService()
        analytics = subscription_service.get_subscription_analytics(start_date=start_date, end_date=end_date)

        return success_response(data={"analytics": analytics})

    except Exception as e:
        current_app.logger.error(f"Get subscription analytics error: {e}")
        return internal_error_response("Failed to get subscription analytics")


# ============================================================================
# PAYMENT MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/payments", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_payments():
    """Get all payments with filtering and search"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        status = request.args.get("status")  # pending, completed, failed, refunded
        payment_method = request.args.get("payment_method")
        user_id = request.args.get("user_id", type=int)
        order_id = request.args.get("order_id", type=int)
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        sort_by = request.args.get("sort_by", "created_at")  # created_at, amount

        # Build query
        query = Payment.query

        # Apply filters
        if status:
            query = query.filter_by(status=status)

        if payment_method:
            query = query.filter_by(payment_method=payment_method)

        if user_id:
            query = query.filter_by(user_id=user_id)

        if order_id:
            query = query.filter_by(order_id=order_id)

        # Date range filter
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                query = query.filter(Payment.created_at >= start_dt)
            except:  # noqa: E722
                return validation_error_response("Invalid start_date format")

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                query = query.filter(Payment.created_at <= end_dt)
            except:  # noqa: E722
                return validation_error_response("Invalid end_date format")

        # Search
        if search:
            search_term = f"%{search}%"
            query = query.join(User).filter(
                or_(
                    Payment.payment_id.ilike(search_term),
                    Payment.provider_transaction_id.ilike(search_term),
                    User.first_name.ilike(search_term),
                    User.last_name.ilike(search_term),
                    User.email.ilike(search_term),
                )
            )

        # Apply sorting
        if sort_by == "amount":
            query = query.order_by(Payment.amount.desc())
        else:  # Default: created_at
            query = query.order_by(Payment.created_at.desc())

        # Eager-load user + order to avoid N+1 on per-row serialization (ARCH-009).
        query = get_payments_optimized(query)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize payments
        payments_data = []
        for payment in pagination.items:
            payment_dict = {
                "id": payment.id,
                "payment_id": payment.payment_id,
                "user_id": payment.user_id,
                "user_name": (
                    f"{payment.user.first_name} {payment.user.last_name or ''}".strip() if payment.user else None
                ),
                "user_email": payment.user.email if payment.user else None,
                "order_id": payment.order_id,
                "order_number": payment.order.order_number if payment.order else None,
                "subscription_id": payment.subscription_id,
                "amount": float(payment.amount),
                "currency": payment.currency,
                "payment_method": payment.payment_method,
                "status": payment.status,
                "provider_transaction_id": payment.provider_transaction_id,
                "description": payment.description,
                "failure_reason": payment.failure_reason,
                "webhook_processed": payment.webhook_processed,
                "created_at": payment.created_at.isoformat() if payment.created_at else None,
            }
            payments_data.append(payment_dict)

        # Calculate summary statistics
        total_amount = (
            db.session.query(func.sum(Payment.amount)).filter(Payment.id.in_([p.id for p in pagination.items])).scalar()
            or 0
        )

        return paginated_response(
            items=payments_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={"total_amount": float(total_amount)},
        )

    except Exception as e:
        current_app.logger.error(f"Get payments error: {e}")
        return internal_error_response("Failed to get payments")


@admin_bp.route("/payments/<int:payment_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_payment(payment_id):
    """Get detailed payment information including transactions"""
    try:
        payment = Payment.query.get(payment_id)

        if not payment:
            return not_found_response("Payment not found")

        # Get payment transactions
        transactions = (
            PaymentTransaction.query.filter_by(payment_id=payment_id)
            .order_by(PaymentTransaction.created_at.desc())
            .all()
        )

        transaction_data = []
        for txn in transactions:
            transaction_data.append(
                {
                    "id": txn.id,
                    "transaction_type": txn.transaction_type,
                    "amount": float(txn.amount),
                    "currency": txn.currency,
                    "status": txn.status,
                    "provider_transaction_id": txn.provider_transaction_id,
                    "provider_reference": txn.provider_reference,
                    "success": txn.success,
                    "failure_reason": txn.failure_reason,
                    "ip_address": txn.ip_address,
                    "created_at": txn.created_at.isoformat() if txn.created_at else None,
                }
            )

        payment_data = {
            "id": payment.id,
            "payment_id": payment.payment_id,
            "user": (
                {
                    "id": payment.user.id,
                    "name": f"{payment.user.first_name} {payment.user.last_name or ''}".strip(),
                    "email": payment.user.email,
                    "phone": payment.user.phone,
                }
                if payment.user
                else None
            ),
            "order": (
                {
                    "id": payment.order.id,
                    "order_number": payment.order.order_number,
                    "status": payment.order.status,
                    "total_amount": float(payment.order.total_amount),
                }
                if payment.order
                else None
            ),
            "subscription_id": payment.subscription_id,
            "amount": float(payment.amount),
            "currency": payment.currency,
            "payment_method": payment.payment_method,
            "status": payment.status,
            "payment_provider": getattr(payment, "payment_provider", None),
            "provider_transaction_id": payment.provider_transaction_id,
            "provider_data": payment.provider_data,
            "payment_link": payment.payment_link,
            "payment_link_expires_at": (
                payment.payment_link_expires_at.isoformat() if payment.payment_link_expires_at else None
            ),
            "webhook_processed": payment.webhook_processed,
            "webhook_attempts": payment.webhook_attempts,
            "description": payment.description,
            "callback_url": payment.callback_url,
            "failure_reason": payment.failure_reason,
            "consume_marking_codes": bool(getattr(payment, "consume_marking_codes", False)),
            "fiscalization": payment.fiscalization.to_dict() if getattr(payment, "fiscalization", None) else None,
            "transactions": transaction_data,
            "created_at": payment.created_at.isoformat() if payment.created_at else None,
            "updated_at": payment.updated_at.isoformat() if payment.updated_at else None,
        }
        try:
            payment_data["fiscalization_diagnostic"] = PaymentFiscalizationService().diagnose_fiscalization_gap(payment)
        except Exception as diag_exc:  # noqa: BLE001
            current_app.logger.warning(
                "Failed to build fiscalization diagnostic for payment %s: %s",
                payment.id,
                diag_exc,
            )
            payment_data["fiscalization_diagnostic"] = {
                "payment_id": payment.id,
                "is_ready": False,
                "issues": [f"diagnostic_error:{diag_exc}"],
            }

        return success_response(data={"payment": payment_data})

    except Exception as e:
        current_app.logger.error(f"Get payment error: {e}")
        return internal_error_response("Failed to get payment")


@admin_bp.route("/payments/<int:payment_id>/fiscalization/retry", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def retry_payment_fiscalization(payment_id):
    """Retry Click fiscalization for a payment."""
    try:
        from business_app.utils.audit_logger import audit_logger

        actor_user_id = get_jwt_identity()
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="payment_fiscalization_retry_requested",
            severity=AuditSeverity.MEDIUM,
            resource_type="payment",
            resource_id=str(payment_id),
            description=f"Requested fiscalization retry for payment {payment_id}",
            additional_data={
                "payment_id": payment_id,
                "actor_user_id": actor_user_id,
            },
        )
        fiscalization = PaymentFiscalizationService().process_click_fiscalization(
            payment_id,
            force=True,
            actor_user_id=actor_user_id,
        )
        db.session.commit()
        return success_response(
            data={"fiscalization": fiscalization.to_dict()}, message="Payment fiscalization retried successfully"
        )
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(message=str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Retry payment fiscalization error: {e}")
        return internal_error_response("Failed to retry payment fiscalization")


@admin_bp.route("/payments/<int:payment_id>/refund", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def refund_payment(payment_id):
    """Process a payment refund"""
    try:
        data = request.get_json() or {}
        refund_amount = data.get("amount")
        reason = data.get("reason", "Refund requested by administrator")

        if not refund_amount:
            return validation_error_response("Refund amount is required")

        payment = Payment.query.get(payment_id)
        if not payment:
            return not_found_response("Payment not found")

        # Validate refund amount
        try:
            refund_amount = int(refund_amount)
            if refund_amount <= 0:
                return validation_error_response("Refund amount must be greater than 0")
            if refund_amount > payment.amount:
                return validation_error_response("Refund amount cannot exceed payment amount")
        except ValueError:
            return validation_error_response("Invalid refund amount")

        # Process refund using payment service
        payment_service = PaymentService()
        success = payment_service.process_refund(payment_id=payment_id, amount=refund_amount, reason=reason)

        if success:
            current_app.logger.info(f"Payment refunded by admin: {payment_id}, Amount: {refund_amount}")
            return success_response(
                data={"payment_id": payment.payment_id, "refund_amount": refund_amount},
                message="Refund processed successfully",
            )
        else:
            return internal_error_response("Refund processing failed")

    except Exception as e:
        current_app.logger.error(f"Refund payment error: {e}")
        return internal_error_response(f"Failed to process refund: {str(e)}")


@admin_bp.route("/payments/<int:payment_id>/status", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders"])
def get_payment_status(payment_id):
    """Get current payment status from provider"""
    try:
        payment_service = PaymentService()
        status_data = payment_service.get_payment_status(payment_id)

        return success_response(data={"status": status_data})

    except Exception as e:
        current_app.logger.error(f"Get payment status error: {e}")
        return internal_error_response(f"Failed to get payment status: {str(e)}")


@admin_bp.route("/payments/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders"])
def get_payment_analytics():
    """Get payment analytics and statistics"""
    try:
        # Parse date range
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        # Default to last 30 days if no dates provided
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=30)

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid start_date format")

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid end_date format")

        # Query payments in date range
        payments_query = Payment.query.filter(Payment.created_at.between(start_date, end_date))

        # Total payments
        total_payments = payments_query.count()

        # Payments by status
        status_counts = (
            db.session.query(
                Payment.status, func.count(Payment.id).label("count"), func.sum(Payment.amount).label("total_amount")
            )
            .filter(Payment.created_at.between(start_date, end_date))
            .group_by(Payment.status)
            .all()
        )

        status_breakdown = {}
        for status, count, total in status_counts:
            status_breakdown[status] = {"count": count, "total_amount": float(total) if total else 0}

        # Payments by method
        method_counts = (
            db.session.query(
                Payment.payment_method,
                func.count(Payment.id).label("count"),
                func.sum(Payment.amount).label("total_amount"),
            )
            .filter(Payment.created_at.between(start_date, end_date))
            .group_by(Payment.payment_method)
            .all()
        )

        method_breakdown = {}
        for method, count, total in method_counts:
            method_breakdown[method] = {"count": count, "total_amount": float(total) if total else 0}

        # Total revenue
        total_revenue = (
            db.session.query(func.sum(Payment.amount))
            .filter(Payment.status == PaymentStatus.COMPLETED)
            .filter(Payment.created_at.between(start_date, end_date))
            .scalar()
            or 0
        )

        # Refunded amount
        refunded_amount = (
            db.session.query(func.sum(PaymentTransaction.amount))
            .join(Payment)
            .filter(PaymentTransaction.transaction_type == "refund")
            .filter(PaymentTransaction.success == True)  # noqa: E712
            .filter(Payment.created_at.between(start_date, end_date))
            .scalar()
            or 0
        )

        # Failed payments
        failed_payments = payments_query.filter(Payment.status == PaymentStatus.FAILED).count()

        # Success rate
        completed_payments = payments_query.filter(Payment.status == PaymentStatus.COMPLETED).count()
        success_rate = (completed_payments / total_payments * 100) if total_payments > 0 else 0

        analytics = {
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "totals": {
                "total_payments": total_payments,
                "completed_payments": completed_payments,
                "failed_payments": failed_payments,
                "total_revenue": float(total_revenue),
                "refunded_amount": float(refunded_amount),
                "net_revenue": float(total_revenue - refunded_amount),
                "success_rate": round(success_rate, 2),
            },
            "by_status": status_breakdown,
            "by_method": method_breakdown,
        }

        return success_response(data={"analytics": analytics})

    except Exception as e:
        current_app.logger.error(f"Get payment analytics error: {e}")
        return internal_error_response("Failed to get payment analytics")


@admin_bp.route("/payments/transactions", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders"])
def get_payment_transactions():
    """Get all payment transactions with filtering"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        transaction_type = request.args.get("transaction_type")  # charge, refund, capture, cancel
        status = request.args.get("status")  # success, failed, pending
        payment_id = request.args.get("payment_id", type=int)

        # Build query
        query = PaymentTransaction.query

        # Apply filters
        if transaction_type:
            query = query.filter_by(transaction_type=transaction_type)

        if status:
            query = query.filter_by(status=status)

        if payment_id:
            query = query.filter_by(payment_id=payment_id)

        # Order by most recent
        query = query.order_by(PaymentTransaction.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize transactions
        transactions_data = []
        for txn in pagination.items:
            txn_dict = {
                "id": txn.id,
                "payment_id": txn.payment_id,
                "transaction_type": txn.transaction_type,
                "amount": float(txn.amount),
                "currency": txn.currency,
                "status": txn.status,
                "provider_transaction_id": txn.provider_transaction_id,
                "provider_reference": txn.provider_reference,
                "success": txn.success,
                "failure_reason": txn.failure_reason,
                "ip_address": txn.ip_address,
                "created_at": txn.created_at.isoformat() if txn.created_at else None,
            }
            transactions_data.append(txn_dict)

        return paginated_response(items=transactions_data, page=page, per_page=per_page, total=pagination.total)

    except Exception as e:
        current_app.logger.error(f"Get payment transactions error: {e}")
        return internal_error_response("Failed to get payment transactions")


# ============================================================================
# REVIEW MODERATION ENDPOINTS
# ============================================================================


@admin_bp.route("/reviews", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_reviews():
    """Get all reviews with filtering for moderation"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)
        search = request.args.get("search", "").strip()
        is_approved = request.args.get("is_approved", type=bool)
        is_featured = request.args.get("is_featured", type=bool)
        rating = request.args.get("rating", type=int)
        product_id = request.args.get("product_id", type=int)
        user_id = request.args.get("user_id", type=int)
        pending_only = request.args.get("pending_only", "false").lower() == "true"
        sort_by = request.args.get("sort_by", "created_at")  # created_at, rating, helpful_count

        # Build query
        query = Review.query

        # Apply filters
        if pending_only:
            query = query.filter_by(is_approved=False)
        elif is_approved is not None:
            query = query.filter_by(is_approved=is_approved)

        if is_featured is not None:
            query = query.filter_by(is_featured=is_featured)

        if rating:
            query = query.filter_by(rating=rating)

        if product_id:
            query = query.filter_by(product_id=product_id)

        if user_id:
            query = query.filter_by(user_id=user_id)

        # Search
        if search:
            search_term = f"%{search}%"
            query = (
                query.join(User)
                .join(Product)
                .filter(
                    or_(
                        Review.title.ilike(search_term),
                        Review.comment.ilike(search_term),
                        User.first_name.ilike(search_term),
                        User.last_name.ilike(search_term),
                        Product.name.ilike(search_term),
                    )
                )
            )

        # Apply sorting
        if sort_by == "rating":
            query = query.order_by(Review.rating.desc())
        elif sort_by == "helpful_count":
            query = query.order_by(Review.helpful_count.desc())
        else:  # Default: created_at
            query = query.order_by(Review.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize reviews
        reviews_data = []
        for review in pagination.items:
            review_dict = {
                "id": review.id,
                "user_id": review.user_id,
                "user_name": f"{review.user.first_name} {review.user.last_name or ''}".strip() if review.user else None,
                "product_id": review.product_id,
                "product_name": review.product.name if review.product else None,
                "order_id": review.order_id,
                "rating": review.rating,
                "title": review.title,
                "comment": review.comment,
                "is_approved": review.is_approved,
                "is_featured": review.is_featured,
                "moderator_notes": review.moderator_notes,
                "helpful_count": review.helpful_count,
                "photos": review.photos,
                "created_at": review.created_at.isoformat() if review.created_at else None,
            }
            reviews_data.append(review_dict)

        # Statistics
        total_pending = Review.query.filter_by(is_approved=False).count()
        total_featured = Review.query.filter_by(is_featured=True).count()

        return paginated_response(
            items=reviews_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={"total_pending": total_pending, "total_featured": total_featured},
        )

    except Exception as e:
        current_app.logger.error(f"Get reviews error: {e}")
        return internal_error_response("Failed to get reviews")


@admin_bp.route("/reviews/<int:review_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_review(review_id):
    """Get detailed review information"""
    try:
        review = Review.query.get(review_id)

        if not review:
            return not_found_response("Review not found")

        # Get product info
        product = review.product
        product_info = None
        if product:
            # Get product's average rating
            avg_rating = (
                db.session.query(func.avg(Review.rating))
                .filter(Review.product_id == product.id, Review.is_approved == True)  # noqa: E712
                .scalar()
                or 0
            )

            product_info = {
                "id": product.id,
                "name": product.name,
                "average_rating": round(float(avg_rating), 2),
                "total_reviews": Review.query.filter_by(product_id=product.id, is_approved=True).count(),
            }

        # Get user info
        user = review.user
        user_info = None
        if user:
            user_reviews_count = Review.query.filter_by(user_id=user.id).count()
            user_approved_reviews = Review.query.filter_by(user_id=user.id, is_approved=True).count()

            user_info = {
                "id": user.id,
                "name": f"{user.first_name} {user.last_name or ''}".strip(),
                "email": user.email,
                "phone": user.phone,
                "total_reviews": user_reviews_count,
                "approved_reviews": user_approved_reviews,
            }

        review_data = {
            "id": review.id,
            "user": user_info,
            "product": product_info,
            "order_id": review.order_id,
            "rating": review.rating,
            "title": review.title,
            "comment": review.comment,
            "is_approved": review.is_approved,
            "is_featured": review.is_featured,
            "moderator_notes": review.moderator_notes,
            "helpful_count": review.helpful_count,
            "photos": review.photos,
            "created_at": review.created_at.isoformat() if review.created_at else None,
            "updated_at": review.updated_at.isoformat() if review.updated_at else None,
        }

        return success_response(data={"review": review_data})

    except Exception as e:
        current_app.logger.error(f"Get review error: {e}")
        return internal_error_response("Failed to get review")


@admin_bp.route("/reviews/<int:review_id>/approve", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def approve_review(review_id):
    """Approve a review"""
    try:
        data = request.get_json() or {}
        moderator_notes = data.get("moderator_notes")

        review_service = ReviewService()

        # Use the moderate_review method
        updated_review = review_service.moderate_review(
            review_id=review_id, is_approved=True, moderator_notes=moderator_notes, admin_user_id=get_jwt_identity()
        )

        current_app.logger.info(f"Review approved by admin: {review_id}")

        return success_response(
            data={"review_id": updated_review.id, "is_approved": updated_review.is_approved},
            message="Review approved successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Approve review error: {e}")
        return internal_error_response(f"Failed to approve review: {str(e)}")


@admin_bp.route("/reviews/<int:review_id>/reject", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def reject_review(review_id):
    """Reject a review"""
    try:
        data = request.get_json() or {}
        moderator_notes = data.get("moderator_notes", "Rejected by administrator")

        review_service = ReviewService()

        # Use the moderate_review method
        updated_review = review_service.moderate_review(
            review_id=review_id, is_approved=False, moderator_notes=moderator_notes, admin_user_id=get_jwt_identity()
        )

        current_app.logger.info(f"Review rejected by admin: {review_id}")

        return success_response(
            data={"review_id": updated_review.id, "is_approved": updated_review.is_approved},
            message="Review rejected successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Reject review error: {e}")
        return internal_error_response(f"Failed to reject review: {str(e)}")


@admin_bp.route("/reviews/<int:review_id>/feature", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def feature_review(review_id):
    """Feature or unfeature a review"""
    try:
        data = request.get_json() or {}
        is_featured = data.get("is_featured", True)

        review = Review.query.get(review_id)
        if not review:
            return not_found_response("Review not found")

        # Only approved reviews can be featured
        if is_featured and not review.is_approved:
            return validation_error_response("Only approved reviews can be featured")

        review.is_featured = is_featured
        db.session.commit()

        current_app.logger.info(f"Review {'featured' if is_featured else 'unfeatured'} by admin: {review_id}")

        return success_response(
            data={"review_id": review.id, "is_featured": review.is_featured},
            message=f"Review {'featured' if is_featured else 'unfeatured'} successfully",
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Feature review error: {e}")
        return internal_error_response("Failed to feature review")


@admin_bp.route("/reviews/<int:review_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_products"])
def delete_review(review_id):
    """Delete a review"""
    try:
        review_service = ReviewService()
        get_jwt_identity()

        # The service expects user_id for ownership check, but admin can delete any review
        # We'll need to pass the review's user_id or modify the service
        review = Review.query.get(review_id)
        if not review:
            return not_found_response("Review not found")

        success = review_service.delete_review(review_id=review_id, user_id=review.user_id, is_admin=True)

        if success:
            current_app.logger.info(f"Review deleted by admin: {review_id}")
            return success_response(message=get_translation("api.admin.success.review_deleted"))
        else:
            return internal_error_response("Failed to delete review")

    except Exception as e:
        current_app.logger.error(f"Delete review error: {e}")
        return internal_error_response(f"Failed to delete review: {str(e)}")


@admin_bp.route("/reviews/bulk-approve", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
def bulk_approve_reviews():
    """Bulk approve multiple reviews"""
    try:
        data = request.get_json()
        review_ids = data.get("review_ids", [])
        moderator_notes = data.get("moderator_notes")

        if not review_ids:
            return validation_error_response("review_ids is required")

        review_service = ReviewService()
        approved_count = 0
        failed_count = 0

        for review_id in review_ids:
            try:
                review_service.moderate_review(
                    review_id=review_id,
                    is_approved=True,
                    moderator_notes=moderator_notes,
                    admin_user_id=get_jwt_identity(),
                )
                approved_count += 1
            except Exception as e:
                current_app.logger.error(f"Failed to approve review {review_id}: {e}")
                failed_count += 1

        current_app.logger.info(f"Bulk approval completed: {approved_count} approved, {failed_count} failed")

        return success_response(
            data={"approved": approved_count, "failed": failed_count},
            message=f"Bulk approval completed: {approved_count} approved, {failed_count} failed",
        )

    except Exception as e:
        current_app.logger.error(f"Bulk approve reviews error: {e}")
        return internal_error_response("Failed to bulk approve reviews")


@admin_bp.route("/reviews/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products"])
def get_review_analytics():
    """Get review analytics and statistics"""
    try:
        # Parse date range
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        # Default to last 30 days if no dates provided
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=30)

        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid start_date format")

        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except:  # noqa: E722
                return validation_error_response("Invalid end_date format")

        # Total reviews
        total_reviews = Review.query.filter(Review.created_at.between(start_date, end_date)).count()

        # Reviews by approval status
        approved_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date), Review.is_approved == True  # noqa: E712
        ).count()

        pending_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date), Review.is_approved == False  # noqa: E712
        ).count()

        featured_reviews = Review.query.filter(
            Review.created_at.between(start_date, end_date), Review.is_featured == True  # noqa: E712
        ).count()

        # Average rating
        avg_rating = (
            db.session.query(func.avg(Review.rating))
            .filter(Review.created_at.between(start_date, end_date))
            .filter(Review.is_approved == True)  # noqa: E712
            .scalar()
            or 0
        )

        # Reviews by rating
        rating_breakdown = (
            db.session.query(Review.rating, func.count(Review.id).label("count"))
            .filter(Review.created_at.between(start_date, end_date))
            .group_by(Review.rating)
            .all()
        )

        rating_counts = {rating: count for rating, count in rating_breakdown}

        # Top reviewed products
        top_products = (
            db.session.query(
                Product.id,
                Product.name,
                func.count(Review.id).label("review_count"),
                func.avg(Review.rating).label("avg_rating"),
            )
            .join(Review)
            .filter(Review.created_at.between(start_date, end_date))
            .filter(Review.is_approved == True)  # noqa: E712
            .group_by(Product.id, Product.name)
            .order_by(desc("review_count"))
            .limit(10)
            .all()
        )

        top_products_data = [
            {
                "product_id": p.id,
                "product_name": p.name,
                "review_count": p.review_count,
                "average_rating": round(float(p.avg_rating), 2) if p.avg_rating else 0,
            }
            for p in top_products
        ]

        analytics = {
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "totals": {
                "total_reviews": total_reviews,
                "approved_reviews": approved_reviews,
                "pending_reviews": pending_reviews,
                "featured_reviews": featured_reviews,
                "average_rating": round(float(avg_rating), 2),
                "approval_rate": round((approved_reviews / total_reviews * 100), 2) if total_reviews > 0 else 0,
            },
            "rating_breakdown": rating_counts,
            "top_products": top_products_data,
        }

        return success_response(data={"analytics": analytics})

    except Exception as e:
        current_app.logger.error(f"Get review analytics error: {e}")
        return internal_error_response("Failed to get review analytics")


# ============================================================================
# CAMPAIGN MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/campaigns", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_campaigns", "manage_campaigns"])
def get_promotional_campaigns():
    """
    Get promotional campaigns with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - is_active: Filter by active status
        - campaign_type: Filter by type (discount, loyalty_bonus, free_delivery)
        - search: Search in name and description
        - valid_only: Show only currently valid campaigns
    """
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)

        # Build query
        query = PromotionalCampaign.query

        # Active filter
        is_active = request.args.get("is_active")
        if is_active is not None:
            is_active_bool = is_active.lower() == "true"
            query = query.filter_by(is_active=is_active_bool)

        # Campaign type filter
        campaign_type = request.args.get("campaign_type")
        if campaign_type:
            query = query.filter_by(campaign_type=campaign_type)

        # Search
        search = request.args.get("search")
        if search:
            query = query.filter(
                or_(
                    PromotionalCampaign.name.ilike(f"%{search}%"),
                    PromotionalCampaign.description.ilike(f"%{search}%"),
                    PromotionalCampaign.promo_code.ilike(f"%{search}%"),
                )
            )

        # Valid only filter
        valid_only = request.args.get("valid_only")
        if valid_only and valid_only.lower() == "true":
            now = datetime.now(UTC)
            query = query.filter(
                PromotionalCampaign.is_active == True, PromotionalCampaign.start_date <= now  # noqa: E712
            ).filter(  # noqa: E501,E712
                or_(PromotionalCampaign.end_date.is_(None), PromotionalCampaign.end_date >= now)
            )

        # Order by creation date (newest first)
        query = query.order_by(PromotionalCampaign.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize campaigns
        language = get_current_language()
        campaigns_data = []
        for campaign in pagination.items:
            campaign_data = campaign.to_dict(language=language)
            campaign_data.update(
                {
                    "total_uses": campaign.total_uses,
                    "total_discount_given": (
                        float(campaign.total_discount_given) if campaign.total_discount_given else 0
                    ),
                    "total_revenue_generated": (
                        float(campaign.total_revenue_generated) if campaign.total_revenue_generated else 0
                    ),
                }
            )
            campaigns_data.append(campaign_data)

        return paginated_response(items=campaigns_data, total=pagination.total, page=page, per_page=per_page)

    except Exception as e:
        current_app.logger.error(f"Get promotional campaigns error: {e}")
        return internal_error_response("Failed to get campaigns")


@admin_bp.route("/campaigns/<int:campaign_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_campaigns", "manage_campaigns"])
def get_campaign_detail(campaign_id):
    """Get detailed information about a specific campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response("Campaign not found")

        language = get_current_language()
        campaign_data = campaign.to_dict(language=language, include_all_translations=True)

        # Add usage statistics
        from business_app.models.analytics import CampaignUsage

        usage_stats = {
            "total_uses": campaign.total_uses,
            "total_discount_given": float(campaign.total_discount_given) if campaign.total_discount_given else 0,
            "total_revenue_generated": (
                float(campaign.total_revenue_generated) if campaign.total_revenue_generated else 0
            ),
            "unique_customers": CampaignUsage.query.filter_by(campaign_id=campaign_id)
            .distinct(CampaignUsage.user_id)
            .count(),
            "usage_limit": campaign.usage_limit,
            "usage_limit_per_customer": campaign.usage_limit_per_customer,
            "remaining_uses": campaign.usage_limit - campaign.total_uses if campaign.usage_limit else None,
        }

        campaign_data["usage_stats"] = usage_stats

        return success_response(data={"campaign": campaign_data})

    except Exception as e:
        current_app.logger.error(f"Get campaign detail error: {e}")
        return internal_error_response("Failed to get campaign detail")


@admin_bp.route("/campaigns", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_campaigns"])
@validate_json(["name", "campaign_type", "start_date"])
def create_campaign():
    """
    Create a new promotional campaign

    Request Body:
        - name: Campaign name
        - description: Campaign description
        - campaign_type: Type (discount, loyalty_bonus, free_delivery)
        - discount_type: Discount type (percentage, fixed, buy_x_get_y)
        - discount_value: Discount value
        - min_order_value: Minimum order value
        - max_discount_amount: Maximum discount amount
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format, optional)
        - usage_limit: Total usage limit (optional)
        - usage_limit_per_customer: Per customer limit (default: 1)
        - promo_code: Promo code (optional, will be generated if not provided)
        - target_all_customers: Target all customers
        - target_new_customers: Target new customers only
        - target_vip_customers: Target VIP customers only
        - target_segments: List of customer segment IDs
        - is_active: Active status (default: true)
        - translations: Multilingual content
    """
    try:
        data = request.get_json()

        # Validate campaign type
        valid_types = ["discount", "loyalty_bonus", "free_delivery"]
        campaign_type = data.get("campaign_type")
        if campaign_type not in valid_types:
            return validation_error_response(f'Invalid campaign_type. Must be one of: {", ".join(valid_types)}')

        # Validate discount fields for discount campaigns
        if campaign_type == "discount":
            if not data.get("discount_type") or not data.get("discount_value"):
                return validation_error_response("discount_type and discount_value required for discount campaigns")

        # Parse dates
        start_date = datetime.fromisoformat(data["start_date"].replace("Z", "+00:00"))
        end_date = None
        if data.get("end_date"):
            end_date = datetime.fromisoformat(data["end_date"].replace("Z", "+00:00"))

        # Generate promo code if not provided
        promo_code = data.get("promo_code")
        if not promo_code and campaign_type == "discount":
            import random
            import string

            promo_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

        # Check for duplicate promo code
        if promo_code:
            existing = PromotionalCampaign.query.filter_by(promo_code=promo_code).first()
            if existing:
                return validation_error_response(f'Promo code "{promo_code}" already exists')

        # Create campaign
        campaign = PromotionalCampaign(
            name=data.get("name"),
            description=data.get("description"),
            campaign_type=campaign_type,
            discount_type=data.get("discount_type"),
            discount_value=Decimal(str(data.get("discount_value"))) if data.get("discount_value") else None,
            min_order_value=Decimal(str(data.get("min_order_value"))) if data.get("min_order_value") else None,
            max_discount_amount=(
                Decimal(str(data.get("max_discount_amount"))) if data.get("max_discount_amount") else None
            ),
            start_date=start_date,
            end_date=end_date,
            usage_limit=data.get("usage_limit"),
            usage_limit_per_customer=data.get("usage_limit_per_customer", 1),
            promo_code=promo_code,
            target_all_customers=data.get("target_all_customers", False),
            target_new_customers=data.get("target_new_customers", False),
            target_vip_customers=data.get("target_vip_customers", False),
            target_segments=data.get("target_segments", []),
            is_active=data.get("is_active", True),
        )

        db.session.add(campaign)
        db.session.flush()

        # Handle translations
        if data.get("translations"):
            campaign.set_translations(data["translations"])

        db.session.commit()

        language = get_current_language()
        return created_response(
            data={"campaign": campaign.to_dict(language=language)}, message="Campaign created successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create campaign error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to create campaign")


@admin_bp.route("/campaigns/<int:campaign_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_campaigns"])
def update_campaign(campaign_id):
    """Update an existing campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response("Campaign not found")

        data = request.get_json()

        # Update basic fields
        if "name" in data:
            campaign.name = data["name"]
        if "description" in data:
            campaign.description = data["description"]
        if "discount_type" in data:
            campaign.discount_type = data["discount_type"]
        if "discount_value" in data:
            campaign.discount_value = Decimal(str(data["discount_value"]))
        if "min_order_value" in data:
            campaign.min_order_value = Decimal(str(data["min_order_value"]))
        if "max_discount_amount" in data:
            campaign.max_discount_amount = Decimal(str(data["max_discount_amount"]))
        if "usage_limit" in data:
            campaign.usage_limit = data["usage_limit"]
        if "usage_limit_per_customer" in data:
            campaign.usage_limit_per_customer = data["usage_limit_per_customer"]
        if "is_active" in data:
            campaign.is_active = data["is_active"]
        if "target_all_customers" in data:
            campaign.target_all_customers = data["target_all_customers"]
        if "target_new_customers" in data:
            campaign.target_new_customers = data["target_new_customers"]
        if "target_vip_customers" in data:
            campaign.target_vip_customers = data["target_vip_customers"]
        if "target_segments" in data:
            campaign.target_segments = data["target_segments"]

        # Update dates
        if "start_date" in data:
            campaign.start_date = datetime.fromisoformat(data["start_date"].replace("Z", "+00:00"))
        if "end_date" in data:
            campaign.end_date = (
                datetime.fromisoformat(data["end_date"].replace("Z", "+00:00")) if data["end_date"] else None
            )

        # Update promo code (check for duplicates)
        if "promo_code" in data and data["promo_code"] != campaign.promo_code:
            existing = PromotionalCampaign.query.filter_by(promo_code=data["promo_code"]).first()
            if existing:
                return validation_error_response(f'Promo code "{data["promo_code"]}" already exists')
            campaign.promo_code = data["promo_code"]

        # Handle translations
        if data.get("translations"):
            campaign.set_translations(data["translations"])

        db.session.commit()

        language = get_current_language()
        return success_response(
            data={"campaign": campaign.to_dict(language=language)}, message="Campaign updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update campaign error: {e}")
        return internal_error_response("Failed to update campaign")


@admin_bp.route("/campaigns/<int:campaign_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_campaigns"])
def delete_campaign(campaign_id):
    """Delete or deactivate a campaign"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response("Campaign not found")

        # Check if campaign has been used
        if campaign.total_uses > 0:
            # Don't delete, just deactivate
            campaign.is_active = False
            db.session.commit()
            return success_response(message=get_translation("api.admin.success.campaign_deactivated"))

        # Safe to delete
        db.session.delete(campaign)
        db.session.commit()

        return success_response(message=get_translation("api.admin.success.campaign_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete campaign error: {e}")
        return internal_error_response("Failed to delete campaign")


@admin_bp.route("/campaigns/<int:campaign_id>/toggle", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_campaigns"])
def toggle_campaign(campaign_id):
    """Toggle campaign active status"""
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response("Campaign not found")

        campaign.is_active = not campaign.is_active
        db.session.commit()

        status = "activated" if campaign.is_active else "deactivated"
        return success_response(data={"is_active": campaign.is_active}, message=f"Campaign {status} successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Toggle campaign error: {e}")
        return internal_error_response("Failed to toggle campaign")


@admin_bp.route("/campaigns/<int:campaign_id>/usage", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_campaigns", "view_reports"])
def get_campaign_usage(campaign_id):
    """
    Get campaign usage history

    Query Parameters:
        - page: Page number
        - per_page: Items per page
    """
    try:
        campaign = PromotionalCampaign.query.get(campaign_id)

        if not campaign:
            return not_found_response("Campaign not found")

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)

        from business_app.models.analytics import CampaignUsage

        # Get usage records
        usage_query = CampaignUsage.query.filter_by(campaign_id=campaign_id)
        pagination = usage_query.order_by(CampaignUsage.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        usage_data = []
        for usage in pagination.items:
            # Get user info
            user = User.query.get(usage.user_id)
            usage_item = {
                "id": usage.id,
                "user_id": usage.user_id,
                "user_name": user.name if user else None,
                "user_email": user.email if user else None,
                "order_id": usage.order_id,
                "created_at": usage.created_at.isoformat() if usage.created_at else None,
            }
            usage_data.append(usage_item)

        return paginated_response(items=usage_data, total=pagination.total, page=page, per_page=per_page)

    except Exception as e:
        current_app.logger.error(f"Get campaign usage error: {e}")
        return internal_error_response("Failed to get campaign usage")


# ============================================================================
# PRICE RULE MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/price-rules", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_price_rules():
    """
    Get all price rules with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - product_id: Filter by product ID
        - rule_type: Filter by rule type (bulk_discount, vip_discount, etc.)
        - is_active: Filter by active status
        - valid_only: Show only currently valid rules
        - search: Search in name and description
    """
    try:
        from business_app.models.product import PriceRule
        from business_app.utils.constants import PriceRuleType

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)

        # Build query
        query = PriceRule.query

        # Product filter
        product_id = request.args.get("product_id", type=int)
        if product_id:
            query = query.filter_by(product_id=product_id)

        # Rule type filter
        rule_type = request.args.get("rule_type")
        if rule_type:
            try:
                query = query.filter_by(rule_type=PriceRuleType(rule_type))
            except ValueError:
                return validation_error_response(f"Invalid rule_type: {rule_type}")

        # Active filter
        is_active = request.args.get("is_active")
        if is_active is not None:
            is_active_bool = is_active.lower() == "true"
            query = query.filter_by(is_active=is_active_bool)

        # Valid only filter
        valid_only = request.args.get("valid_only")
        if valid_only and valid_only.lower() == "true":
            now = datetime.now(UTC)
            query = (
                query.filter(PriceRule.is_active == True)  # noqa: E712
                .filter(or_(PriceRule.valid_from.is_(None), PriceRule.valid_from <= now))
                .filter(or_(PriceRule.valid_until.is_(None), PriceRule.valid_until >= now))
            )

        # Search
        search = request.args.get("search")
        if search:
            query = query.filter(or_(PriceRule.name.ilike(f"%{search}%"), PriceRule.description.ilike(f"%{search}%")))

        # Sort by product and creation date
        query = query.order_by(PriceRule.product_id.asc(), PriceRule.created_at.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize price rules
        language = get_current_language()
        rules_data = []
        for rule in pagination.items:
            rule_dict = (
                rule.to_dict(language=language)
                if hasattr(rule, "to_dict")
                else {
                    "id": rule.id,
                    "product_id": rule.product_id,
                    "rule_type": rule.rule_type.value if rule.rule_type else None,
                    "name": rule.name,
                    "description": rule.description,
                    "min_quantity": rule.min_quantity,
                    "max_quantity": rule.max_quantity,
                    "min_order_value": float(rule.min_order_value) if rule.min_order_value else None,
                    "customer_type": rule.customer_type,
                    "discount_type": rule.discount_type,
                    "discount_value": float(rule.discount_value) if rule.discount_value else None,
                    "is_active": rule.is_active,
                    "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
                    "valid_until": rule.valid_until.isoformat() if rule.valid_until else None,
                    "created_at": rule.created_at.isoformat() if rule.created_at else None,
                }
            )

            # Add product info
            product = Product.query.get(rule.product_id)
            if product:
                rule_dict["product"] = {
                    "id": product.id,
                    "name": product.name,
                    "price": float(product.price) if product.price else None,
                }

            rules_data.append(rule_dict)

        return paginated_response(items=rules_data, total=pagination.total, page=page, per_page=per_page)

    except Exception as e:
        current_app.logger.error(f"Get price rules error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to get price rules")


@admin_bp.route("/price-rules/<int:rule_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_price_rule_detail(rule_id):
    """Get detailed information about a specific price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response("Price rule not found")

        language = get_current_language()
        rule_data = (
            rule.to_dict(language=language, include_all_translations=True)
            if hasattr(rule, "to_dict")
            else {
                "id": rule.id,
                "product_id": rule.product_id,
                "rule_type": rule.rule_type.value if rule.rule_type else None,
                "name": rule.name,
                "description": rule.description,
                "min_quantity": rule.min_quantity,
                "max_quantity": rule.max_quantity,
                "min_order_value": float(rule.min_order_value) if rule.min_order_value else None,
                "customer_type": rule.customer_type,
                "discount_type": rule.discount_type,
                "discount_value": float(rule.discount_value) if rule.discount_value else None,
                "is_active": rule.is_active,
                "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
                "valid_until": rule.valid_until.isoformat() if rule.valid_until else None,
                "created_at": rule.created_at.isoformat() if rule.created_at else None,
            }
        )

        # Add product info
        product = Product.query.get(rule.product_id)
        if product:
            rule_data["product"] = {
                "id": product.id,
                "name": product.name,
                "price": float(product.price) if product.price else None,
                "is_active": product.is_active,
            }

        return success_response(data={"price_rule": rule_data})

    except Exception as e:
        current_app.logger.error(f"Get price rule detail error: {e}")
        return internal_error_response("Failed to get price rule detail")


@admin_bp.route("/price-rules", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
@validate_json(["product_id", "rule_type", "name", "discount_value"])
def create_price_rule():
    """
    Create a new price rule

    Request Body:
        - product_id: Product ID
        - rule_type: Rule type (bulk_discount, vip_discount, loyalty_discount, seasonal_discount, time_based)
        - name: Rule name
        - description: Rule description
        - min_quantity: Minimum quantity (default: 1)
        - max_quantity: Maximum quantity (optional)
        - min_order_value: Minimum order value (optional)
        - customer_type: Customer type filter (vip, regular, etc.)
        - discount_type: Discount type (percentage or fixed)
        - discount_value: Discount value
        - is_active: Active status (default: true)
        - valid_from: Valid from date (ISO format, optional)
        - valid_until: Valid until date (ISO format, optional)
        - translations: Multilingual content
    """
    try:
        from business_app.models.product import PriceRule
        from business_app.utils.constants import PriceRuleType

        data = request.get_json()

        # Validate product exists
        product = Product.query.get(data.get("product_id"))
        if not product:
            return not_found_response("Product not found")

        # Validate rule type
        valid_rule_types = [t.value for t in PriceRuleType]
        rule_type_str = data.get("rule_type")
        if rule_type_str not in valid_rule_types:
            return validation_error_response(f'Invalid rule_type. Must be one of: {", ".join(valid_rule_types)}')

        rule_type = PriceRuleType(rule_type_str)

        # Validate discount type
        discount_type = data.get("discount_type", "percentage")
        if discount_type not in ["percentage", "fixed"]:
            return validation_error_response('discount_type must be "percentage" or "fixed"')

        # Create price rule
        rule = PriceRule(
            product_id=data.get("product_id"),
            rule_type=rule_type,
            name=data.get("name"),
            description=data.get("description"),
            min_quantity=data.get("min_quantity", 1),
            max_quantity=data.get("max_quantity"),
            min_order_value=Decimal(str(data.get("min_order_value"))) if data.get("min_order_value") else None,
            customer_type=data.get("customer_type"),
            discount_type=discount_type,
            discount_value=Decimal(str(data.get("discount_value"))),
            is_active=data.get("is_active", True),
        )

        # Set validity dates
        if data.get("valid_from"):
            rule.valid_from = datetime.fromisoformat(data["valid_from"].replace("Z", "+00:00"))
        if data.get("valid_until"):
            rule.valid_until = datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00"))

        db.session.add(rule)
        db.session.flush()

        # Handle translations if supported
        if hasattr(rule, "set_translations") and data.get("translations"):
            rule.set_translations(data["translations"])

        db.session.commit()

        get_current_language()
        rule_data = {
            "id": rule.id,
            "product_id": rule.product_id,
            "rule_type": rule.rule_type.value,
            "name": rule.name,
            "description": rule.description,
            "discount_value": float(rule.discount_value),
            "is_active": rule.is_active,
        }

        return created_response(data={"price_rule": rule_data}, message="Price rule created successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create price rule error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to create price rule")


@admin_bp.route("/price-rules/<int:rule_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_products"])
def update_price_rule(rule_id):
    """Update an existing price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response("Price rule not found")

        data = request.get_json()

        # Update basic fields
        if "name" in data:
            rule.name = data["name"]
        if "description" in data:
            rule.description = data["description"]
        if "min_quantity" in data:
            rule.min_quantity = data["min_quantity"]
        if "max_quantity" in data:
            rule.max_quantity = data["max_quantity"]
        if "min_order_value" in data:
            rule.min_order_value = Decimal(str(data["min_order_value"])) if data["min_order_value"] else None
        if "customer_type" in data:
            rule.customer_type = data["customer_type"]
        if "discount_type" in data:
            if data["discount_type"] not in ["percentage", "fixed"]:
                return validation_error_response('discount_type must be "percentage" or "fixed"')
            rule.discount_type = data["discount_type"]
        if "discount_value" in data:
            rule.discount_value = Decimal(str(data["discount_value"]))
        if "is_active" in data:
            rule.is_active = data["is_active"]

        # Update validity dates
        if "valid_from" in data:
            rule.valid_from = (
                datetime.fromisoformat(data["valid_from"].replace("Z", "+00:00")) if data["valid_from"] else None
            )
        if "valid_until" in data:
            rule.valid_until = (
                datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00")) if data["valid_until"] else None
            )

        # Handle translations if supported
        if hasattr(rule, "set_translations") and data.get("translations"):
            rule.set_translations(data["translations"])

        db.session.commit()

        get_current_language()
        rule_data = {
            "id": rule.id,
            "product_id": rule.product_id,
            "name": rule.name,
            "discount_value": float(rule.discount_value),
            "is_active": rule.is_active,
        }

        return success_response(data={"price_rule": rule_data}, message="Price rule updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update price rule error: {e}")
        return internal_error_response("Failed to update price rule")


@admin_bp.route("/price-rules/<int:rule_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_products"])
def delete_price_rule(rule_id):
    """Delete a price rule"""
    try:
        from business_app.models.product import PriceRule

        rule = PriceRule.query.get(rule_id)

        if not rule:
            return not_found_response("Price rule not found")

        db.session.delete(rule)
        db.session.commit()

        return success_response(message=get_translation("api.admin.success.price_rule_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete price rule error: {e}")
        return internal_error_response("Failed to delete price rule")


@admin_bp.route("/price-rules/types", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
def get_price_rule_types():
    """Get list of available price rule types"""
    try:
        from business_app.utils.constants import PriceRuleType

        rule_types = [
            {
                "value": rule_type.value,
                "label": rule_type.value.replace("_", " ").title(),
                "description": _get_rule_type_description(rule_type.value),
            }
            for rule_type in PriceRuleType
        ]

        return success_response(data={"rule_types": rule_types})

    except Exception as e:
        current_app.logger.error(f"Get price rule types error: {e}")
        return internal_error_response("Failed to get price rule types")


def _get_rule_type_description(rule_type):
    """Get human-readable description for rule type"""
    descriptions = {
        "bulk_discount": "Discount based on quantity purchased",
        "vip_discount": "Special discount for VIP customers",
        "loyalty_discount": "Discount based on loyalty tier",
        "seasonal_discount": "Time-limited seasonal promotion",
        "time_based": "Discount active during specific time periods",
    }
    return descriptions.get(rule_type, "")


@admin_bp.route("/reports/generate", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=1800, per="user")  # 5 reports per 30 minutes per user
@validate_admin_action(["view_reports", "generate_reports"])
@validate_json(["report_type"])
def generate_report():
    """
    Generate administrative report with various formats

    Request Body:
        - report_type: Type of report (sales_summary, customer_report, etc.)
        - date_range: {start_date: ISO, end_date: ISO}
        - filters: Additional filters specific to report type
        - format: Output format (json, csv, excel) - default: json
        - include_charts: Include chart data (default: true)
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        report_type = data.get("report_type")
        date_range = data.get("date_range", {})
        filters = data.get("filters", {})
        format_type = data.get("format", "json")
        data.get("include_charts", True)

        # Validate report type
        try:
            AdminReportService.validate_report_type(report_type)
        except ValidationError as err:
            return validation_error_response(err.message)

        # Parse date range
        start_date = date_range.get("start_date")
        end_date = date_range.get("end_date")

        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        else:
            start_dt = datetime.now(UTC) - timedelta(days=30)

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        else:
            end_dt = datetime.now(UTC)

        # Generate report via service layer
        report_data = AdminReportService.generate(report_type, start_dt, end_dt, filters)

        # Add metadata
        report_data["metadata"] = {
            "report_type": report_type,
            "generated_at": datetime.now(UTC).isoformat(),
            "generated_by": current_user_id,
            "date_range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "filters": filters,
            "format": format_type,
        }

        # Format output
        return AdminReportService.format_report(report_data, report_type, format_type)

    except Exception as e:
        current_app.logger.error(f"Generate report error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to generate report")


@admin_bp.route("/bulk-actions", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=10, window_seconds=600, per="user")  # 10 bulk actions per 10 minutes per user
@validate_admin_action(["manage_users", "manage_orders", "manage_products"])
@validate_json(["action", "target_type", "target_ids"])
def perform_bulk_action():
    """
    Perform bulk actions on multiple entities

    Request Body:
        - action: Action to perform
        - target_type: Type of entities (user, order, product, review, subscription)
        - target_ids: List of entity IDs (max 1000)
        - parameters: Additional parameters for the action
        - reason: Reason for the action (required for some actions)
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        action = data.get("action")
        target_type = data.get("target_type")
        target_ids = data.get("target_ids")
        parameters = data.get("parameters", {})
        reason = data.get("reason", f"Bulk action by admin {current_user_id}")

        # Validate inputs
        if not isinstance(target_ids, list) or len(target_ids) == 0:
            return validation_error_response("target_ids must be a non-empty list")

        if len(target_ids) > 1000:
            return validation_error_response("Maximum 1000 items allowed per bulk action")

        valid_actions = AdminBulkActionService.get_valid_actions()

        if target_type not in valid_actions:
            return validation_error_response(f'Invalid target_type. Valid types: {", ".join(valid_actions.keys())}')

        if action not in valid_actions[target_type]:
            return validation_error_response(
                f'Invalid action "{action}" for {target_type}. Valid actions: {", ".join(valid_actions[target_type])}'
            )

        result = AdminBulkActionService.perform(
            action=action,
            target_type=target_type,
            target_ids=target_ids,
            parameters=parameters,
            reason=reason,
            admin_id=current_user_id,
        )

        # Log bulk action
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.BULK_OPERATION,
            action=f"bulk_{action}_{target_type}",
            severity=AuditSeverity.HIGH,
            resource_type=target_type,
            description=f'Bulk {action} on {result["success_count"]} {target_type}(s)',
            success=True,
            additional_data={
                "action": action,
                "target_type": target_type,
                "total_items": len(target_ids),
                "success_count": result["success_count"],
                "failed_count": result["failed_count"],
                "reason": reason,
            },
        )

        return success_response(
            data={"results": result},
            message=f'Bulk action completed: {result["success_count"]} succeeded, {result["failed_count"]} failed',
        )

    except Exception as e:
        current_app.logger.error(f"Perform bulk action error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to perform bulk action")


# ============================================================================
# LOYALTY REWARD MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/loyalty/members", methods=["GET"])
@admin_bp.route("/loyalty-customers", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_members():
    """Get loyalty members with pagination, filtering, and summary stats."""
    try:
        result = AdminLoyaltyService.list_members(
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
            search=request.args.get("search", "", type=str),
            program_id=request.args.get("program_id", type=int),
            tier=request.args.get("tier", type=str),
        )
        return paginated_response(
            items=result["items"],
            total=result["total"],
            page=result["page"],
            per_page=result["per_page"],
            additional_meta={"summary": result["summary"]},
        )
    except Exception as e:
        current_app.logger.error(f"Get loyalty members error: {e}")
        return internal_error_response("Failed to get loyalty members")


@admin_bp.route("/loyalty/members/<int:user_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_member_detail(user_id):
    """Get detailed loyalty member data."""
    try:
        return success_response(data=AdminLoyaltyService.get_member_detail(user_id))
    except NotFoundError as exc:
        return not_found_response(str(exc))
    except Exception as e:
        current_app.logger.error(f"Get loyalty member detail error: {e}")
        return internal_error_response("Failed to get loyalty member detail")


@admin_bp.route("/loyalty/members/<int:user_id>/transactions", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_member_transactions(user_id):
    """Get a loyalty member's full transaction ledger, paginated (newest first)."""
    try:
        result = AdminLoyaltyService.get_member_transactions(
            user_id,
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
        )
        return paginated_response(
            items=result["items"],
            total=result["total"],
            page=result["page"],
            per_page=result["per_page"],
        )
    except NotFoundError as exc:
        return not_found_response(str(exc))
    except Exception as e:
        current_app.logger.error(f"Get loyalty member transactions error: {e}")
        return internal_error_response("Failed to get loyalty member transactions")


@admin_bp.route("/loyalty/rewards", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_rewards():
    """
    Get all loyalty rewards with filtering and pagination

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - program_id: Filter by loyalty program
        - reward_type: Filter by reward type (discount, free_product)
        - is_active: Filter by active status
        - is_featured: Filter by featured status
        - search: Search in name and description
    """
    try:
        result = AdminLoyaltyService.list_rewards(
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
            program_id=request.args.get("program_id", type=int),
            reward_type=request.args.get("reward_type"),
            is_active=request.args.get("is_active"),
            search=request.args.get("search", "", type=str),
            language=get_current_language(),
        )
        return paginated_response(
            items=result["items"], total=result["total"], page=result["page"], per_page=result["per_page"]
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty rewards error: {e}")
        return internal_error_response("Failed to get loyalty rewards")


@admin_bp.route("/loyalty/rewards/<int:reward_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_reward_detail(reward_id):
    """Get detailed information about a specific loyalty reward"""
    try:
        reward_data = AdminLoyaltyService.get_reward_detail(
            reward_id,
            language=get_current_language(),
        )
        return success_response(data={"reward": reward_data})
    except NotFoundError as exc:
        return not_found_response(str(exc))
    except Exception as e:
        current_app.logger.error(f"Get loyalty reward detail error: {e}")
        return internal_error_response("Failed to get loyalty reward detail")


@admin_bp.route("/loyalty/rewards", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
@validate_json(["name", "reward_type", "points_cost"])
def create_loyalty_reward():
    """
    Create a new loyalty reward

    Request Body:
        - program_id: ID of loyalty program
        - name: Reward name
        - description: Reward description
        - reward_type: Type (discount, free_product)
        - points_cost: Points required
        - min_order_value: Minimum order value (optional)
        - max_uses_per_user: Max uses per user (default: 1)
        - max_redemptions: Overall limit (optional)
        - discount_type: percentage or fixed (for discount rewards)
        - discount_value: Discount value
        - free_product_id: Product ID (for free_product rewards)
        - is_active: Active status (default: true)
        - is_featured: Featured status (default: false)
        - valid_from: Valid from date (ISO format)
        - valid_until: Valid until date (ISO format)
        - applicable_products: List of product IDs
        - applicable_categories: List of category IDs
        - terms_conditions: Terms and conditions
        - image_url: Image URL
        - sort_order: Display order (default: 0)
        - translations: Multilingual content
    """
    try:
        data = request.get_json()

        # Get default program if not specified
        program_id = data.get("program_id")
        if not program_id:
            default_program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not default_program:
                default_program = LoyaltyProgram.query.filter_by(is_active=True).first()

            if not default_program:
                return validation_error_response("No active loyalty program found")

            program_id = default_program.id

        # Validate program exists
        program = LoyaltyProgram.query.get(program_id)
        if not program:
            return not_found_response("Loyalty program not found")

        # Validate reward type. Only discount + free_product are real, redeemable
        # reward types; free_delivery (delivery is always free) and voucher were
        # never applied/redeemable and have been removed.
        reward_type = data.get("reward_type")
        valid_types = ["discount", "free_product"]
        if reward_type not in valid_types:
            return validation_error_response(f'Invalid reward_type. Must be one of: {", ".join(valid_types)}')

        # Type-specific validation
        if reward_type == "discount":
            if not data.get("discount_type") or not data.get("discount_value"):
                return validation_error_response("discount_type and discount_value required for discount rewards")

        if reward_type == "free_product":
            if not data.get("free_product_id"):
                return validation_error_response("free_product_id required for free_product rewards")
            if int(data.get("free_product_quantity", 1)) < 1:
                return validation_error_response("free_product_quantity must be at least 1")

        # Create reward
        reward = LoyaltyReward(
            program_id=program_id,
            name=data.get("name"),
            description=data.get("description"),
            reward_type=reward_type,
            points_cost=data.get("points_cost"),
            min_order_value=Decimal(str(data.get("min_order_value") or 0)),
            max_uses_per_user=data.get("max_uses_per_user", 1),
            max_redemptions=data.get("max_redemptions"),
            discount_type=data.get("discount_type"),
            discount_value=Decimal(str(data.get("discount_value"))) if data.get("discount_value") else None,
            free_product_id=data.get("free_product_id"),
            free_product_quantity=data.get("free_product_quantity", 1) if reward_type == "free_product" else None,
            is_active=data.get("is_active", True),
            is_featured=data.get("is_featured", False),
            applicable_products=data.get("applicable_products", []),
            applicable_categories=data.get("applicable_categories", []),
            terms_conditions=data.get("terms_conditions"),
            image_url=data.get("image_url"),
            sort_order=data.get("sort_order", 0),
        )

        # Set validity dates
        if data.get("valid_from"):
            reward.valid_from = datetime.fromisoformat(data["valid_from"].replace("Z", "+00:00"))
        if data.get("valid_until"):
            reward.valid_until = datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00"))

        db.session.add(reward)
        db.session.flush()

        # Handle translations
        if data.get("translations"):
            reward.set_translations(data["translations"])

        db.session.commit()

        language = get_current_language()
        return created_response(
            data={"reward": reward.to_dict(language=language)}, message="Loyalty reward created successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty reward error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to create loyalty reward")


@admin_bp.route("/loyalty/rewards/<int:reward_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def update_loyalty_reward(reward_id):
    """Update an existing loyalty reward"""
    try:
        reward = LoyaltyReward.query.get(reward_id)

        if not reward:
            return not_found_response("Loyalty reward not found")

        data = request.get_json()

        # Update basic fields
        if "name" in data:
            reward.name = data["name"]
        if "description" in data:
            reward.description = data["description"]
        if "points_cost" in data:
            reward.points_cost = data["points_cost"]
        if "min_order_value" in data:
            reward.min_order_value = (
                Decimal(str(data["min_order_value"])) if data["min_order_value"] not in (None, "") else Decimal("0.00")
            )
        if "max_uses_per_user" in data:
            reward.max_uses_per_user = data["max_uses_per_user"]
        if "max_redemptions" in data:
            reward.max_redemptions = data["max_redemptions"]
        if "discount_type" in data:
            reward.discount_type = data["discount_type"]
        if "discount_value" in data:
            reward.discount_value = (
                Decimal(str(data["discount_value"])) if data["discount_value"] not in (None, "") else None
            )
        if "free_product_id" in data:
            reward.free_product_id = data["free_product_id"]
        if "free_product_quantity" in data:
            reward.free_product_quantity = data["free_product_quantity"]
        if "is_active" in data:
            reward.is_active = data["is_active"]
        if "is_featured" in data:
            reward.is_featured = data["is_featured"]
        if "applicable_products" in data:
            reward.applicable_products = data["applicable_products"]
        if "applicable_categories" in data:
            reward.applicable_categories = data["applicable_categories"]
        if "terms_conditions" in data:
            reward.terms_conditions = data["terms_conditions"]
        if "image_url" in data:
            reward.image_url = data["image_url"]
        if "sort_order" in data:
            reward.sort_order = data["sort_order"]

        # Update validity dates
        if "valid_from" in data:
            reward.valid_from = (
                datetime.fromisoformat(data["valid_from"].replace("Z", "+00:00")) if data["valid_from"] else None
            )
        if "valid_until" in data:
            reward.valid_until = (
                datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00")) if data["valid_until"] else None
            )

        # Handle translations
        if data.get("translations"):
            reward.set_translations(data["translations"])

        db.session.commit()

        language = get_current_language()
        return success_response(
            data={"reward": reward.to_dict(language=language)}, message="Loyalty reward updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty reward error: {e}")
        return internal_error_response("Failed to update loyalty reward")


@admin_bp.route("/loyalty/rewards/<int:reward_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def delete_loyalty_reward(reward_id):
    """Delete a loyalty reward"""
    try:
        reward = LoyaltyReward.query.get(reward_id)

        if not reward:
            return not_found_response("Loyalty reward not found")

        # Check if reward has been redeemed
        if reward.redemptions_used > 0:
            # Don't delete, just deactivate
            reward.is_active = False
            db.session.commit()
            return success_response(message=get_translation("api.admin.success.loyalty_reward_deactivated"))

        # Safe to delete
        db.session.delete(reward)
        db.session.commit()

        return success_response(message=get_translation("api.admin.success.loyalty_reward_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty reward error: {e}")
        return internal_error_response("Failed to delete loyalty reward")


@admin_bp.route("/loyalty/programs", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_programs():
    """Get all loyalty programs"""
    try:
        result = AdminLoyaltyService.list_programs(
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 20, type=int),
            search=request.args.get("search", "", type=str),
            status=request.args.get("status"),
        )
        return paginated_response(
            items=result["items"],
            total=result["total"],
            page=result["page"],
            per_page=result["per_page"],
        )
    except Exception as e:
        current_app.logger.error(f"Get loyalty programs error: {e}")
        return internal_error_response("Failed to get loyalty programs")


@admin_bp.route("/loyalty/programs", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def create_loyalty_program():
    """Create a new loyalty program"""
    try:
        data = request.get_json()

        # Validate required fields
        if not data.get("name"):
            return validation_error_response("Program name is required")

        # Create new program
        program = LoyaltyProgram(
            name=data["name"],
            description=data.get("description"),
            is_active=data.get("is_active", True),
            is_default=data.get("is_default", False),
            uzs_per_point=data.get("uzs_per_point", 250),
            signup_bonus=data.get("signup_bonus", 100),
            referral_bonus=data.get("referral_bonus", 50),
            birthday_bonus=data.get("birthday_bonus", 25),
            points_expiry_days=data.get("points_expiry_days", 365),
            min_redemption_points=data.get("min_redemption_points", 100),
            surprise_enabled=data.get("surprise_enabled", True),
            surprise_chance_percent=data.get("surprise_chance_percent", 5),
            surprise_amounts=data.get("surprise_amounts", "50,100,200"),
            surprise_cooldown_days=data.get("surprise_cooldown_days", 7),
            surprise_daily_cap=data.get("surprise_daily_cap", 5),
            # tier_thresholds / tier_multipliers intentionally NOT set: tiers are
            # owned by LoyaltyTierConfig (single source of truth), not program JSON.
            terms_and_conditions=data.get("terms_and_conditions"),
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
        )

        db.session.add(program)
        db.session.commit()

        if "translations" in data:
            program.set_translations(data["translations"])
            db.session.commit()

        current_app.logger.info(f"Loyalty program created: {program.name} (ID: {program.id})")

        return success_response(
            data={"program": program.to_dict()}, message="Loyalty program created successfully", status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty program error: {e}")
        return internal_error_response("Failed to create loyalty program")


@admin_bp.route("/loyalty/programs/<int:program_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def update_loyalty_program(program_id):
    """Update loyalty program settings"""
    try:
        program = LoyaltyProgram.query.get(program_id)

        if not program:
            return not_found_response("Loyalty program not found")

        data = request.get_json()

        # Update fields
        if "name" in data:
            program.name = data["name"]
        if "description" in data:
            program.description = data["description"]
        if "is_active" in data:
            program.is_active = data["is_active"]
        if "uzs_per_point" in data:
            program.uzs_per_point = data["uzs_per_point"]
        if "signup_bonus" in data:
            program.signup_bonus = data["signup_bonus"]
        if "referral_bonus" in data:
            program.referral_bonus = data["referral_bonus"]
        if "birthday_bonus" in data:
            program.birthday_bonus = data["birthday_bonus"]
        if "points_expiry_days" in data:
            program.points_expiry_days = data["points_expiry_days"]
        if "min_redemption_points" in data:
            program.min_redemption_points = data["min_redemption_points"]
        if "surprise_enabled" in data:
            program.surprise_enabled = data["surprise_enabled"]
        if "surprise_chance_percent" in data:
            program.surprise_chance_percent = data["surprise_chance_percent"]
        if "surprise_amounts" in data:
            program.surprise_amounts = data["surprise_amounts"]
        if "surprise_cooldown_days" in data:
            program.surprise_cooldown_days = data["surprise_cooldown_days"]
        if "surprise_daily_cap" in data:
            program.surprise_daily_cap = data["surprise_daily_cap"]
        # tier_thresholds / tier_multipliers intentionally NOT writable: tiers are
        # owned by LoyaltyTierConfig (single source of truth), not program JSON.
        if "terms_and_conditions" in data:
            program.terms_and_conditions = data["terms_and_conditions"]

        db.session.commit()

        if "translations" in data:
            program.set_translations(data["translations"])
            db.session.commit()

        return success_response(data={"program": program.to_dict()}, message="Loyalty program updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty program error: {e}")
        return internal_error_response("Failed to update loyalty program")


@admin_bp.route("/loyalty/programs/<int:program_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def delete_loyalty_program(program_id):
    """Delete a loyalty program"""
    try:
        program = LoyaltyProgram.query.get(program_id)

        if not program:
            return not_found_response("Loyalty program not found")

        # Check if this is the default program
        if program.is_default:
            return validation_error_response("Cannot delete the default loyalty program")

        # Check if program has active members
        from business_app.models.loyalty import LoyaltyPoints

        member_count = LoyaltyPoints.query.filter_by(program_id=program_id).count()

        if member_count > 0:
            # Soft delete - just deactivate
            program.is_active = False
            db.session.commit()
            current_app.logger.info(f"Loyalty program deactivated: {program.name} (ID: {program.id})")
            return success_response(message=f"Program deactivated (has {member_count} members)")
        else:
            # Hard delete if no members
            program_name = program.name
            db.session.delete(program)
            db.session.commit()
            current_app.logger.info(f"Loyalty program deleted: {program_name} (ID: {program_id})")
            return success_response(message=get_translation("api.admin.success.loyalty_program_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty program error: {e}")
        return internal_error_response("Failed to delete loyalty program")


# ============================================================================
# LOYALTY TIER CONFIGURATION MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/loyalty/tiers", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_tier_configs():
    """
    Get all loyalty tier configurations.

    Query Parameters:
        - program_id: Filter by loyalty program (optional, uses default if not specified)
    """
    try:
        from business_app.models.loyalty import LoyaltyTierConfig

        program_id = request.args.get("program_id", type=int)

        query = LoyaltyTierConfig.query

        if program_id:
            query = query.filter_by(program_id=program_id)
        else:
            # Get default program's tiers
            default_program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            if default_program:
                query = query.filter_by(program_id=default_program.id)

        tiers = query.order_by(LoyaltyTierConfig.display_order.asc()).all()

        return success_response(
            data={
                "tiers": [
                    {**tier.to_dict(), "translations": {"name": tier.get_all_translations("name")}} for tier in tiers
                ],
                "tier_count": len(tiers),
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty tier configs error: {e}")
        return internal_error_response("Failed to get loyalty tier configurations")


@admin_bp.route("/loyalty/tiers", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
@validate_json(["name", "min_points"])
def create_loyalty_tier_config():
    """
    Create a new loyalty tier configuration.

    Request Body:
        - name: Tier name (e.g., "Diamond")
        - program_id: Loyalty program ID (optional, uses default)
        - display_order: Order for display (optional)
        - min_points: Minimum points to qualify
        - max_points: Maximum points (null for highest tier)
        - points_multiplier: Points earning multiplier (default: 1.0)
        - discount_percentage: Discount percentage (default: 0)
        - benefits: List of benefit descriptions
        - color: Hex color for UI
        - icon: Font Awesome icon class
        - is_active: Active status (default: true)
    """
    try:
        from business_app.models.loyalty import LoyaltyTierConfig

        data = request.get_json()

        # Get program ID
        program_id = data.get("program_id")
        if not program_id:
            default_program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not default_program:
                return validation_error_response("No default loyalty program found")
            program_id = default_program.id

        # Check for duplicate tier name in same program
        existing = LoyaltyTierConfig.query.filter_by(program_id=program_id, name=data.get("name")).first()

        if existing:
            return validation_error_response(f"Tier '{data.get('name')}' already exists in this program")

        # Create tier
        tier = LoyaltyTierConfig(
            program_id=program_id,
            name=data.get("name"),
            display_order=data.get("display_order", 0),
            min_points=data.get("min_points", 0),
            max_points=data.get("max_points"),
            points_multiplier=data.get("points_multiplier", 1.0),
            discount_percentage=data.get("discount_percentage", 0),
            benefits=data.get("benefits", []),
            color=data.get("color", "#CD7F32"),
            icon=data.get("icon", "fa-medal"),
            is_active=data.get("is_active", True),
        )

        db.session.add(tier)
        db.session.commit()

        if "translations" in data:
            tier.set_translations(data["translations"])
            db.session.commit()

        # Invalidate tier cache
        from business_app.utils.decorators import invalidate_cache

        invalidate_cache("loyalty:tiers")

        current_app.logger.info(f"Loyalty tier created: {tier.name} (ID: {tier.id})")

        return success_response(
            data={"tier": tier.to_dict()}, message="Loyalty tier created successfully", status_code=201
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty tier config error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to create loyalty tier")


@admin_bp.route("/loyalty/tiers/<int:tier_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def update_loyalty_tier_config(tier_id):
    """Update an existing loyalty tier configuration"""
    try:
        from business_app.models.loyalty import LoyaltyTierConfig

        tier = LoyaltyTierConfig.query.get(tier_id)

        if not tier:
            return not_found_response("Loyalty tier not found")

        data = request.get_json()

        # Update fields
        if "name" in data:
            # Check for duplicate name
            existing = LoyaltyTierConfig.query.filter(
                LoyaltyTierConfig.program_id == tier.program_id,
                LoyaltyTierConfig.name == data["name"],
                LoyaltyTierConfig.id != tier_id,
            ).first()
            if existing:
                return validation_error_response(f"Tier '{data['name']}' already exists")
            tier.name = data["name"]

        if "display_order" in data:
            tier.display_order = data["display_order"]
        if "min_points" in data:
            tier.min_points = data["min_points"]
        if "max_points" in data:
            tier.max_points = data["max_points"]
        if "points_multiplier" in data:
            tier.points_multiplier = data["points_multiplier"]
        if "discount_percentage" in data:
            tier.discount_percentage = data["discount_percentage"]
        if "benefits" in data:
            tier.benefits = data["benefits"]
        if "color" in data:
            tier.color = data["color"]
        if "icon" in data:
            tier.icon = data["icon"]
        if "is_active" in data:
            tier.is_active = data["is_active"]

        db.session.commit()

        if "translations" in data:
            tier.set_translations(data["translations"])
            db.session.commit()

        # Invalidate tier cache
        from business_app.utils.decorators import invalidate_cache

        invalidate_cache("loyalty:tiers")

        current_app.logger.info(f"Loyalty tier updated: {tier.name} (ID: {tier.id})")

        return success_response(data={"tier": tier.to_dict()}, message="Loyalty tier updated successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty tier config error: {e}")
        return internal_error_response("Failed to update loyalty tier")


@admin_bp.route("/loyalty/tiers/<int:tier_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def delete_loyalty_tier_config(tier_id):
    """Delete a loyalty tier configuration"""
    try:
        from business_app.models.loyalty import LoyaltyTierConfig, LoyaltyPoints

        tier = LoyaltyTierConfig.query.get(tier_id)

        if not tier:
            return not_found_response("Loyalty tier not found")

        # Check if users are currently in this tier
        users_in_tier = LoyaltyPoints.query.filter_by(current_tier=tier.name).count()

        if users_in_tier > 0:
            # Soft delete - deactivate instead
            tier.is_active = False
            db.session.commit()
            current_app.logger.info(f"Loyalty tier deactivated: {tier.name} (has {users_in_tier} users)")
            return success_response(message=f"Tier deactivated (has {users_in_tier} users in this tier)")

        # Hard delete
        tier_name = tier.name
        db.session.delete(tier)
        db.session.commit()

        # Invalidate tier cache
        from business_app.utils.decorators import invalidate_cache

        invalidate_cache("loyalty:tiers")

        current_app.logger.info(f"Loyalty tier deleted: {tier_name} (ID: {tier_id})")

        return success_response(message="Loyalty tier deleted successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty tier config error: {e}")
        return internal_error_response("Failed to delete loyalty tier")


# LOYALTY STREAK RULE MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/loyalty/streak-rules", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "manage_loyalty"])
def get_loyalty_streak_rules():
    """
    List streak rules.

    Query Parameters:
        - program_id: Filter by loyalty program (optional, uses default if not specified)
    """
    try:
        from business_app.models.loyalty import LoyaltyStreakRule

        program_id = request.args.get("program_id", type=int)

        query = LoyaltyStreakRule.query

        if program_id:
            query = query.filter_by(program_id=program_id)
        else:
            default_program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            if default_program:
                query = query.filter_by(program_id=default_program.id)

        rules = query.order_by(LoyaltyStreakRule.display_order.asc()).all()

        return success_response(
            data={
                "streak_rules": [
                    {**rule.to_dict(), "translations": {"name": rule.get_all_translations("name")}} for rule in rules
                ],
                "streak_rule_count": len(rules),
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty streak rules error: {e}")
        return internal_error_response("Failed to get loyalty streak rules")


@admin_bp.route("/loyalty/streak-rules", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
@validate_json(["name", "required_orders", "window_days", "bonus_points"])
def create_loyalty_streak_rule():
    """
    Create a streak rule.

    Request Body:
        - name: Rule name (user-facing, translatable)
        - program_id: Loyalty program ID (optional, uses default)
        - required_orders: Number of orders to complete (≥ 1)
        - window_days: Trailing window in days (≥ 1)
        - bonus_points: Points awarded on streak completion (≥ 1)
        - min_order_amount: Minimum order amount per order (optional, > 0)
        - is_active: Active status (default: true)
        - starts_at: ISO datetime when rule becomes effective (optional)
        - ends_at: ISO datetime when rule expires (optional)
        - display_order: Display ordering (default: 0)
        - translations: Dict of {lang: name} overrides (optional)
    """
    try:
        from business_app.models.loyalty import LoyaltyStreakRule
        from datetime import datetime as _dt

        data = request.get_json()

        # Resolve program
        program_id = data.get("program_id")
        if not program_id:
            default_program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not default_program:
                return validation_error_response("No default loyalty program found")
            program_id = default_program.id

        # Validate numeric constraints
        if int(data["required_orders"]) < 1 or int(data["window_days"]) < 1 or int(data["bonus_points"]) < 1:
            return validation_error_response("required_orders, window_days and bonus_points must be ≥ 1")
        if data.get("min_order_amount") is not None and float(data["min_order_amount"]) <= 0:
            return validation_error_response("min_order_amount must be > 0 when set")

        # Parse optional datetime fields
        starts_at = _dt.fromisoformat(data["starts_at"]) if data.get("starts_at") else None
        ends_at = _dt.fromisoformat(data["ends_at"]) if data.get("ends_at") else None
        if starts_at and ends_at and ends_at <= starts_at:
            return validation_error_response("ends_at must be after starts_at")

        rule = LoyaltyStreakRule(
            program_id=program_id,
            name=data["name"],
            required_orders=int(data["required_orders"]),
            window_days=int(data["window_days"]),
            bonus_points=int(data["bonus_points"]),
            min_order_amount=data.get("min_order_amount"),
            is_active=data.get("is_active", True),
            starts_at=starts_at,
            ends_at=ends_at,
            display_order=data.get("display_order", 0),
        )
        db.session.add(rule)
        db.session.commit()

        if "translations" in data:
            rule.set_translations(data["translations"])
            db.session.commit()

        current_app.logger.info(f"Loyalty streak rule created: {rule.name} (ID: {rule.id})")
        return success_response(
            data={"streak_rule": rule.to_dict()},
            message="Streak rule created successfully",
            status_code=201,
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create loyalty streak rule error: {e}")
        return internal_error_response("Failed to create streak rule")


@admin_bp.route("/loyalty/streak-rules/<int:rule_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def update_loyalty_streak_rule(rule_id):
    """Update a streak rule."""
    try:
        from business_app.models.loyalty import LoyaltyStreakRule
        from datetime import datetime as _dt

        rule = LoyaltyStreakRule.query.get(rule_id)
        if not rule:
            return not_found_response("Streak rule not found")
        data = request.get_json()

        for field, caster in (
            ("name", str),
            ("required_orders", int),
            ("window_days", int),
            ("bonus_points", int),
            ("display_order", int),
            ("is_active", bool),
        ):
            if field in data and data[field] is not None:
                setattr(rule, field, caster(data[field]))
        if "min_order_amount" in data:
            rule.min_order_amount = data["min_order_amount"]  # may be None
        if "starts_at" in data:
            rule.starts_at = _dt.fromisoformat(data["starts_at"]) if data["starts_at"] else None
        if "ends_at" in data:
            rule.ends_at = _dt.fromisoformat(data["ends_at"]) if data["ends_at"] else None
        if rule.required_orders < 1 or rule.window_days < 1 or rule.bonus_points < 1:
            return validation_error_response("required_orders, window_days and bonus_points must be ≥ 1")
        if rule.starts_at and rule.ends_at and rule.ends_at <= rule.starts_at:
            return validation_error_response("ends_at must be after starts_at")

        db.session.commit()
        if "translations" in data:
            rule.set_translations(data["translations"])
            db.session.commit()
        return success_response(data={"streak_rule": rule.to_dict()}, message="Streak rule updated")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update loyalty streak rule error: {e}")
        return internal_error_response("Failed to update streak rule")


@admin_bp.route("/loyalty/streak-rules/<int:rule_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_loyalty"])
def delete_loyalty_streak_rule(rule_id):
    """Delete a streak rule (hard delete — rules carry no per-user state)."""
    try:
        from business_app.models.loyalty import LoyaltyStreakRule

        rule = LoyaltyStreakRule.query.get(rule_id)
        if not rule:
            return not_found_response("Streak rule not found")
        db.session.delete(rule)
        db.session.commit()
        return success_response(message="Streak rule deleted successfully")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete loyalty streak rule error: {e}")
        return internal_error_response("Failed to delete streak rule")


@admin_bp.route("/loyalty/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_loyalty", "view_reports"])
def get_loyalty_analytics():
    """
    Get loyalty program analytics and statistics

    Query Parameters:
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format)
        - program_id: Filter by program ID
    """
    try:
        return success_response(
            data=AdminLoyaltyService.get_analytics(
                start_date=request.args.get("start_date"),
                end_date=request.args.get("end_date"),
                program_id=request.args.get("program_id", type=int),
            )
        )
    except Exception as e:
        current_app.logger.error(f"Get loyalty analytics error: {e}")
        import traceback

        current_app.logger.error(traceback.format_exc())
        return internal_error_response("Failed to get loyalty analytics")


# ============================================================================
# NOTIFICATION CAMPAIGN MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/notification-campaigns", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_campaigns():
    """Get saved notification campaigns for the admin notifications page."""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)
        search = request.args.get("search")
        status = request.args.get("status")
        channel = request.args.get("channel") or request.args.get("type")
        target_audience = request.args.get("target_audience")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        campaigns_data = get_notification_service().get_notification_campaigns_paginated(
            requester_id=get_jwt_identity(),
            page=page,
            per_page=per_page,
            search=search,
            status=status,
            channel=channel,
            target_audience=target_audience,
            start_date=start_date,
            end_date=end_date,
        )

        return paginated_response(
            items=campaigns_data["items"],
            total=campaigns_data["total"],
            page=campaigns_data["page"],
            per_page=campaigns_data["per_page"],
        )

    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except ValueError:
        return validation_error_response("Invalid pagination value")
    except Exception as e:
        current_app.logger.error(f"Get notification campaigns error: {e}")
        return internal_error_response("Failed to get notification campaigns")


@admin_bp.route("/notification-campaigns", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def create_notification_campaign():
    """Create a notification campaign."""
    try:
        campaign = get_notification_service().create_notification_campaign(
            sender_id=get_jwt_identity(),
            payload=request.get_json() or {},
        )

        return created_response(
            data={"campaign": campaign},
            message="Notification campaign created successfully",
        )

    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Create notification campaign error: {e}")
        return internal_error_response("Failed to create notification campaign")


@admin_bp.route("/notification-campaigns/<int:campaign_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_campaign_detail(campaign_id):
    """Get notification campaign details."""
    try:
        campaign = get_notification_service().get_notification_campaign_detail(
            requester_id=get_jwt_identity(),
            campaign_id=campaign_id,
        )
        return success_response(data={"campaign": campaign})
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification campaign detail error: {e}")
        return internal_error_response("Failed to get notification campaign detail")


@admin_bp.route("/notification-campaigns/<int:campaign_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def update_notification_campaign(campaign_id):
    """Update a notification campaign."""
    try:
        campaign = get_notification_service().update_notification_campaign(
            sender_id=get_jwt_identity(),
            campaign_id=campaign_id,
            payload=request.get_json() or {},
        )
        return success_response(
            data={"campaign": campaign},
            message="Notification campaign updated successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except (ValidationError, ConflictError) as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Update notification campaign error: {e}")
        return internal_error_response("Failed to update notification campaign")


@admin_bp.route("/notification-campaigns/<int:campaign_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def delete_notification_campaign(campaign_id):
    """Delete a draft or cancelled notification campaign."""
    try:
        get_notification_service().delete_notification_campaign(
            sender_id=get_jwt_identity(),
            campaign_id=campaign_id,
        )
        return success_response(message="Notification campaign deleted successfully")
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except (ValidationError, ConflictError) as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Delete notification campaign error: {e}")
        return internal_error_response("Failed to delete notification campaign")


@admin_bp.route("/notification-campaigns/<int:campaign_id>/duplicate", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def duplicate_notification_campaign(campaign_id):
    """Duplicate a notification campaign."""
    try:
        campaign = get_notification_service().duplicate_notification_campaign(
            sender_id=get_jwt_identity(),
            campaign_id=campaign_id,
        )
        return created_response(
            data={"campaign": campaign},
            message="Notification campaign duplicated successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Duplicate notification campaign error: {e}")
        return internal_error_response("Failed to duplicate notification campaign")


@admin_bp.route("/notification-campaigns/<int:campaign_id>/send", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def send_notification_campaign(campaign_id):
    """Queue a notification campaign for immediate or scheduled execution."""
    try:
        payload = request.get_json() or {}
        send_now = payload.get("send_now")
        if send_now is None:
            send_now = not bool(payload.get("scheduled_at"))

        if "scheduled_at" in payload:
            get_notification_service().update_notification_campaign(
                sender_id=get_jwt_identity(),
                campaign_id=campaign_id,
                payload=payload,
            )

        campaign = get_notification_service().queue_notification_campaign(
            sender_id=get_jwt_identity(),
            campaign_id=campaign_id,
            send_now=bool(send_now),
        )
        return success_response(
            data={"campaign": campaign},
            message="Notification campaign queued successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except (ValidationError, ConflictError) as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Send notification campaign error: {e}")
        return internal_error_response("Failed to queue notification campaign")


@admin_bp.route("/notification-campaigns/<int:campaign_id>/cancel", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def cancel_notification_campaign(campaign_id):
    """Cancel a scheduled or sending notification campaign."""
    try:
        campaign = get_notification_service().cancel_notification_campaign(
            sender_id=get_jwt_identity(),
            campaign_id=campaign_id,
        )
        return success_response(
            data={"campaign": campaign},
            message="Notification campaign cancelled successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except (ValidationError, ConflictError) as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Cancel notification campaign error: {e}")
        return internal_error_response("Failed to cancel notification campaign")


# ============================================================================
# NOTIFICATION TEMPLATE MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/notification-templates", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_templates():
    """
    Get all notification templates with filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (default: 20)
        - notification_type: Filter by type
        - channel: Filter by channel (email, sms, push, telegram)
        - is_active: Filter by active status
        - search: Search in name, subject, content
    """
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)
        is_active = request.args.get("is_active")
        is_active_bool = None
        if is_active is not None:
            is_active_bool = is_active.lower() == "true"

        templates_data = get_notification_service().get_admin_notification_templates_paginated(
            requester_id=get_jwt_identity(),
            page=page,
            per_page=per_page,
            search=request.args.get("search"),
            notification_type=request.args.get("notification_type"),
            channel=request.args.get("channel"),
            is_active=is_active_bool,
        )

        return paginated_response(
            items=templates_data["items"],
            total=templates_data["total"],
            page=templates_data["page"],
            per_page=templates_data["per_page"],
        )
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification templates error: {e}")
        return internal_error_response("Failed to get notification templates")


@admin_bp.route("/notification-templates/<int:template_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_template_detail(template_id):
    """Get detailed information about a specific notification template"""
    try:
        template_data = get_notification_service().get_admin_notification_template_detail(
            requester_id=get_jwt_identity(),
            template_id=template_id,
        )
        return success_response(data={"template": template_data})
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification template detail error: {e}")
        return internal_error_response("Failed to get notification template detail")


@admin_bp.route("/notification-templates", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def create_notification_template():
    """
    Create a new notification template

    Request Body:
        - name: Template name
        - notification_type: Type (order_update, delivery_reminder, etc.)
        - channel: Channel (email, sms, push, telegram)
        - subject: Email subject (required for email channel)
        - content: Template content with placeholders
        - is_active: Active status (default: true)
        - translations: Multilingual content
    """
    try:
        template = get_notification_service().create_admin_notification_template(
            requester_id=get_jwt_identity(),
            payload=request.get_json() or {},
        )
        return created_response(data={"template": template}, message="Notification template created successfully")
    except ConflictError as e:
        return validation_error_response(str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Create notification template error: {e}")
        return internal_error_response("Failed to create notification template")


@admin_bp.route("/notification-templates/<int:template_id>", methods=["PUT"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def update_notification_template(template_id):
    """Update an existing notification template"""
    try:
        template = get_notification_service().update_admin_notification_template(
            requester_id=get_jwt_identity(),
            template_id=template_id,
            payload=request.get_json() or {},
        )
        return success_response(data={"template": template}, message="Notification template updated successfully")
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except (ValidationError, ConflictError) as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Update notification template error: {e}")
        return internal_error_response("Failed to update notification template")


@admin_bp.route("/notification-templates/<int:template_id>", methods=["DELETE"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def delete_notification_template(template_id):
    """Delete a notification template"""
    try:
        template = get_notification_service().delete_admin_notification_template(
            requester_id=get_jwt_identity(),
            template_id=template_id,
            reactivate=bool((request.get_json(silent=True) or {}).get("reactivate")),
        )
        return success_response(
            data={"template": template},
            message="Notification template status updated successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Delete notification template error: {e}")
        return internal_error_response("Failed to delete notification template")


@admin_bp.route("/notification-templates/<int:template_id>/preview", methods=["POST"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def preview_notification_template(template_id):
    """
    Preview a notification template with sample data

    Request Body:
        - variables: Dictionary of placeholder values
        - language: Language code (optional)
    """
    try:
        preview = get_notification_service().preview_admin_notification_template(
            requester_id=get_jwt_identity(),
            template_id=template_id,
            payload=request.get_json() or {},
        )
        return success_response(data={"preview": preview})
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Preview notification template error: {e}")
        return internal_error_response("Failed to preview notification template")


@admin_bp.route("/notification-templates/<int:template_id>/test-send", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_notifications"])
def test_send_notification_template(template_id):
    """Send a test notification using a template."""
    try:
        result = get_notification_service().test_send_admin_notification_template(
            requester_id=get_jwt_identity(),
            template_id=template_id,
            payload=request.get_json() or {},
        )
        return success_response(
            data={"test_send": result},
            message="Notification template test send queued successfully",
        )
    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Test send notification template error: {e}")
        return internal_error_response("Failed to send notification template test")


@admin_bp.route("/notification-templates/types", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_types():
    """Get list of available notification types"""
    try:
        types_data = get_notification_service().get_admin_notification_types(
            requester_id=get_jwt_identity(),
        )
        return success_response(data={"types": types_data})
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification types error: {e}")
        return internal_error_response("Failed to get notification types")


@admin_bp.route("/notification-templates/channels", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_channels():
    """Get list of available notification channels"""
    try:
        channels = get_notification_service().get_admin_notification_channels(
            requester_id=get_jwt_identity(),
        )
        return success_response(data={"channels": channels})
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification channels error: {e}")
        return internal_error_response("Failed to get notification channels")


@admin_bp.route("/notification-campaign-segments", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_notifications", "manage_notifications"])
def get_notification_campaign_segments():
    """Get available segments for notification campaign targeting."""
    try:
        segments = get_notification_service().get_admin_notification_segments(
            requester_id=get_jwt_identity(),
        )
        return success_response(data={"segments": segments})
    except ValidationError as e:
        return validation_error_response(str(e))
    except ForbiddenError as e:
        return forbidden_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Get notification campaign segments error: {e}")
        return internal_error_response("Failed to get notification campaign segments")


@admin_bp.route("/system-settings", methods=["GET"])
@jwt_required()
@super_admin_required
def get_system_settings():
    """Get system settings"""
    try:
        # Get current system settings from app config and database
        settings = {
            "general": {
                "app_name": current_app.config.get("APP_NAME", "BlueStream Water Delivery"),
                "timezone": current_app.config.get("TIMEZONE", DISPLAY_TIMEZONE),
                "default_language": current_app.config.get("DEFAULT_LANGUAGE", "uz"),
                "supported_languages": current_app.config.get("SUPPORTED_LANGUAGES", ["uz", "ru", "en"]),
                "maintenance_mode": current_app.config.get("MAINTENANCE_MODE", False),
            },
            "business": {
                "currency": current_app.config.get("CURRENCY", "UZS"),
                "currency_symbol": current_app.config.get("CURRENCY_SYMBOL", "сум"),
                "tax_rate": current_app.config.get("TAX_RATE", 0),
                "min_order_amount": current_app.config["MIN_ORDER_AMOUNT"],
                "default_delivery_fee": current_app.config["DEFAULT_DELIVERY_FEE"],
            },
            "orders": {
                "auto_cancel_pending_orders_hours": current_app.config.get("AUTO_CANCEL_PENDING_ORDERS_HOURS", 24),
                "allow_order_cancellation_minutes": current_app.config.get("ALLOW_ORDER_CANCELLATION_MINUTES", 30),
                "max_order_items": current_app.config.get("MAX_ORDER_ITEMS", 50),
                "order_number_prefix": current_app.config.get("ORDER_NUMBER_PREFIX", "ORD"),
            },
            "delivery": {
                "delivery_radius_km": current_app.config.get("DELIVERY_RADIUS_KM", 50),
                "delivery_slots_enabled": current_app.config.get("DELIVERY_SLOTS_ENABLED", True),
                "same_day_delivery_cutoff_hour": current_app.config.get("SAME_DAY_DELIVERY_CUTOFF_HOUR", 14),
                "avg_delivery_time_minutes": current_app.config.get("AVG_DELIVERY_TIME_MINUTES", 60),
            },
            # Loyalty config is NOT exposed here: it lives in LoyaltyProgram rows
            # (single source of truth) and is managed via the dedicated admin
            # loyalty programs/rewards/tiers endpoints. The former System-Settings
            # loyalty category was a phantom surface reading undefined config keys
            # (always 1/100/50/365) with no UI consumer — removed (loyalty SSOT, Phase 2).
            "notifications": {
                "sms_enabled": current_app.config.get("SMS_ENABLED", True),
                "email_enabled": current_app.config.get("EMAIL_ENABLED", True),
                "telegram_enabled": current_app.config.get("TELEGRAM_ENABLED", True),
                "push_enabled": current_app.config.get("PUSH_ENABLED", False),
                "order_confirmation_sms": current_app.config.get("ORDER_CONFIRMATION_SMS", True),
                "delivery_reminder_sms": current_app.config.get("DELIVERY_REMINDER_SMS", True),
            },
            "payments": {
                "payment_methods_enabled": current_app.config.get(
                    "PAYMENT_METHODS_ENABLED", ["cash", "card", "online"]
                ),
                "default_payment_method": current_app.config.get("DEFAULT_PAYMENT_METHOD", "cash"),
                "payment_timeout_minutes": current_app.config.get("PAYMENT_TIMEOUT_MINUTES", 15),
                "auto_refund_failed_deliveries": current_app.config.get("AUTO_REFUND_FAILED_DELIVERIES", True),
            },
            "security": {
                "max_login_attempts": current_app.config.get("MAX_LOGIN_ATTEMPTS", 5),
                "login_lockout_minutes": current_app.config.get("LOGIN_LOCKOUT_MINUTES", 30),
                "password_min_length": current_app.config.get("PASSWORD_MIN_LENGTH", 8),
                "session_timeout_minutes": current_app.config.get("SESSION_TIMEOUT_MINUTES", 1440),
                "require_email_verification": current_app.config.get("REQUIRE_EMAIL_VERIFICATION", False),
                "require_phone_verification": current_app.config.get("REQUIRE_PHONE_VERIFICATION", True),
            },
            "api": {
                "rate_limit_enabled": current_app.config.get("RATE_LIMIT_ENABLED", True),
                "rate_limit_per_minute": current_app.config.get("RATE_LIMIT_PER_MINUTE", 60),
                "api_version": current_app.config.get("API_VERSION", "v1"),
                "cors_enabled": current_app.config.get("CORS_ENABLED", True),
            },
            "files": {
                "max_upload_size_mb": current_app.config.get("MAX_UPLOAD_SIZE_MB", 10),
                "allowed_image_extensions": current_app.config.get(
                    "ALLOWED_IMAGE_EXTENSIONS", ["jpg", "jpeg", "png", "webp"]
                ),
                "allowed_document_extensions": current_app.config.get(
                    "ALLOWED_DOCUMENT_EXTENSIONS", ["pdf", "doc", "docx"]
                ),
            },
        }

        return success_response(data={"settings": settings})

    except Exception as e:
        current_app.logger.error(f"Get system settings error: {e}")
        return internal_error_response("Failed to get system settings")


@admin_bp.route("/system-settings", methods=["PUT"])
@jwt_required()
@super_admin_required
@validate_json()
def update_system_settings():
    """Update system settings"""
    try:
        get_jwt_identity()
        data = request.get_json()

        if not data:
            return validation_error_response("Settings data is required")

        # Track which settings were updated
        updated_settings = []

        # Update settings categories
        categories = [
            "general",
            "business",
            "orders",
            "delivery",
            # "loyalty" removed (loyalty SSOT, Phase 2): config lives in LoyaltyProgram.
            "notifications",
            "payments",
            "security",
            "api",
            "files",
        ]

        for category in categories:
            if category in data:
                category_settings = data[category]

                # Validate and update each setting in the category
                for key, value in category_settings.items():
                    setting_key = f"{category.upper()}_{key.upper()}"

                    # Validate specific settings
                    if category == "business":
                        if key == "tax_rate" and (value < 0 or value > 100):
                            return validation_error_response("Tax rate must be between 0 and 100")
                        if key in ["min_order_amount", "default_delivery_fee"] and value < 0:
                            return validation_error_response(f"{key} cannot be negative")

                    if category == "orders":
                        if (
                            key
                            in [
                                "auto_cancel_pending_orders_hours",
                                "allow_order_cancellation_minutes",
                                "max_order_items",
                            ]
                            and value < 0
                        ):
                            return validation_error_response(f"{key} must be positive")

                    if category == "delivery":
                        if key == "delivery_radius_km" and value <= 0:
                            return validation_error_response("Delivery radius must be positive")
                        if key == "same_day_delivery_cutoff_hour" and (value < 0 or value > 23):
                            return validation_error_response("Cutoff hour must be between 0 and 23")

                    if category == "security":
                        if key == "password_min_length" and value < 6:
                            return validation_error_response("Password minimum length must be at least 6")
                        if (
                            key in ["max_login_attempts", "login_lockout_minutes", "session_timeout_minutes"]
                            and value <= 0
                        ):
                            return validation_error_response(f"{key} must be positive")

                    if category == "api":
                        if key == "rate_limit_per_minute" and value <= 0:
                            return validation_error_response("Rate limit must be positive")

                    if category == "files":
                        if key == "max_upload_size_mb" and value <= 0:
                            return validation_error_response("Max upload size must be positive")

                    # Update config (runtime only - would need env file or database persistence for permanent storage)
                    current_app.config[setting_key] = value
                    updated_settings.append(setting_key)

        # Log the settings update for audit
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="update_system_settings",
            severity=AuditSeverity.HIGH,
            resource_type="system_settings",
            description=f'System settings updated: {", ".join(updated_settings)}',
            new_values=data,
            success=True,
        )

        return success_response(
            message=f"System settings updated successfully ({len(updated_settings)} settings changed)",
            data={
                "updated_settings": updated_settings,
                "note": "Settings are updated in runtime. For permanent changes, update environment variables or configuration files.",  # noqa: E501
            },
        )

    except Exception as e:
        current_app.logger.error(f"Update system settings error: {e}")
        return internal_error_response("Failed to update system settings")


@admin_bp.route("/system-settings/categories", methods=["GET"])
@jwt_required()
@super_admin_required
def get_system_settings_categories():
    """Get list of system settings categories with descriptions"""
    try:
        categories = [
            {
                "key": "general",
                "name": "General Settings",
                "description": "Application name, timezone, language, and maintenance mode",
            },
            {
                "key": "business",
                "name": "Business Settings",
                "description": "Currency, tax rate, minimum order amount, delivery fees",
            },
            {
                "key": "orders",
                "name": "Order Settings",
                "description": "Order cancellation, auto-cancel, maximum items",
            },
            {
                "key": "delivery",
                "name": "Delivery Settings",
                "description": "Delivery radius, time slots, same-day delivery cutoff",
            },
            # Loyalty Program Settings removed (loyalty SSOT, Phase 2): managed via
            # the dedicated admin loyalty programs/tiers/rewards endpoints.
            {
                "key": "notifications",
                "name": "Notification Settings",
                "description": "SMS, email, Telegram, push notifications configuration",
            },
            {"key": "payments", "name": "Payment Settings", "description": "Payment methods, timeouts, auto-refunds"},
            {
                "key": "security",
                "name": "Security Settings",
                "description": "Login attempts, password rules, verification requirements",
            },
            {"key": "api", "name": "API Settings", "description": "Rate limiting, versioning, CORS configuration"},
            {
                "key": "files",
                "name": "File Upload Settings",
                "description": "Maximum upload size, allowed file extensions",
            },
        ]

        return success_response(data={"categories": categories})

    except Exception as e:
        current_app.logger.error(f"Get system settings categories error: {e}")
        return internal_error_response("Failed to get system settings categories")


@admin_bp.route("/system-settings/reset", methods=["POST"])
@jwt_required()
@super_admin_required
@validate_json()
def reset_system_settings():
    """Reset system settings to defaults"""
    try:
        get_jwt_identity()
        data = request.get_json()

        category = data.get("category")

        if category and category not in [
            "general",
            "business",
            "orders",
            "delivery",
            "notifications",
            "payments",
            "security",
            "api",
            "files",
        ]:
            return validation_error_response("Invalid category")

        # Define default values
        defaults = {
            "GENERAL_APP_NAME": "BlueStream Water Delivery",
            "GENERAL_TIMEZONE": DISPLAY_TIMEZONE,
            "GENERAL_DEFAULT_LANGUAGE": "uz",
            "GENERAL_SUPPORTED_LANGUAGES": ["uz", "ru", "en"],
            "GENERAL_MAINTENANCE_MODE": False,
            "BUSINESS_CURRENCY": "UZS",
            "BUSINESS_CURRENCY_SYMBOL": "сум",
            "BUSINESS_TAX_RATE": 0,
            "BUSINESS_MIN_ORDER_AMOUNT": 0,
            "BUSINESS_DEFAULT_DELIVERY_FEE": 0,
            "ORDERS_AUTO_CANCEL_PENDING_ORDERS_HOURS": 24,
            "ORDERS_ALLOW_ORDER_CANCELLATION_MINUTES": 30,
            "ORDERS_MAX_ORDER_ITEMS": 50,
            "ORDERS_ORDER_NUMBER_PREFIX": "ORD",
            "DELIVERY_DELIVERY_RADIUS_KM": 50,
            "DELIVERY_DELIVERY_SLOTS_ENABLED": True,
            "DELIVERY_SAME_DAY_DELIVERY_CUTOFF_HOUR": 14,
            "DELIVERY_AVG_DELIVERY_TIME_MINUTES": 60,
            # Loyalty defaults removed (loyalty SSOT, Phase 2) — managed via LoyaltyProgram.
            "NOTIFICATIONS_SMS_ENABLED": True,
            "NOTIFICATIONS_EMAIL_ENABLED": True,
            "NOTIFICATIONS_TELEGRAM_ENABLED": True,
            "NOTIFICATIONS_PUSH_ENABLED": False,
            "NOTIFICATIONS_ORDER_CONFIRMATION_SMS": True,
            "NOTIFICATIONS_DELIVERY_REMINDER_SMS": True,
            "PAYMENTS_PAYMENT_METHODS_ENABLED": ["cash", "card", "online"],
            "PAYMENTS_DEFAULT_PAYMENT_METHOD": "cash",
            "PAYMENTS_PAYMENT_TIMEOUT_MINUTES": 15,
            "PAYMENTS_AUTO_REFUND_FAILED_DELIVERIES": True,
            "SECURITY_MAX_LOGIN_ATTEMPTS": 5,
            "SECURITY_LOGIN_LOCKOUT_MINUTES": 30,
            "SECURITY_PASSWORD_MIN_LENGTH": 8,
            "SECURITY_SESSION_TIMEOUT_MINUTES": 1440,
            "SECURITY_REQUIRE_EMAIL_VERIFICATION": False,
            "SECURITY_REQUIRE_PHONE_VERIFICATION": True,
            "API_RATE_LIMIT_ENABLED": True,
            "API_RATE_LIMIT_PER_MINUTE": 60,
            "API_API_VERSION": "v1",
            "API_CORS_ENABLED": True,
            "FILES_MAX_UPLOAD_SIZE_MB": 10,
            "FILES_ALLOWED_IMAGE_EXTENSIONS": ["jpg", "jpeg", "png", "webp"],
            "FILES_ALLOWED_DOCUMENT_EXTENSIONS": ["pdf", "doc", "docx"],
        }

        reset_settings = []

        # Reset specific category or all settings
        if category:
            prefix = f"{category.upper()}_"
            for key, value in defaults.items():
                if key.startswith(prefix):
                    current_app.config[key] = value
                    reset_settings.append(key)
        else:
            # Reset all settings
            for key, value in defaults.items():
                current_app.config[key] = value
                reset_settings.append(key)

        # Log the reset for audit
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="reset_system_settings",
            severity=AuditSeverity.HIGH,
            resource_type="system_settings",
            description=f'System settings reset to defaults: {category if category else "all categories"}',
            success=True,
        )

        return success_response(
            message=f"System settings reset to defaults ({len(reset_settings)} settings)",
            data={"reset_settings": reset_settings},
        )

    except Exception as e:
        current_app.logger.error(f"Reset system settings error: {e}")
        return internal_error_response("Failed to reset system settings")


@admin_bp.route("/audit-logs", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_audit_logs", "super_admin"])
def get_audit_logs():
    """
    Get audit logs with comprehensive filtering

    Query Parameters:
        - page: Page number (default: 1)
        - per_page: Items per page (max: 100, default: 50)
        - event_type: Filter by event type (login_success, order_created, etc.)
        - severity: Filter by severity (low, medium, high, critical)
        - user_id: Filter by user ID
        - resource_type: Filter by resource type (user, order, product, etc.)
        - resource_id: Filter by specific resource ID
        - action: Filter by action name
        - success: Filter by success status (true/false)
        - start_date: Filter logs after this date (ISO format)
        - end_date: Filter logs before this date (ISO format)
        - ip_address: Filter by IP address
        - search: Search in description and action fields
        - sort_by: Sort field (created_at, severity, duration_ms) - default: created_at
        - sort_order: Sort order (asc, desc) - default: desc
    """
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 100)

        # Build query
        query = AuditLog.query

        # Event type filter
        event_type = request.args.get("event_type")
        if event_type:
            try:
                query = query.filter_by(event_type=AuditEventType(event_type))
            except ValueError:
                return validation_error_response(f"Invalid event_type: {event_type}")

        # Severity filter
        severity = request.args.get("severity")
        if severity:
            try:
                query = query.filter_by(severity=AuditSeverity(severity))
            except ValueError:
                return validation_error_response(f"Invalid severity: {severity}")

        # User filter
        user_id = request.args.get("user_id", type=int)
        if user_id:
            query = query.filter_by(user_id=user_id)

        # Resource filters
        resource_type = request.args.get("resource_type")
        if resource_type:
            query = query.filter_by(resource_type=resource_type)

        resource_id = request.args.get("resource_id")
        if resource_id:
            query = query.filter_by(resource_id=resource_id)

        # Action filter
        action = request.args.get("action")
        if action:
            query = query.filter(AuditLog.action.ilike(f"%{action}%"))

        # Success filter
        success = request.args.get("success")
        if success is not None:
            success_bool = success.lower() == "true"
            query = query.filter_by(success=success_bool)

        # Date range filter
        start_date = request.args.get("start_date")
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                query = query.filter(AuditLog.created_at >= start_dt)
            except ValueError:
                return validation_error_response("Invalid start_date format. Use ISO format.")

        end_date = request.args.get("end_date")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                query = query.filter(AuditLog.created_at <= end_dt)
            except ValueError:
                return validation_error_response("Invalid end_date format. Use ISO format.")

        # IP address filter
        ip_address = request.args.get("ip_address")
        if ip_address:
            query = query.filter_by(ip_address=ip_address)

        # Search in description and action
        search = request.args.get("search")
        if search:
            query = query.filter(or_(AuditLog.description.ilike(f"%{search}%"), AuditLog.action.ilike(f"%{search}%")))

        # Sorting
        sort_by = request.args.get("sort_by", "created_at")
        sort_order = request.args.get("sort_order", "desc")

        if sort_by == "severity":
            sort_field = AuditLog.severity
        elif sort_by == "duration_ms":
            sort_field = AuditLog.duration_ms
        else:
            sort_field = AuditLog.created_at

        if sort_order == "asc":
            query = query.order_by(sort_field.asc())
        else:
            query = query.order_by(sort_field.desc())

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize logs
        logs = []
        for log in pagination.items:
            log_data = log.to_dict()

            # Add user info if available
            if log.user_id:
                user = User.query.get(log.user_id)
                if user:
                    log_data["user"] = {"id": user.id, "name": user.name, "email": user.email, "role": user.role}

            logs.append(log_data)

        return paginated_response(items=logs, total=pagination.total, page=page, per_page=per_page)

    except Exception as e:
        current_app.logger.error(f"Get audit logs error: {e}")
        return internal_error_response("Failed to get audit logs")


@admin_bp.route("/audit-logs/<int:log_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_audit_logs", "super_admin"])
def get_audit_log_detail(log_id):
    """Get detailed information about a specific audit log entry"""
    try:
        log = AuditLog.query.get(log_id)

        if not log:
            return not_found_response("Audit log not found")

        log_data = log.to_dict()

        # Add related information
        if log.user_id:
            user = User.query.get(log.user_id)
            if user:
                log_data["user"] = {
                    "id": user.id,
                    "name": user.name,
                    "email": user.email,
                    "phone": user.phone,
                    "role": user.role,
                    "status": user.status,
                }

        # Add related resource information if applicable
        if log.resource_type and log.resource_id:
            if log.resource_type == "order":
                order = Order.query.get(log.resource_id)
                if order:
                    log_data["resource"] = {
                        "type": "order",
                        "id": order.id,
                        "order_number": order.order_number,
                        "status": order.status,
                        "total_amount": float(order.total_amount),
                    }
            elif log.resource_type == "product":
                product = Product.query.get(log.resource_id)
                if product:
                    log_data["resource"] = {
                        "type": "product",
                        "id": product.id,
                        "name": product.name,
                        "is_active": product.is_active,
                    }
            elif log.resource_type == "user":
                user = User.query.get(log.resource_id)
                if user:
                    log_data["resource"] = {
                        "type": "user",
                        "id": user.id,
                        "name": user.name,
                        "email": user.email,
                        "role": user.role,
                    }

        return success_response(data={"audit_log": log_data})

    except Exception as e:
        current_app.logger.error(f"Get audit log detail error: {e}")
        return internal_error_response("Failed to get audit log detail")


@admin_bp.route("/audit-logs/analytics", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_audit_logs", "super_admin"])
def get_audit_log_analytics():
    """
    Get analytics and statistics for audit logs

    Query Parameters:
        - start_date: Start date for analytics (ISO format)
        - end_date: End date for analytics (ISO format)
        - period: Time period (day, week, month) - default: month
    """
    try:
        # Date range
        end_date = request.args.get("end_date")
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        else:
            end_dt = datetime.now(UTC)

        period = request.args.get("period", "month")
        if period == "day":
            start_dt = end_dt - timedelta(days=1)
        elif period == "week":
            start_dt = end_dt - timedelta(days=7)
        else:  # month
            start_dt = end_dt - timedelta(days=30)

        start_date = request.args.get("start_date")
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))

        # Base query for period
        base_query = AuditLog.query.filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt)

        # Total events
        total_events = base_query.count()

        # Events by severity
        severity_breakdown = (
            db.session.query(AuditLog.severity, func.count(AuditLog.id))
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt)
            .group_by(AuditLog.severity)
            .all()
        )

        severity_stats = {severity.value: count for severity, count in severity_breakdown}

        # Events by type
        event_type_breakdown = (
            db.session.query(AuditLog.event_type, func.count(AuditLog.id))
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt)
            .group_by(AuditLog.event_type)
            .order_by(func.count(AuditLog.id).desc())
            .limit(10)
            .all()
        )

        event_type_stats = {event_type.value: count for event_type, count in event_type_breakdown}

        # Success vs failure rate
        success_count = base_query.filter_by(success=True).count()
        failure_count = base_query.filter_by(success=False).count()

        # Top users by activity
        top_users = (
            db.session.query(
                AuditLog.user_id, User.name, User.email, User.role, func.count(AuditLog.id).label("event_count")
            )
            .join(User, AuditLog.user_id == User.id)
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt, AuditLog.user_id.isnot(None))
            .group_by(AuditLog.user_id, User.name, User.email, User.role)
            .order_by(desc("event_count"))
            .limit(10)
            .all()
        )

        top_users_list = [
            {"user_id": user_id, "name": name, "email": email, "role": role, "event_count": event_count}
            for user_id, name, email, role, event_count in top_users
        ]

        # Security events
        security_event_types = [
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
            AuditEventType.LOGIN_FAILURE,
        ]

        security_events = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.event_type.in_(security_event_types),
        ).count()

        # Failed operations by resource type
        failed_operations = (
            db.session.query(AuditLog.resource_type, func.count(AuditLog.id))
            .filter(
                AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt, AuditLog.success == False  # noqa: E712
            )  # noqa: E501,E712
            .group_by(AuditLog.resource_type)
            .all()
        )

        failed_ops_stats = {resource_type: count for resource_type, count in failed_operations if resource_type}

        # Average duration by event type (top 10)
        avg_duration = (
            db.session.query(AuditLog.event_type, func.avg(AuditLog.duration_ms).label("avg_duration"))
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt, AuditLog.duration_ms.isnot(None))
            .group_by(AuditLog.event_type)
            .order_by(desc("avg_duration"))
            .limit(10)
            .all()
        )

        duration_stats = {event_type.value: round(avg_dur, 2) for event_type, avg_dur in avg_duration if avg_dur}

        # Events over time (daily breakdown)
        daily_events = (
            db.session.query(func.date(AuditLog.created_at).label("date"), func.count(AuditLog.id).label("count"))
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt)
            .group_by("date")
            .order_by("date")
            .all()
        )

        timeline = [{"date": date.isoformat() if date else None, "count": count} for date, count in daily_events]

        analytics = {
            "period": {"start_date": start_dt.isoformat(), "end_date": end_dt.isoformat(), "period": period},
            "total_events": total_events,
            "success_rate": round((success_count / total_events * 100), 2) if total_events > 0 else 0,
            "failure_rate": round((failure_count / total_events * 100), 2) if total_events > 0 else 0,
            "severity_breakdown": severity_stats,
            "event_type_breakdown": event_type_stats,
            "security_events": security_events,
            "top_users": top_users_list,
            "failed_operations_by_type": failed_ops_stats,
            "average_duration_by_event_type": duration_stats,
            "timeline": timeline,
        }

        return success_response(data={"analytics": analytics})

    except Exception as e:
        current_app.logger.error(f"Get audit log analytics error: {e}")
        return internal_error_response("Failed to get audit log analytics")


@admin_bp.route("/audit-logs/export", methods=["POST"])
@jwt_required()
@validate_admin_action(["export_data", "super_admin"])
def export_audit_logs():
    """
    Export audit logs to CSV/JSON format

    Request Body:
        - format: Export format (csv, json) - default: csv
        - filters: Same filters as get_audit_logs endpoint
        - start_date: Start date (ISO format)
        - end_date: End date (ISO format)
    """
    try:
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        data = request.get_json() or {}
        export_format = data.get("format", "csv")
        filters = data.get("filters", {})

        # Build query with filters
        query = AuditLog.query

        # Apply filters (similar to get_audit_logs)
        if filters.get("event_type"):
            query = query.filter_by(event_type=AuditEventType(filters["event_type"]))
        if filters.get("severity"):
            query = query.filter_by(severity=AuditSeverity(filters["severity"]))
        if filters.get("user_id"):
            query = query.filter_by(user_id=filters["user_id"])
        if filters.get("start_date"):
            start_dt = datetime.fromisoformat(filters["start_date"].replace("Z", "+00:00"))
            query = query.filter(AuditLog.created_at >= start_dt)
        if filters.get("end_date"):
            end_dt = datetime.fromisoformat(filters["end_date"].replace("Z", "+00:00"))
            query = query.filter(AuditLog.created_at <= end_dt)

        # Limit to prevent excessive exports
        query = query.order_by(AuditLog.created_at.desc()).limit(10000)

        logs = query.all()

        # Log the export action
        get_jwt_identity()
        audit_logger.log_event(
            event_type=AuditEventType.DATA_EXPORT,
            action="export_audit_logs",
            severity=AuditSeverity.HIGH,
            resource_type="audit_logs",
            description=f"Exported {len(logs)} audit logs in {export_format} format",
            additional_data={"format": export_format, "filters": filters, "record_count": len(logs)},
        )

        if export_format == "json":
            export_data = [log.to_dict() for log in logs]
            return success_response(data={"export": export_data, "format": "json", "record_count": len(logs)})
        else:
            # CSV format
            import csv
            from io import StringIO

            output = StringIO()
            fieldnames = [
                "event_id",
                "event_type",
                "severity",
                "user_id",
                "action",
                "resource_type",
                "resource_id",
                "success",
                "ip_address",
                "description",
                "created_at",
            ]

            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            for log in logs:
                writer.writerow(
                    {
                        "event_id": log.event_id,
                        "event_type": log.event_type.value,
                        "severity": log.severity.value,
                        "user_id": log.user_id,
                        "action": log.action,
                        "resource_type": log.resource_type,
                        "resource_id": log.resource_id,
                        "success": log.success,
                        "ip_address": log.ip_address,
                        "description": log.description,
                        "created_at": log.created_at.isoformat() if log.created_at else "",
                    }
                )

            csv_data = output.getvalue()

            return success_response(data={"export": csv_data, "format": "csv", "record_count": len(logs)})

    except Exception as e:
        current_app.logger.error(f"Export audit logs error: {e}")
        return internal_error_response("Failed to export audit logs")


@admin_bp.route("/audit-logs/security-alerts", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_audit_logs", "super_admin"])
def get_security_alerts():
    """
    Get recent security-related audit log entries

    Query Parameters:
        - hours: Number of hours to look back (default: 24)
        - severity: Minimum severity (low, medium, high, critical) - default: medium
    """
    try:
        hours = int(request.args.get("hours", 24))
        min_severity = request.args.get("severity", "medium")

        # Calculate time range
        start_dt = datetime.now(UTC) - timedelta(hours=hours)

        # Security event types
        security_event_types = [
            AuditEventType.LOGIN_FAILURE,
            AuditEventType.PERMISSION_DENIED,
            AuditEventType.SUSPICIOUS_ACTIVITY,
            AuditEventType.EMERGENCY_OPERATION,
            AuditEventType.SENSITIVE_DATA_ACCESS,
        ]

        # Severity filter
        severity_levels = {
            "low": [AuditSeverity.LOW, AuditSeverity.MEDIUM, AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            "medium": [AuditSeverity.MEDIUM, AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            "high": [AuditSeverity.HIGH, AuditSeverity.CRITICAL],
            "critical": [AuditSeverity.CRITICAL],
        }

        allowed_severities = severity_levels.get(min_severity, severity_levels["medium"])

        # Query security events
        alerts = (
            AuditLog.query.filter(
                AuditLog.created_at >= start_dt,
                AuditLog.event_type.in_(security_event_types),
                AuditLog.severity.in_(allowed_severities),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(100)
            .all()
        )

        # Also include all failed operations with high severity
        failed_critical = (
            AuditLog.query.filter(
                AuditLog.created_at >= start_dt,
                AuditLog.success == False,  # noqa: E712
                AuditLog.severity.in_([AuditSeverity.HIGH, AuditSeverity.CRITICAL]),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(100)
            .all()
        )

        # Combine and deduplicate
        all_alerts = {alert.id: alert for alert in alerts + failed_critical}

        alerts_data = []
        for alert in sorted(all_alerts.values(), key=lambda x: x.created_at, reverse=True):
            alert_data = alert.to_dict()

            # Add user info
            if alert.user_id:
                user = User.query.get(alert.user_id)
                if user:
                    alert_data["user"] = {"id": user.id, "name": user.name, "email": user.email}

            alerts_data.append(alert_data)

        # Summary statistics
        summary = {
            "total_alerts": len(alerts_data),
            "critical_count": sum(1 for a in alerts_data if a["severity"] == "critical"),
            "high_count": sum(1 for a in alerts_data if a["severity"] == "high"),
            "time_range_hours": hours,
            "most_recent": alerts_data[0]["created_at"] if alerts_data else None,
        }

        return success_response(data={"alerts": alerts_data[:50], "summary": summary})  # Limit to 50 most recent

    except Exception as e:
        current_app.logger.error(f"Get security alerts error: {e}")
        return internal_error_response("Failed to get security alerts")


@admin_bp.route("/send-announcement", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per="user")  # 3 announcements per hour per user
@manager_or_higher_required
@validate_json(["title", "message"])
def send_announcement():
    """Send announcement to users"""
    try:
        get_jwt_identity()
        data = request.get_json()

        data.get("title")
        data.get("message")
        data.get("target_users", "all")  # all, active, segment_id
        data.get("channels", ["email", "push"])

        # Send announcement asynchronously (placeholder until task is implemented)
        # task = send_bulk_email_task.delay(
        #     subject=title,
        #     message=message,
        #     target_users=target_users,
        #     channels=channels,
        #     sender_id=current_user_id
        # )
        task_id = "placeholder_announcement_task"

        return success_response(data={"task_id": task_id}, message="Announcement queued for sending")

    except Exception as e:
        current_app.logger.error(f"Send announcement error: {e}")
        return internal_error_response("Failed to send announcement")


@admin_bp.route("/inventory/<int:product_id>/status", methods=["GET"])
@jwt_required()
@validate_admin_action(["manage_products", "view_products"])
def get_inventory_status(product_id):
    """Get detailed inventory status for a product"""
    try:
        inventory_status = get_inventory_service().get_inventory_status(product_id)
        return success_response(data=inventory_status)

    except Exception as e:
        current_app.logger.error(f"Get inventory status error: {e}")
        return internal_error_response("Failed to get inventory status")


@admin_bp.route("/inventory/<int:product_id>/adjust", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_products"])
@validate_json(["quantity_change", "operation_type", "reason"])
def adjust_inventory(product_id):
    """Manually adjust inventory levels"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        quantity_change = int(data["quantity_change"])
        operation_type_str = data["operation_type"]
        reason = data["reason"]

        # Validate operation type
        try:
            operation_type = InventoryOperationType(operation_type_str)
        except ValueError:
            return validation_error_response(
                f"Invalid operation type. Must be one of: {[op.value for op in InventoryOperationType]}"
            )

        # Validate quantity change
        if quantity_change == 0:
            return validation_error_response("Quantity change cannot be zero")

        if abs(quantity_change) > 10000:
            return validation_error_response("Quantity change too large (max 10000)")

        # Perform adjustment
        result = get_inventory_service().adjust_inventory(
            product_id=product_id,
            quantity_change=quantity_change,
            operation_type=operation_type,
            reason=reason,
            user_id=current_user_id,
        )

        if result["success"]:
            return success_response(data=result, message="Inventory adjusted successfully")
        else:
            return error_response(message=result.get("reason", "Adjustment failed"), status_code=400)

    except Exception as e:
        current_app.logger.error(f"Adjust inventory error: {e}")
        return internal_error_response("Failed to adjust inventory")


@admin_bp.route("/inventory/check-availability", methods=["POST"])
@jwt_required()
@validate_admin_action(["view_products", "manage_products"])
@validate_json(["items"])
def check_inventory_availability():
    """Check inventory availability for multiple products"""
    try:
        data = request.get_json()
        items = data["items"]

        # Validate items structure
        for item in items:
            if "product_id" not in item or "quantity" not in item:
                return validation_error_response("Each item must have product_id and quantity")

        # Check availability
        availability_results = get_inventory_service().check_multiple_products_availability(items)

        # Format response
        language = get_current_language()
        results = []
        for result in availability_results:
            product = Product.query.get(result.product_id)
            product_name = product.get_translated("name", language) if product else "Unknown"
            results.append(
                {
                    "product_id": result.product_id,
                    "product_name": product_name,
                    "requested_quantity": result.requested_quantity,
                    "available_quantity": result.available_quantity,
                    "reserved_quantity": result.reserved_quantity,
                    "is_available": result.is_available,
                    "reason": result.reason,
                }
            )

        return success_response(
            data={"results": results, "all_available": all(r.is_available for r in availability_results)}
        )

    except Exception as e:
        current_app.logger.error(f"Check inventory availability error: {e}")
        return internal_error_response("Failed to check inventory availability")


@admin_bp.route("/inventory/reservations/<int:order_id>", methods=["GET"])
@jwt_required()
@validate_admin_action(["view_orders", "manage_orders"])
def get_order_reservations(order_id):
    """Get inventory reservations for an order"""
    try:
        # This would require extending the inventory service to get reservations by order
        # For now, return basic order information
        order = Order.query.get(order_id)
        if not order:
            return not_found_response(resource_type="Order")

        # Get inventory status for each item in the order
        language = get_current_language()
        reservations = []
        for item in order.order_items:
            inventory_status = get_inventory_service().get_inventory_status(item.product_id)
            product_name = item.product.get_translated("name", language) if item.product else "Unknown"
            reservations.append(
                {
                    "product_id": item.product_id,
                    "product_name": product_name,
                    "quantity": item.quantity,
                    "current_stock": inventory_status["current_stock"],
                    "available_quantity": inventory_status["available_quantity"],
                    "reserved_quantity": inventory_status["reserved_quantity"],
                }
            )

        return success_response(
            data={"order_id": order_id, "order_status": order.status.value, "reservations": reservations}
        )

    except Exception as e:
        current_app.logger.error(f"Get order reservations error: {e}")
        return internal_error_response("Failed to get order reservations")


@admin_bp.route("/inventory/reservations/<int:order_id>/release", methods=["POST"])
@jwt_required()
@validate_admin_action(["manage_orders"])
def release_order_reservations(order_id):
    """Manually release inventory reservations for an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}
        reason = data.get("reason", "Manual release by admin")

        # Check if order exists
        order = Order.query.get(order_id)
        if not order:
            return not_found_response(resource_type="Order")

        # Release reservations
        result = get_inventory_service().release_reservations(order_id)

        if result["success"]:
            # Log the manual release
            from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

            audit_logger.log_event(
                event_type=AuditEventType.INVENTORY_UPDATED,
                action="inventory_reservations_manually_released",
                severity=AuditSeverity.HIGH,
                resource_type="order_inventory",
                resource_id=str(order_id),
                description=f"Admin manually released inventory reservations for order {order_id}",
                additional_data={"order_id": order_id, "released_by_user_id": current_user_id, "reason": reason},
            )

            return success_response(data=result, message="Reservations released successfully")
        else:
            return error_response(message=result.get("reason", "Failed to release reservations"), status_code=400)

    except Exception as e:
        current_app.logger.error(f"Release order reservations error: {e}")
        return internal_error_response("Failed to release reservations")


@admin_bp.route("/backup", methods=["POST"])
@jwt_required()
@rate_limit(max_requests=2, window_seconds=3600, per="user")  # 2 backups per hour per user
@super_admin_required
def create_backup():
    """Create system backup"""
    try:
        get_jwt_identity()
        data = request.get_json() or {}

        data.get("type", "full")  # full, incremental
        data.get("include_files", True)

        # Create backup asynchronously (placeholder until service is implemented)
        # backup_result = admin_service.create_backup(
        #     backup_type=backup_type,
        #     include_files=include_files,
        #     requested_by=current_user_id
        # )
        backup_result = {"backup_id": "placeholder_backup_id"}

        return success_response(data={"backup_id": backup_result["backup_id"]}, message="Backup creation started")

    except Exception as e:
        current_app.logger.error(f"Create backup error: {e}")
        return internal_error_response("Failed to create backup")


# =============================================================================
# TRANSLATION MANAGEMENT ROUTES
# =============================================================================


def parse_entity_key(key):
    """Parse entity key format: EntityType.field.ID"""
    parts = key.split(".")
    if len(parts) == 3:
        try:
            entity_type, field_name, entity_id = parts
            return entity_type, field_name, int(entity_id)
        except ValueError:
            return None, None, None
    return None, None, None


def format_entity_translation(translation):
    """Convert Translation record to entity translation format for API compatibility"""
    entity_type, field_name, entity_id = parse_entity_key(translation.key)

    if entity_type and field_name and entity_id:
        return {
            "id": translation.id,
            "key": translation.key,
            "value": translation.value,
            "category": translation.category,
            "description": translation.description,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field_name": field_name,
            "language": translation.language,
            "content": translation.value,
            "is_active": translation.is_active,
            "version": 1,  # For compatibility
            "created_at": translation.created_at.isoformat() if translation.created_at else None,
            "updated_at": translation.updated_at.isoformat() if translation.updated_at else None,
        }
    return None


@admin_bp.route("/translations", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_translations():
    """Get all translations (both static and entity) with filtering and pagination"""
    try:
        # Get query parameters
        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 50, type=int), 100)
        category = request.args.get("category")
        entity_type = request.args.get("entity_type")
        entity_id = request.args.get("entity_id", type=int)
        field_name = request.args.get("field_name")
        language = request.args.get("language")
        search = request.args.get("search")
        translation_type = request.args.get("type")  # 'static' or 'entity'

        # Build base query
        query = Translation.query

        # Filter by translation type
        if translation_type == "static":
            # Static translations have category NOT starting with 'entity_'
            query = query.filter(~Translation.category.like("entity_%"))
        elif translation_type == "entity" or entity_type or entity_id or field_name:
            # Entity translations have category starting with 'entity_'
            query = query.filter(Translation.category.like("entity_%"))

        if category:
            query = query.filter(Translation.category == category)

        # Apply filters for entity translations
        if entity_type:
            query = query.filter(Translation.category == f"entity_{entity_type.lower()}")
        if entity_id:
            query = query.filter(Translation.key.like(f"%.%.{entity_id}"))
        if field_name:
            query = query.filter(Translation.key.like(f"%.{field_name}.%"))
        if language:
            query = query.filter(Translation.language == language)
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    Translation.key.ilike(search_term),
                    Translation.value.ilike(search_term),
                    Translation.category.ilike(search_term),
                    Translation.description.ilike(search_term),
                )
            )

        # Order by key and language for consistency
        query = query.order_by(Translation.key, Translation.language)

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Format results based on category (already filtered by query)
        translations = []
        for item in pagination.items:
            # Check if it's an entity translation based on category
            if item.category and item.category.startswith("entity_"):
                # Entity translation - format with entity structure
                entity_trans = format_entity_translation(item)
                if entity_trans:
                    translations.append(entity_trans)
            else:
                # Static translation - format with standard structure
                translations.append(
                    {
                        "id": item.id,
                        "key": item.key,
                        "language": item.language,
                        "value": item.value,
                        "category": item.category,
                        "description": item.description,
                        "is_active": item.is_active,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                    }
                )

        # Get statistics - count individual translation records (each key-language pair is one record)
        # Entity translations have category starting with 'entity_'
        total_translation_records = Translation.query.count()
        entity_translation_records = Translation.query.filter(Translation.category.like("entity_%")).count()
        static_translation_records = total_translation_records - entity_translation_records

        # Count unique translatable items (not individual records)
        unique_entity_items = (
            db.session.query(Translation.key).filter(Translation.category.like("entity_%")).distinct().count()
        )

        unique_static_keys = (
            db.session.query(Translation.key).filter(~Translation.category.like("entity_%")).distinct().count()
        )

        # Language breakdown
        language_stats = (
            db.session.query(Translation.language, func.count(Translation.id).label("count"))
            .group_by(Translation.language)
            .all()
        )

        return success_response(
            data={
                "translations": translations,
                "statistics": {
                    "total_records": total_translation_records,
                    "entity_records": entity_translation_records,
                    "static_records": static_translation_records,
                    "unique_entity_items": unique_entity_items,
                    "unique_static_keys": unique_static_keys,
                    "total_unique_items": unique_entity_items + unique_static_keys,
                    "language_stats": [{"language": lang, "count": count} for lang, count in language_stats],
                    "description": "Records = individual key-language pairs, Items = unique translatable content",
                },
            },
            meta={
                "page": page,
                "pages": pagination.pages,
                "per_page": per_page,
                "total": pagination.total,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            },
        )

    except Exception as e:
        current_app.logger.error(f"Error getting translations: {e}")
        return internal_error_response("Failed to get translations")


@admin_bp.route("/translations/<int:translation_id>", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_translation_by_id(translation_id):
    """Get a specific translation by ID"""
    try:
        translation = Translation.query.get_or_404(translation_id)

        # Check if it's an entity translation
        if "." in translation.key and len(translation.key.split(".")) == 3:
            result = format_entity_translation(translation)
            if result:
                return success_response(data={"translation": result})

        # Static translation
        return success_response(
            data={
                "translation": {
                    "id": translation.id,
                    "key": translation.key,
                    "language": translation.language,
                    "value": translation.value,
                    "category": translation.category,
                    "description": translation.description,
                    "is_active": translation.is_active,
                    "created_at": translation.created_at.isoformat() if translation.created_at else None,
                    "updated_at": translation.updated_at.isoformat() if translation.updated_at else None,
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Error getting translation: {e}")
        return not_found_response("Translation not found")


@admin_bp.route("/translations", methods=["POST"])
@jwt_required()
@manager_or_higher_required
@validate_json()
def create_translation():
    """Create a new translation (static or entity)"""
    try:
        data = request.get_json()

        # Determine if it's an entity translation or static translation
        if all(field in data for field in ["entity_type", "entity_id", "field_name"]):
            # Entity translation
            entity_type = data["entity_type"]
            entity_id = data["entity_id"]
            field_name = data["field_name"]
            language = data["language"]
            content = data["content"]

            # Use unified Translation model
            success = Translation.set_entity_translation(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                language=language,
                value=content,
                user_id=get_jwt_identity(),
            )

            if success:
                db.session.commit()

                # Trigger bot translation reload if telegram category
                if data.get("category") == "telegram":
                    trigger_translation_reload()

                return created_response(message="Entity translation created successfully")
            else:
                return internal_error_response("Failed to create entity translation")

        elif all(field in data for field in ["key", "language", "value"]):
            # Static translation
            existing = Translation.query.filter_by(key=data["key"], language=data["language"]).first()

            if existing:
                return validation_error_response("Translation already exists")

            translation = Translation(
                key=data["key"],
                language=data["language"],
                value=data["value"],
                category=data.get("category", "general"),
                description=data.get("description"),
                is_active=True,
                created_by=get_jwt_identity(),
                updated_by=get_jwt_identity(),
            )

            db.session.add(translation)
        else:
            return validation_error_response("Invalid translation data format")

        db.session.commit()

        # Trigger bot translation reload if telegram category
        if data.get("category") == "telegram":
            trigger_translation_reload()

        return created_response(message="Translation created successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create translation error: {e}")
        return internal_error_response("Failed to create translation")


@admin_bp.route("/translations/<int:translation_id>", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
@validate_json()
def update_translation(translation_id):
    """Update an existing translation"""
    try:
        translation = Translation.query.get_or_404(translation_id)
        data = request.get_json()

        # Update fields
        if "content" in data:
            translation.value = data["content"]
        if "value" in data:
            translation.value = data["value"]
        if "category" in data:
            translation.category = data["category"]
        if "description" in data:
            translation.description = data["description"]
        if "is_active" in data:
            translation.is_active = data["is_active"]

        translation.updated_by = get_jwt_identity()
        translation.updated_at = datetime.now(UTC)

        db.session.commit()

        # Trigger bot translation reload if telegram category
        if translation.category == "telegram":
            trigger_translation_reload()

        return success_response(message=get_translation("api.admin.success.translation_updated"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update translation error: {e}")
        return internal_error_response("Failed to update translation")


@admin_bp.route("/translations/<int:translation_id>", methods=["DELETE"])
@jwt_required()
@manager_or_higher_required
def delete_translation(translation_id):
    """Delete a translation"""
    try:
        translation = Translation.query.get_or_404(translation_id)

        # Check if it's a telegram translation before deletion
        is_telegram = translation.category == "telegram"

        db.session.delete(translation)
        db.session.commit()

        # Trigger bot translation reload if telegram category
        if is_telegram:
            trigger_translation_reload()

        return success_response(message=get_translation("api.admin.success.translation_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Delete translation error: {e}")
        return internal_error_response("Failed to delete translation")


@admin_bp.route("/translations/entities", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_translatable_entities():
    """Get all translatable entities with their available fields"""
    try:
        # Get distinct entity categories (like entity_product, entity_subscription)
        entity_categories = (
            db.session.query(Translation.category).filter(Translation.category.like("entity_%")).distinct().all()
        )

        entities = []
        for (category,) in entity_categories:
            # Extract entity type from category (entity_product -> Product)
            entity_type = category.replace("entity_", "").title()

            # Get translations for this entity category to parse fields and count entities
            translations = db.session.query(Translation.key).filter_by(category=category).distinct().all()

            available_fields = set()
            entity_ids = set()

            for (key,) in translations:
                # Parse key format: EntityType.field.ID (e.g., Product.name.123)
                key_parts = key.split(".")
                if len(key_parts) == 3:
                    parsed_entity_type, field_name, entity_id = key_parts
                    available_fields.add(field_name)
                    try:
                        entity_ids.add(int(entity_id))
                    except ValueError:
                        continue

            entities.append(
                {
                    "entity_type": entity_type,
                    "available_fields": list(available_fields),
                    "entity_count": len(entity_ids),
                }
            )

        return success_response(data={"entities": entities})

    except Exception as e:
        current_app.logger.error(f"Get translatable entities error: {e}")
        return internal_error_response("Failed to fetch translatable entities")


@admin_bp.route("/translations/sync/<entity_type>", methods=["POST"])
@jwt_required()
@manager_or_higher_required
@validate_json()
def sync_entity_translations(entity_type):
    """Sync translations for all entities of a specific type"""
    try:
        data = request.get_json()
        entity_ids = data.get("entity_ids", [])  # Empty list means sync all

        # Map entity types to model classes
        entity_models = {
            "Product": Product,
            "ProductCategory": ProductCategory,
            "SubscriptionPlan": None,  # Will need to import if needed
            "LoyaltyReward": LoyaltyReward,
            "NotificationTemplate": NotificationTemplate,
        }

        if entity_type not in entity_models:
            return validation_error_response(f"Unsupported entity type: {entity_type}")

        model_class = entity_models[entity_type]
        if not model_class:
            return validation_error_response(f"Model not available for entity type: {entity_type}")

        # Get entities to sync
        query = model_class.query
        if entity_ids:
            query = query.filter(model_class.id.in_(entity_ids))

        entities = query.all()
        synced_count = 0

        for entity in entities:
            # Get translatable fields for this entity
            translatable_fields = getattr(entity, "_translatable_fields", [])

            if translatable_fields:
                # Sync translations (create baseline if not exists)
                translations = {}
                for field in translatable_fields:
                    field_value = getattr(entity, field, None)
                    if field_value:
                        translations.setdefault(field, {})["uz"] = field_value

                if translations and hasattr(entity, "set_translations"):
                    entity.set_translations(translations)
                    synced_count += 1

        db.session.commit()

        return success_response(message=f"Synced translations for {synced_count} {entity_type} entities")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Sync translations error: {e}")
        return internal_error_response("Failed to sync translations")


@admin_bp.route("/translations/export", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def export_translations():
    """Export translations in various formats (CSV, JSON)"""
    try:
        import csv
        import io
        import json

        format_type = request.args.get("format", "json").lower()
        category = request.args.get("category")
        entity_type = request.args.get("entity_type")
        language = request.args.get("language")
        search = request.args.get("search")
        translation_type = request.args.get("type")

        query = Translation.query

        if translation_type == "static":
            query = query.filter(~Translation.category.like("entity_%"))
        elif translation_type == "entity":
            query = query.filter(Translation.category.like("entity_%"))

        if entity_type:
            query = query.filter(Translation.category == f"entity_{entity_type.lower()}")
        if category:
            query = query.filter(Translation.category == category)
        if language:
            query = query.filter(Translation.language == language)
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    Translation.key.ilike(search_term),
                    Translation.value.ilike(search_term),
                    Translation.category.ilike(search_term),
                    Translation.description.ilike(search_term),
                )
            )

        translations = query.order_by(Translation.key, Translation.language).all()

        if format_type == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    "id",
                    "key",
                    "language",
                    "value",
                    "category",
                    "description",
                    "is_active",
                    "entity_type",
                    "entity_id",
                    "field_name",
                ]
            )

            for translation in translations:
                parsed_entity_type, parsed_field_name, parsed_entity_id = parse_entity_key(translation.key)
                writer.writerow(
                    [
                        translation.id,
                        translation.key,
                        translation.language,
                        translation.value,
                        translation.category,
                        translation.description,
                        translation.is_active,
                        parsed_entity_type,
                        parsed_entity_id,
                        parsed_field_name,
                    ]
                )

            response = current_app.response_class(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment;filename=translations.csv"},
            )
            return response

        exported_rows = []
        for translation in translations:
            parsed_entity_type, parsed_field_name, parsed_entity_id = parse_entity_key(translation.key)
            exported_rows.append(
                {
                    "id": translation.id,
                    "key": translation.key,
                    "language": translation.language,
                    "value": translation.value,
                    "content": translation.value,  # Backward compatible alias
                    "category": translation.category,
                    "description": translation.description,
                    "is_active": translation.is_active,
                    "entity_type": parsed_entity_type,
                    "entity_id": parsed_entity_id,
                    "field_name": parsed_field_name,
                }
            )

        response = current_app.response_class(
            json.dumps({"translations": exported_rows, "count": len(exported_rows)}, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=translations.json"},
        )
        return response

    except Exception as e:
        current_app.logger.error(f"Export translations error: {e}")
        return internal_error_response("Failed to export translations")


@admin_bp.route("/translations/import", methods=["POST"])
@jwt_required()
@admin_required  # Require admin for imports
@validate_json()
def import_translations():
    """Import translations from uploaded data"""
    try:
        data = request.get_json()
        translations_data = data.get("translations", [])
        update_existing = data.get("update_existing", False)

        if not translations_data:
            return validation_error_response("No translations data provided")

        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []
        touched_telegram = False

        for item in translations_data:
            try:
                raw_language = item.get("language")
                if not raw_language:
                    errors.append(f"Missing required field 'language' in item: {item}")
                    continue

                key = item.get("key")
                value = item.get("value", item.get("content"))
                category = item.get("category")

                if not key:
                    entity_type_value = item.get("entity_type")
                    entity_id_value = item.get("entity_id")
                    field_name_value = item.get("field_name")
                    if entity_type_value and entity_id_value is not None and field_name_value:
                        key = f"{entity_type_value}.{field_name_value}.{entity_id_value}"
                        category = category or f"entity_{str(entity_type_value).lower()}"

                if not key:
                    errors.append(f"Missing required key/entity fields in item: {item}")
                    continue

                if value is None:
                    errors.append(f"Missing required value/content in item: {item}")
                    continue

                existing = Translation.query.filter_by(key=key, language=raw_language).first()

                if existing:
                    if update_existing:
                        existing.value = value
                        if category:
                            existing.category = category
                        if "description" in item:
                            existing.description = item.get("description")
                        if "is_active" in item:
                            existing.is_active = item["is_active"]
                        existing.updated_by = get_jwt_identity()
                        existing.updated_at = datetime.now(UTC)
                        updated_count += 1
                        if existing.category == "telegram":
                            touched_telegram = True
                    else:
                        skipped_count += 1
                else:
                    translation = Translation(
                        key=key,
                        language=raw_language,
                        value=value,
                        category=category or "general",
                        description=item.get("description"),
                        is_active=item.get("is_active", True),
                        created_by=get_jwt_identity(),
                        updated_by=get_jwt_identity(),
                    )
                    db.session.add(translation)
                    created_count += 1
                    if translation.category == "telegram":
                        touched_telegram = True

            except Exception as e:
                errors.append(f"Error processing item {item}: {e}")

        db.session.commit()

        if touched_telegram:
            trigger_translation_reload()

        return success_response(
            data={
                "results": {
                    "created": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "errors": len(errors),
                },
                "errors": errors[:10],  # Return first 10 errors
            },
            message="Import completed",
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Import translations error: {e}")
        return internal_error_response("Failed to import translations")


@admin_bp.route("/translations/completion", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_translation_completion():
    """Get translation completion statistics for both entity and static translations"""
    try:
        # Get all languages from config
        languages = ["en", "uz", "ru"]  # From config
        entity_type_filter = request.args.get("entity_type")
        category_filter = request.args.get("category")

        include_entity_completion = not category_filter or category_filter.startswith("entity_")
        include_static_completion = not category_filter or not category_filter.startswith("entity_")
        static_category_filter = None

        if category_filter:
            if category_filter.startswith("entity_"):
                if not entity_type_filter:
                    entity_type_filter = category_filter.replace("entity_", "")
            else:
                static_category_filter = category_filter

        # ========== ENTITY TRANSLATIONS ==========
        # Get all entity translations (category starts with 'entity_')
        entity_translations_query = Translation.query.filter(Translation.category.like("entity_%"))

        if not include_entity_completion:
            entity_translations_query = entity_translations_query.filter(text("1=0"))

        if entity_type_filter:
            entity_translations_query = entity_translations_query.filter(
                Translation.category == f"entity_{entity_type_filter.lower()}"
            )

        all_entity_translations = entity_translations_query.all()

        # Parse unique entity/field combinations from keys (format: EntityType.field.ID)
        unique_entity_combinations = set()
        for trans in all_entity_translations:
            key_parts = trans.key.split(".")
            if len(key_parts) == 3:
                entity_type_name, field_name, entity_id = key_parts
                unique_entity_combinations.add((trans.category, entity_id, field_name))

        unique_entity_fields = list(unique_entity_combinations)

        # ========== STATIC TRANSLATIONS ==========
        # Get all static translations (category does NOT start with 'entity_')
        static_translations_query = Translation.query.filter(~Translation.category.like("entity_%"))
        if static_category_filter:
            static_translations_query = static_translations_query.filter(Translation.category == static_category_filter)
        all_static_translations = static_translations_query.all()

        # Parse unique static keys (each unique key should have translations for all languages)
        unique_static_keys = set()
        for trans in all_static_translations:
            unique_static_keys.add((trans.category, trans.key))

        unique_static_keys = list(unique_static_keys)

        # ========== OVERALL STATS INITIALIZATION ==========
        completion_stats = []

        # Separate entity and static totals for clarity
        total_entity_fields = len(unique_entity_fields)
        total_static_keys = len(unique_static_keys)
        total_translatable_items = total_entity_fields + total_static_keys

        overall_stats = {
            "total_translatable_items": total_translatable_items,
            "entity_translatable_fields": total_entity_fields,
            "static_translatable_keys": total_static_keys,
            "total_possible_translations": total_translatable_items * len(languages),
            "entity_possible_translations": total_entity_fields * len(languages),
            "static_possible_translations": total_static_keys * len(languages),
            "total_actual_translations": 0,
            "entity_actual_translations": 0,
            "static_actual_translations": 0,
            "overall_completion_percentage": 0.0,
            "language_breakdown": {},
        }

        for lang in languages:
            overall_stats["language_breakdown"][lang] = {
                "translated": 0,
                "entity_translated": 0,
                "static_translated": 0,
                "total": total_translatable_items,
                "percentage": 0.0,
            }

        # ========== ENTITY TRANSLATION COMPLETION BY CATEGORY ==========
        entity_categories = []
        if include_entity_completion:
            entity_categories_query = db.session.query(Translation.category).filter(
                Translation.category.like("entity_%")
            )
            if entity_type_filter:
                entity_categories_query = entity_categories_query.filter(
                    Translation.category == f"entity_{entity_type_filter.lower()}"
                )
            entity_categories = entity_categories_query.distinct().all()

        for (category_name,) in entity_categories:
            # Get all fields for this entity category
            entity_fields = [uf for uf in unique_entity_fields if uf[0] == category_name]

            # Count translations per language
            lang_stats = {}
            for lang in languages:
                translated_count = Translation.query.filter(
                    Translation.category == category_name,
                    Translation.language == lang,
                    Translation.is_active == True,  # noqa: E501,E712
                ).count()

                lang_stats[lang] = {
                    "translated": translated_count,
                    "total": len(entity_fields),
                    "percentage": round((translated_count / len(entity_fields) * 100) if entity_fields else 0, 2),
                }

                # Add to overall stats
                overall_stats["language_breakdown"][lang]["translated"] += translated_count
                overall_stats["language_breakdown"][lang]["entity_translated"] += translated_count
                overall_stats["total_actual_translations"] += translated_count
                overall_stats["entity_actual_translations"] += translated_count

            # Calculate overall completion for this entity category
            total_possible = len(entity_fields) * len(languages)
            total_actual = sum(lang_stats[lang]["translated"] for lang in languages)
            completion_percentage = round((total_actual / total_possible * 100) if total_possible else 0, 2)

            completion_stats.append(
                {
                    "type": "entity",
                    "category": category_name,
                    "display_name": category_name.replace("entity_", "").title(),
                    "total_fields": len(entity_fields),
                    "total_possible_translations": total_possible,
                    "total_actual_translations": total_actual,
                    "completion_percentage": completion_percentage,
                    "language_breakdown": lang_stats,
                    "missing_translations": total_possible - total_actual,
                }
            )

        # ========== STATIC TRANSLATION COMPLETION BY CATEGORY ==========
        static_categories_query = db.session.query(Translation.category).filter(~Translation.category.like("entity_%"))
        if static_category_filter:
            static_categories_query = static_categories_query.filter(Translation.category == static_category_filter)
        static_categories = static_categories_query.distinct().all() if include_static_completion else []

        for (category_name,) in static_categories:
            # Get all keys for this static category
            static_keys = [sk for sk in unique_static_keys if sk[0] == category_name]

            # Count translations per language
            lang_stats = {}
            for lang in languages:
                translated_count = Translation.query.filter(
                    Translation.category == category_name,
                    Translation.language == lang,
                    Translation.is_active == True,  # noqa: E501,E712
                ).count()

                lang_stats[lang] = {
                    "translated": translated_count,
                    "total": len(static_keys),
                    "percentage": round((translated_count / len(static_keys) * 100) if static_keys else 0, 2),
                }

                # Add to overall stats
                overall_stats["language_breakdown"][lang]["translated"] += translated_count
                overall_stats["language_breakdown"][lang]["static_translated"] += translated_count
                overall_stats["total_actual_translations"] += translated_count
                overall_stats["static_actual_translations"] += translated_count

            # Calculate overall completion for this static category
            total_possible = len(static_keys) * len(languages)
            total_actual = sum(lang_stats[lang]["translated"] for lang in languages)
            completion_percentage = round((total_actual / total_possible * 100) if total_possible else 0, 2)

            completion_stats.append(
                {
                    "type": "static",
                    "category": category_name,
                    "display_name": category_name.title(),
                    "total_keys": len(static_keys),
                    "total_possible_translations": total_possible,
                    "total_actual_translations": total_actual,
                    "completion_percentage": completion_percentage,
                    "language_breakdown": lang_stats,
                    "missing_translations": total_possible - total_actual,
                }
            )

        # ========== CALCULATE OVERALL PERCENTAGES ==========
        if overall_stats["total_possible_translations"] > 0:
            overall_stats["overall_completion_percentage"] = round(
                (overall_stats["total_actual_translations"] / overall_stats["total_possible_translations"] * 100), 2
            )

        for lang in languages:
            if overall_stats["language_breakdown"][lang]["total"] > 0:
                overall_stats["language_breakdown"][lang]["percentage"] = round(
                    (
                        overall_stats["language_breakdown"][lang]["translated"]
                        / overall_stats["language_breakdown"][lang]["total"]
                        * 100
                    ),
                    2,
                )

        return success_response(data={"completion_stats": completion_stats, "overall_stats": overall_stats})

    except Exception as e:
        current_app.logger.error(f"Get translation completion error: {e}")
        return internal_error_response("Failed to get translation completion stats")


@admin_bp.route("/translations/missing", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_missing_translations():
    """Get list of missing translations for both entity and static translations"""
    try:
        languages = ["en", "uz", "ru"]
        entity_type = request.args.get("entity_type")
        category_filter = request.args.get("category")
        language = request.args.get("language")
        translation_type = request.args.get("type")  # 'entity', 'static', or None for all
        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 50, type=int), 100)

        missing_translations = []

        include_entity = not translation_type or translation_type == "entity"
        include_static = not translation_type or translation_type == "static"

        # Category filter can force entity/static scope and derive entity_type.
        if category_filter:
            if category_filter.startswith("entity_"):
                include_static = False
                entity_type = category_filter.replace("entity_", "")
            else:
                include_entity = False

        # ========== CHECK MISSING ENTITY TRANSLATIONS ==========
        if include_entity:
            # Get all entity translations (category starts with 'entity_')
            entity_translations_query = Translation.query.filter(Translation.category.like("entity_%"))

            if entity_type:
                entity_translations_query = entity_translations_query.filter(
                    Translation.category == f"entity_{entity_type.lower()}"
                )

            all_entity_translations = entity_translations_query.all()

            # Parse unique entity/field combinations from keys
            unique_entity_combinations = set()
            for trans in all_entity_translations:
                key_parts = trans.key.split(".")
                if len(key_parts) == 3:
                    entity_type_name, field_name, entity_id = key_parts
                    unique_entity_combinations.add((trans.category, entity_id, field_name))

            unique_entity_combinations = list(unique_entity_combinations)

            for entity_type_val, entity_id, field_name in unique_entity_combinations:
                check_languages = [language] if language else languages

                for lang in check_languages:
                    # Construct the expected key format: EntityType.field.ID
                    # Extract entity type name from category (entity_product -> Product)
                    category = entity_type_val
                    expected_key = None
                    existing = None

                    if category.startswith("entity_"):
                        entity_type_name = category.replace("entity_", "").title()
                        expected_key = f"{entity_type_name}.{field_name}.{entity_id}"

                        # Check if translation exists
                        existing = Translation.query.filter_by(key=expected_key, language=lang, is_active=True).first()

                    if not existing and expected_key:
                        missing_translations.append(
                            {
                                "type": "entity",
                                "category": entity_type_val,
                                "entity_type": category.replace("entity_", ""),
                                "entity_id": entity_id,
                                "field_name": field_name,
                                "key": expected_key,
                                "language": lang,
                                "priority": (
                                    "high" if lang == "uz" else "medium"
                                ),  # Uzbek (default) translations higher priority
                            }
                        )

        # ========== CHECK MISSING STATIC TRANSLATIONS ==========
        if include_static:
            # Get all static translations (category does NOT start with 'entity_')
            static_translations_query = Translation.query.filter(~Translation.category.like("entity_%"))
            if category_filter and not category_filter.startswith("entity_"):
                static_translations_query = static_translations_query.filter(Translation.category == category_filter)
            all_static_translations = static_translations_query.all()

            # Parse unique static keys (each unique key should have translations for all languages)
            unique_static_keys = {}  # key -> category mapping
            for trans in all_static_translations:
                if trans.key not in unique_static_keys:
                    unique_static_keys[trans.key] = trans.category

            # Check each unique static key for all languages
            for static_key, category in unique_static_keys.items():
                check_languages = [language] if language else languages

                for lang in check_languages:
                    # Check if translation exists for this language
                    existing = Translation.query.filter_by(key=static_key, language=lang, is_active=True).first()

                    if not existing:
                        missing_translations.append(
                            {
                                "type": "static",
                                "category": category,
                                "key": static_key,
                                "language": lang,
                                "priority": (
                                    "high" if lang == "uz" else "medium"
                                ),  # Uzbek (default) translations higher priority
                            }
                        )

        # Sort by priority, type, and category
        missing_translations.sort(
            key=lambda x: (
                x["priority"] == "medium",  # high priority first
                x["type"],  # entity before static
                x["category"],
                x.get("entity_id", ""),
                x["language"],
            )
        )

        # Manual pagination
        start = (page - 1) * per_page
        end = start + per_page
        paginated_missing = missing_translations[start:end]

        total_pages = (len(missing_translations) + per_page - 1) // per_page if missing_translations else 1

        # Calculate summary statistics
        entity_missing = len([m for m in missing_translations if m["type"] == "entity"])
        static_missing = len([m for m in missing_translations if m["type"] == "static"])

        return success_response(
            data={
                "missing_translations": paginated_missing,
                "summary": {
                    "total_missing": len(missing_translations),
                    "entity_missing": entity_missing,
                    "static_missing": static_missing,
                    "high_priority": len([m for m in missing_translations if m["priority"] == "high"]),
                    "medium_priority": len([m for m in missing_translations if m["priority"] == "medium"]),
                    "by_language": {
                        lang: len([m for m in missing_translations if m["language"] == lang]) for lang in languages
                    },
                },
            },
            meta={
                "page": page,
                "pages": total_pages,
                "per_page": per_page,
                "total": len(missing_translations),
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
        )

    except Exception as e:
        current_app.logger.error(f"Get missing translations error: {e}")
        return internal_error_response("Failed to get missing translations")


@admin_bp.route("/translations/completeness", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_translation_completeness():
    """
    Get comprehensive translation completeness statistics for both system and entity translations

    Query Parameters:
    - include_entities: Include entity translations (default: true)
    - include_system: Include system translations (default: true)
    """
    try:
        languages = ["uz", "en", "ru"]  # Uzbek is default
        include_entities = request.args.get("include_entities", "true").lower() == "true"
        include_system = request.args.get("include_system", "true").lower() == "true"

        completeness_data = {
            "summary": {
                "total_unique_keys": 0,
                "total_possible_translations": 0,
                "total_actual_translations": 0,
                "overall_completion_percentage": 0.0,
            },
            "by_language": {},
            "by_category": {},
            "system_translations": None,
            "entity_translations": None,
        }

        # Initialize language stats
        for lang in languages:
            completeness_data["by_language"][lang] = {"total_keys": 0, "translated": 0, "missing": 0, "percentage": 0.0}

        # ===========================
        # SYSTEM TRANSLATIONS (api.*, error.*, ui.*, etc.)
        # ===========================
        if include_system:
            # Get all translations and filter in Python (regex in DB varies by engine)
            all_translations = Translation.query.filter(Translation.is_active == True).all()  # noqa: E712

            # Filter system translations (keys that don't match EntityType.field.ID format)
            import re

            entity_pattern = re.compile(r"^[A-Z][a-zA-Z]+\.[a-z_]+\.\d+$")
            system_translations = [trans for trans in all_translations if not entity_pattern.match(trans.key)]

            # Group by key to find unique keys
            system_keys = {}
            for trans in system_translations:
                if trans.key not in system_keys:
                    system_keys[trans.key] = {"key": trans.key, "category": trans.category, "languages": {}}
                system_keys[trans.key]["languages"][trans.language] = {"value": trans.value, "translation_id": trans.id}

            # Calculate system translation completeness
            system_stats = {
                "total_unique_keys": len(system_keys),
                "total_possible": len(system_keys) * len(languages),
                "by_language": {},
                "by_category": {},
            }

            for lang in languages:
                translated_count = sum(1 for key_data in system_keys.values() if lang in key_data["languages"])
                missing_count = len(system_keys) - translated_count
                system_stats["by_language"][lang] = {
                    "translated": translated_count,
                    "missing": missing_count,
                    "total": len(system_keys),
                    "percentage": round((translated_count / len(system_keys) * 100) if system_keys else 0, 2),
                }

            # Group by category
            categories = {}
            for key, data in system_keys.items():
                category = data["category"] or "uncategorized"
                if category not in categories:
                    categories[category] = {
                        "total_keys": 0,
                        "by_language": {lang: {"translated": 0, "missing": 0} for lang in languages},
                    }
                categories[category]["total_keys"] += 1
                for lang in languages:
                    if lang in data["languages"]:
                        categories[category]["by_language"][lang]["translated"] += 1
                    else:
                        categories[category]["by_language"][lang]["missing"] += 1

            # Add percentages to categories
            for category, stats in categories.items():
                for lang in languages:
                    total = stats["total_keys"]
                    translated = stats["by_language"][lang]["translated"]
                    stats["by_language"][lang]["percentage"] = round((translated / total * 100) if total > 0 else 0, 2)

            system_stats["by_category"] = categories
            system_stats["total_actual"] = sum(system_stats["by_language"][lang]["translated"] for lang in languages)
            system_stats["overall_percentage"] = round(
                (
                    (system_stats["total_actual"] / system_stats["total_possible"] * 100)
                    if system_stats["total_possible"] > 0
                    else 0
                ),
                2,
            )

            completeness_data["system_translations"] = system_stats

            # Update summary
            completeness_data["summary"]["total_unique_keys"] += system_stats["total_unique_keys"]
            completeness_data["summary"]["total_possible_translations"] += system_stats["total_possible"]
            completeness_data["summary"]["total_actual_translations"] += system_stats["total_actual"]

            # Update language stats
            for lang in languages:
                completeness_data["by_language"][lang]["total_keys"] += system_stats["by_language"][lang]["total"]
                completeness_data["by_language"][lang]["translated"] += system_stats["by_language"][lang]["translated"]
                completeness_data["by_language"][lang]["missing"] += system_stats["by_language"][lang]["missing"]

        # ===========================
        # ENTITY TRANSLATIONS (Product.name.123, etc.)
        # ===========================
        if include_entities:
            # Filter entity translations (keys matching EntityType.field.ID format)
            if not include_system:
                # Need to fetch all translations if we haven't already
                all_translations = Translation.query.filter(Translation.is_active == True).all()  # noqa: E712
                import re

                entity_pattern = re.compile(r"^[A-Z][a-zA-Z]+\.[a-z_]+\.\d+$")

            entity_translations = [trans for trans in all_translations if entity_pattern.match(trans.key)]

            # Group by key to find unique keys
            entity_keys = {}
            for trans in entity_translations:
                if trans.key not in entity_keys:
                    # Parse entity type, field, and ID from key
                    parts = trans.key.split(".")
                    if len(parts) == 3:
                        entity_type, field, entity_id = parts
                        entity_keys[trans.key] = {
                            "key": trans.key,
                            "entity_type": entity_type,
                            "field": field,
                            "entity_id": entity_id,
                            "category": trans.category,
                            "languages": {},
                        }
                if trans.key in entity_keys:
                    entity_keys[trans.key]["languages"][trans.language] = {
                        "value": trans.value,
                        "translation_id": trans.id,
                    }

            # Calculate entity translation completeness
            entity_stats = {
                "total_unique_keys": len(entity_keys),
                "total_possible": len(entity_keys) * len(languages),
                "by_language": {},
                "by_entity_type": {},
            }

            for lang in languages:
                translated_count = sum(1 for key_data in entity_keys.values() if lang in key_data["languages"])
                missing_count = len(entity_keys) - translated_count
                entity_stats["by_language"][lang] = {
                    "translated": translated_count,
                    "missing": missing_count,
                    "total": len(entity_keys),
                    "percentage": round((translated_count / len(entity_keys) * 100) if entity_keys else 0, 2),
                }

            # Group by entity type
            entity_types = {}
            for key, data in entity_keys.items():
                entity_type = data["entity_type"]
                if entity_type not in entity_types:
                    entity_types[entity_type] = {
                        "total_keys": 0,
                        "by_language": {lang: {"translated": 0, "missing": 0} for lang in languages},
                    }
                entity_types[entity_type]["total_keys"] += 1
                for lang in languages:
                    if lang in data["languages"]:
                        entity_types[entity_type]["by_language"][lang]["translated"] += 1
                    else:
                        entity_types[entity_type]["by_language"][lang]["missing"] += 1

            # Add percentages to entity types
            for entity_type, stats in entity_types.items():
                for lang in languages:
                    total = stats["total_keys"]
                    translated = stats["by_language"][lang]["translated"]
                    stats["by_language"][lang]["percentage"] = round((translated / total * 100) if total > 0 else 0, 2)

            entity_stats["by_entity_type"] = entity_types
            entity_stats["total_actual"] = sum(entity_stats["by_language"][lang]["translated"] for lang in languages)
            entity_stats["overall_percentage"] = round(
                (
                    (entity_stats["total_actual"] / entity_stats["total_possible"] * 100)
                    if entity_stats["total_possible"] > 0
                    else 0
                ),
                2,
            )

            completeness_data["entity_translations"] = entity_stats

            # Update summary
            completeness_data["summary"]["total_unique_keys"] += entity_stats["total_unique_keys"]
            completeness_data["summary"]["total_possible_translations"] += entity_stats["total_possible"]
            completeness_data["summary"]["total_actual_translations"] += entity_stats["total_actual"]

            # Update language stats
            for lang in languages:
                completeness_data["by_language"][lang]["total_keys"] += entity_stats["by_language"][lang]["total"]
                completeness_data["by_language"][lang]["translated"] += entity_stats["by_language"][lang]["translated"]
                completeness_data["by_language"][lang]["missing"] += entity_stats["by_language"][lang]["missing"]

        # Calculate overall percentages
        if completeness_data["summary"]["total_possible_translations"] > 0:
            completeness_data["summary"]["overall_completion_percentage"] = round(
                (
                    completeness_data["summary"]["total_actual_translations"]
                    / completeness_data["summary"]["total_possible_translations"]
                    * 100
                ),
                2,
            )

        for lang in languages:
            total = completeness_data["by_language"][lang]["total_keys"]
            translated = completeness_data["by_language"][lang]["translated"]
            completeness_data["by_language"][lang]["percentage"] = round(
                (translated / total * 100) if total > 0 else 0, 2
            )

        # Group completeness by category (for both system and entity)
        all_categories = {}

        # Add system categories
        if include_system and completeness_data["system_translations"]:
            for category, stats in completeness_data["system_translations"]["by_category"].items():
                all_categories[f"system:{category}"] = stats

        # Add entity categories
        if include_entities and completeness_data["entity_translations"]:
            for entity_type, stats in completeness_data["entity_translations"]["by_entity_type"].items():
                all_categories[f"entity:{entity_type}"] = stats

        completeness_data["by_category"] = all_categories

        return success_response(data=completeness_data)

    except Exception as e:
        current_app.logger.error(f"Get translation completeness error: {e}", exc_info=True)
        return internal_error_response("Failed to get translation completeness statistics")


# ============================================================================
# BLOG MANAGEMENT ENDPOINTS
# ============================================================================


@admin_bp.route("/blog/posts", methods=["GET"])
@jwt_required()
@admin_required
def get_all_blog_posts():
    """
    Admin: Get all blog posts (including drafts and archived)
    Query params:
    - page: Page number (default: 1)
    - per_page: Items per page (default: 20)
    - status: Filter by status (draft/published/archived)
    - category: Filter by category
    - search: Search in title
    - language: Language code
    """
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 20, type=int), 100)
        status = request.args.get("status", None)
        category = request.args.get("category", None)
        search = request.args.get("search", None)
        language = request.args.get("language", "uz")

        # Base query
        query = BlogPost.query

        # Apply filters
        if status:
            try:
                status_enum = BlogStatus(status)
                query = query.filter(BlogPost.status == status_enum)
            except ValueError:
                return error_response("Invalid status", status_code=400)

        if category:
            try:
                category_enum = BlogCategory(category)
                query = query.filter(BlogPost.category == category_enum)
            except ValueError:
                return error_response("Invalid category", status_code=400)

        if search:
            query = query.filter(BlogPost.title.ilike(f"%{search}%"))

        # Order by updated date
        query = query.order_by(desc(BlogPost.updated_at))

        # Paginate
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        # Serialize posts
        posts = [post.to_dict(language, include_all_translations=True) for post in pagination.items]

        return paginated_response(
            items=posts, page=pagination.page, per_page=pagination.per_page, total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Error in admin get posts: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts/<int:post_id>", methods=["GET"])
@jwt_required()
@admin_required
def admin_get_blog_post(post_id):
    """Admin: Get a single blog post by ID with all translations"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response("Blog post not found")

        language = request.args.get("language", "uz")
        return success_response(
            data=post.to_dict(language, include_all_translations=True), message="Blog post retrieved successfully"
        )

    except Exception as e:
        current_app.logger.error(f"Error in admin get post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts", methods=["POST"])
@jwt_required()
@admin_required
@validate_json()
def admin_create_blog_post():
    """
    Admin: Create a new blog post
    Required fields:
    - title_uz, title_ru, title_en: Titles in all languages
    - excerpt_uz, excerpt_ru, excerpt_en: Excerpts in all languages
    - content_uz, content_ru, content_en: Full content in all languages
    - category: Blog category
    - slug: URL slug (unique)

    Optional fields:
    - author_name_uz, author_name_ru, author_name_en: Author names
    - featured_image: Image URL
    - image_alt_text: Alt text for image
    - tags: Comma-separated tags
    - is_featured: Boolean
    - status: draft/published (default: draft)
    """
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        data = request.get_json()
        current_user_id = get_jwt_identity()

        # Validate required fields
        required_fields = [
            "title_uz",
            "title_ru",
            "title_en",
            "slug",
            "category",
            "excerpt_uz",
            "excerpt_ru",
            "excerpt_en",
            "content_uz",
            "content_ru",
            "content_en",
        ]

        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return validation_error_response(f"Missing required fields: {', '.join(missing_fields)}")

        # Check if slug is unique
        if BlogPost.query.filter_by(slug=data["slug"]).first():
            return error_response("Slug already exists", status_code=409)

        # Validate category
        try:
            category_enum = BlogCategory(data["category"])
        except ValueError:
            return error_response("Invalid category", status_code=400)

        # Create blog post (Uzbek as default)
        post = BlogPost(
            title=data["title_uz"],
            slug=data["slug"],
            excerpt=data["excerpt_uz"],
            content=data["content_uz"],
            author_name=data.get("author_name_uz", "Admin"),
            author_id=current_user_id,
            category=category_enum,
            tags=data.get("tags"),
            featured_image=data.get("featured_image"),
            image_alt_text=data.get("image_alt_text"),
            is_featured=data.get("is_featured", False),
            sort_order=data.get("sort_order", 0),
            status=BlogStatus(data.get("status", "draft")),
        )

        db.session.add(post)
        db.session.flush()  # Get the post ID

        # Set translations for all languages
        translations = {
            "title": {"uz": data["title_uz"], "ru": data["title_ru"], "en": data["title_en"]},
            "excerpt": {"uz": data["excerpt_uz"], "ru": data["excerpt_ru"], "en": data["excerpt_en"]},
            "content": {"uz": data["content_uz"], "ru": data["content_ru"], "en": data["content_en"]},
        }

        # Add author name translations if provided
        if data.get("author_name_uz"):
            translations["author_name"] = {
                "uz": data.get("author_name_uz", "Admin"),
                "ru": data.get("author_name_ru", data.get("author_name_uz", "Admin")),
                "en": data.get("author_name_en", data.get("author_name_uz", "Admin")),
            }

        # Add SEO translations if provided
        if data.get("meta_title_uz"):
            translations["meta_title"] = {
                "uz": data.get("meta_title_uz", ""),
                "ru": data.get("meta_title_ru", ""),
                "en": data.get("meta_title_en", ""),
            }

        if data.get("meta_description_uz"):
            translations["meta_description"] = {
                "uz": data.get("meta_description_uz", ""),
                "ru": data.get("meta_description_ru", ""),
                "en": data.get("meta_description_en", ""),
            }

        post.set_translations(translations)

        # If published, set published_at
        if post.status == BlogStatus.PUBLISHED and not post.published_at:
            post.published_at = datetime.now(UTC)

        db.session.commit()

        current_app.logger.info(f"Blog post created: {post.id} by user {current_user_id}")

        return created_response(
            data=post.to_dict("uz", include_all_translations=True), message="Blog post created successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating blog post: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts/<int:post_id>", methods=["PUT"])
@jwt_required()
@admin_required
@validate_json()
def admin_update_blog_post(post_id):
    """Admin: Update a blog post"""
    try:
        from business_app.models.blog import BlogPost, BlogStatus, BlogCategory

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response("Blog post not found")

        data = request.get_json()

        # Update basic fields
        if "slug" in data and data["slug"] != post.slug:
            # Check if new slug is unique
            if BlogPost.query.filter(BlogPost.slug == data["slug"], BlogPost.id != post_id).first():
                return error_response("Slug already exists", status_code=409)
            post.slug = data["slug"]

        if "category" in data:
            try:
                post.category = BlogCategory(data["category"])
            except ValueError:
                return error_response("Invalid category", status_code=400)

        if "tags" in data:
            post.tags = data["tags"]
        if "featured_image" in data:
            post.featured_image = data["featured_image"]
        if "image_alt_text" in data:
            post.image_alt_text = data["image_alt_text"]
        if "is_featured" in data:
            post.is_featured = data["is_featured"]
        if "sort_order" in data:
            post.sort_order = data["sort_order"]
        if "status" in data:
            old_status = post.status
            post.status = BlogStatus(data["status"])
            # Set published_at when publishing for the first time
            if post.status == BlogStatus.PUBLISHED and old_status != BlogStatus.PUBLISHED:
                if not post.published_at:
                    post.published_at = datetime.now(UTC)

        # Update Uzbek default values
        if "title_uz" in data:
            post.title = data["title_uz"]
        if "excerpt_uz" in data:
            post.excerpt = data["excerpt_uz"]
        if "content_uz" in data:
            post.content = data["content_uz"]
        if "author_name_uz" in data:
            post.author_name = data["author_name_uz"]

        # Update translations
        translations = {}

        # Title translations
        if any(key in data for key in ["title_uz", "title_ru", "title_en"]):
            translations["title"] = {
                "uz": data.get("title_uz", post.title),
                "ru": data.get("title_ru", post.get_translated("title", "ru")),
                "en": data.get("title_en", post.get_translated("title", "en")),
            }

        # Excerpt translations
        if any(key in data for key in ["excerpt_uz", "excerpt_ru", "excerpt_en"]):
            translations["excerpt"] = {
                "uz": data.get("excerpt_uz", post.excerpt),
                "ru": data.get("excerpt_ru", post.get_translated("excerpt", "ru")),
                "en": data.get("excerpt_en", post.get_translated("excerpt", "en")),
            }

        # Content translations
        if any(key in data for key in ["content_uz", "content_ru", "content_en"]):
            translations["content"] = {
                "uz": data.get("content_uz", post.content),
                "ru": data.get("content_ru", post.get_translated("content", "ru")),
                "en": data.get("content_en", post.get_translated("content", "en")),
            }

        # Author name translations
        if any(key in data for key in ["author_name_uz", "author_name_ru", "author_name_en"]):
            translations["author_name"] = {
                "uz": data.get("author_name_uz", post.author_name),
                "ru": data.get("author_name_ru", post.get_translated("author_name", "ru")),
                "en": data.get("author_name_en", post.get_translated("author_name", "en")),
            }

        # SEO translations
        if any(key in data for key in ["meta_title_uz", "meta_title_ru", "meta_title_en"]):
            translations["meta_title"] = {
                "uz": data.get("meta_title_uz", ""),
                "ru": data.get("meta_title_ru", ""),
                "en": data.get("meta_title_en", ""),
            }

        if any(key in data for key in ["meta_description_uz", "meta_description_ru", "meta_description_en"]):
            translations["meta_description"] = {
                "uz": data.get("meta_description_uz", ""),
                "ru": data.get("meta_description_ru", ""),
                "en": data.get("meta_description_en", ""),
            }

        if translations:
            post.set_translations(translations)

        post.updated_at = datetime.now(UTC)
        db.session.commit()

        current_app.logger.info(f"Blog post updated: {post.id}")

        return success_response(
            data=post.to_dict("uz", include_all_translations=True), message="Blog post updated successfully"
        )

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts/<int:post_id>", methods=["DELETE"])
@jwt_required()
@admin_required
def admin_delete_blog_post(post_id):
    """Admin: Delete a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response("Blog post not found")

        db.session.delete(post)
        db.session.commit()

        current_app.logger.info(f"Blog post deleted: {post_id}")

        return success_response(message=get_translation("api.admin.success.blog_post_deleted"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts/<int:post_id>/publish", methods=["POST"])
@jwt_required()
@admin_required
def admin_publish_blog_post(post_id):
    """Admin: Publish a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response("Blog post not found")

        post.publish()
        db.session.commit()

        current_app.logger.info(f"Blog post published: {post_id}")

        return success_response(data=post.to_dict("uz"), message="Blog post published successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error publishing blog post {post_id}: {str(e)}")
        return internal_error_response()


@admin_bp.route("/blog/posts/<int:post_id>/unpublish", methods=["POST"])
@jwt_required()
@admin_required
def admin_unpublish_blog_post(post_id):
    """Admin: Unpublish a blog post"""
    try:
        from business_app.models.blog import BlogPost

        post = BlogPost.query.get(post_id)
        if not post:
            return not_found_response("Blog post not found")

        post.unpublish()
        db.session.commit()

        current_app.logger.info(f"Blog post unpublished: {post_id}")

        return success_response(data=post.to_dict("uz"), message="Blog post unpublished successfully")

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error unpublishing blog post {post_id}: {str(e)}")
        return internal_error_response()


# ========================================================================
# FILE UPLOAD ENDPOINTS
# ========================================================================


@admin_bp.route("/upload/image", methods=["POST"])
@jwt_required()
@admin_required
def upload_image():
    """
    Upload image file for blog posts, products, etc.
    Returns the file URL that can be stored in the database

    Expected multipart/form-data with:
    - file: Image file
    - folder: (optional) Subfolder name (default: 'blog')
    - resize: (optional) Whether to resize (default: true)
    - max_width: (optional) Max width in pixels (default: 1920)
    - max_height: (optional) Max height in pixels (default: 1080)
    """
    try:
        current_user_id = get_jwt_identity()

        # Check if file is in request
        if "file" not in request.files:
            return validation_error_response("No file provided")

        file = request.files["file"]

        if file.filename == "":
            return validation_error_response("No file selected")

        # Get optional parameters
        folder = request.form.get("folder", "blog")
        resize = request.form.get("resize", "true").lower() == "true"
        max_width = int(request.form.get("max_width", 1920))
        max_height = int(request.form.get("max_height", 1080))
        quality = int(request.form.get("quality", 85))

        # Initialize file storage service
        from business_app.services.file_storage_service import FileStorageService

        storage_service = FileStorageService()

        current_app.logger.info(f"filename: {file.filename}")

        # Upload image
        upload_result = storage_service.upload_image(
            file=file,
            filename=file.filename,
            folder=f"images/{folder}",
            user_id=current_user_id,
            resize=resize,
            max_width=max_width,
            max_height=max_height,
            quality=quality,
        )

        current_app.logger.info(f"Image uploaded by admin {current_user_id}: {upload_result['filename']}")

        return created_response(
            data={
                "url": upload_result["url"],
                "file_path": upload_result["file_path"],
                "filename": upload_result["filename"],
                "size": upload_result["size"],
                "thumbnails": {name: thumb["url"] for name, thumb in upload_result.get("thumbnails", {}).items()},
            },
            message="Image uploaded successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Error uploading image: {str(e)}")
        return error_response(str(e), status_code=500)


@admin_bp.route("/upload/file", methods=["POST"])
@jwt_required()
@admin_required
def upload_file():
    """
    Upload generic file (documents, etc.)
    Returns the file URL

    Expected multipart/form-data with:
    - file: File
    - folder: (optional) Subfolder name (default: 'documents')
    """
    try:
        current_user_id = get_jwt_identity()

        # Check if file is in request
        if "file" not in request.files:
            return validation_error_response("No file provided")

        file = request.files["file"]

        if file.filename == "":
            return validation_error_response("No file selected")

        # Get optional parameters
        folder = request.form.get("folder", "documents")

        # Initialize file storage service
        from business_app.services.file_storage_service import FileStorageService

        storage_service = FileStorageService()

        # Upload file
        upload_result = storage_service.upload_file(
            file=file, filename=file.filename, folder=folder, user_id=current_user_id
        )

        current_app.logger.info(f"File uploaded by admin {current_user_id}: {upload_result['filename']}")

        return created_response(
            data={
                "url": upload_result["url"],
                "file_path": upload_result["file_path"],
                "filename": upload_result["filename"],
                "size": upload_result["size"],
                "content_type": upload_result.get("content_type"),
            },
            message="File uploaded successfully",
        )

    except Exception as e:
        current_app.logger.error(f"Error uploading file: {str(e)}")
        return error_response(str(e), status_code=500)


# ============================================================================
# STAFF MANAGEMENT ENDPOINTS (Admin Panel - Phase 6)
# ============================================================================


def _serialize_staff_delivery_person_data(dp: DeliveryPerson, active_delivery_count=None) -> dict:
    """Serialize delivery person with extra staff/admin fields."""
    from business_app.services.staff_service import StaffService

    if active_delivery_count is None:
        active_delivery_count = StaffService.get_active_delivery_count(dp.user_id)
    data = serialize_delivery_person_admin(
        dp,
        current_active_deliveries=active_delivery_count,
    )
    data["user_id"] = dp.user_id
    data["employee_id"] = dp.employee_id
    data["vehicle_capacity_kg"] = dp.vehicle_capacity_kg
    data["working_hours_start"] = dp.working_hours_start
    data["working_hours_end"] = dp.working_hours_end
    data["working_days"] = dp.working_days
    data["max_concurrent_deliveries"] = dp.max_concurrent_deliveries
    data["current_active_deliveries"] = active_delivery_count
    data["total_cash_collected"] = float(dp.total_cash_collected or 0)
    data["notifications_muted"] = dp.notifications_muted
    data["hire_date"] = dp.hire_date.isoformat() if dp.hire_date else None
    data["emergency_contact_name"] = dp.emergency_contact_name
    data["emergency_contact_phone"] = dp.emergency_contact_phone
    return data


def _serialize_delivery_person_admin_items(delivery_people):
    """Serialize delivery-person rows with live workload counts."""
    from business_app.services.staff_service import StaffService

    delivery_people = list(delivery_people or [])
    active_counts = StaffService.get_active_delivery_counts([person.user_id for person in delivery_people])
    return [
        serialize_delivery_person_admin(
            person,
            current_active_deliveries=active_counts.get(person.user_id, 0),
        )
        for person in delivery_people
    ]


def _build_staff_order_info(delivery: Delivery) -> dict:
    """Build compact order payload for staff assignment notifications."""
    order = delivery.order
    if not order:
        return {"delivery_id": delivery.id, "order_id": delivery.order_id}

    address = order.delivery_address.full_address if order.delivery_address else None
    return {
        "delivery_id": delivery.id,
        "order_id": order.id,
        "order_number": order.order_number,
        "status": order.status.value if hasattr(order.status, "value") else order.status,
        "total_amount": float(order.total_amount or 0),
        "payment_method": order.payment_method.value if getattr(order, "payment_method", None) else None,
        "delivery_address": address,
    }


@admin_bp.route("/staff/delivery-persons", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def create_staff_delivery_person():
    """Create a delivery person (user + delivery profile)."""
    try:
        from business_app.services.staff_service import StaffService

        payload = request.get_json() or {}
        actor_id = int(get_jwt_identity())
        delivery_person = StaffService.create_delivery_person(payload, created_by=actor_id)

        return created_response(
            data={"delivery_person": _serialize_staff_delivery_person_data(delivery_person)},
            message="Delivery person created successfully",
        )
    except (ValidationError, ConflictError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Create staff delivery person error: {e}")
        return internal_error_response("Failed to create delivery person")


@admin_bp.route("/staff/delivery-persons", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_staff_delivery_persons():
    """List delivery persons with filtering and pagination"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)
        search = request.args.get("search", "").strip()
        status = request.args.get("status")  # 'active', 'inactive'
        available = request.args.get("available")  # 'true', 'false'

        query = DeliveryPerson.query.options(db.joinedload(DeliveryPerson.user))

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    DeliveryPerson.full_name.ilike(search_term),
                    DeliveryPerson.phone.ilike(search_term),
                    DeliveryPerson.employee_id.ilike(search_term),
                )
            )

        if status == "active":
            query = query.filter(DeliveryPerson.is_active == True)  # noqa: E712
        elif status == "inactive":
            query = query.filter(DeliveryPerson.is_active == False)  # noqa: E712

        if available == "true":
            query = query.filter(DeliveryPerson.is_available == True)  # noqa: E712
        elif available == "false":
            query = query.filter(DeliveryPerson.is_available == False)  # noqa: E712

        query = query.order_by(DeliveryPerson.created_at.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        from business_app.services.staff_service import StaffService

        active_counts = StaffService.get_active_delivery_counts([dp.user_id for dp in pagination.items])
        persons_data = [
            _serialize_staff_delivery_person_data(
                dp,
                active_delivery_count=active_counts.get(dp.user_id, 0),
            )
            for dp in pagination.items
        ]

        # Summary stats
        total_drivers = DeliveryPerson.query.count()
        active_drivers = DeliveryPerson.query.filter_by(is_active=True).count()
        available_drivers = DeliveryPerson.query.filter_by(is_active=True, is_available=True).count()

        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deliveries_today = Delivery.query.filter(
            Delivery.status == DeliveryStatus.DELIVERED.value, Delivery.delivered_at >= today_start
        ).count()

        avg_rating = (
            db.session.query(func.avg(DeliveryPerson.average_rating))
            .filter(DeliveryPerson.is_active == True, DeliveryPerson.average_rating > 0)  # noqa: E712
            .scalar()
        )

        return paginated_response(
            items=persons_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={
                "summary": {
                    "total_drivers": total_drivers,
                    "active_drivers": active_drivers,
                    "available_drivers": available_drivers,
                    "deliveries_today": deliveries_today,
                    "avg_rating": round(float(avg_rating or 0), 2),
                }
            },
        )

    except Exception as e:
        current_app.logger.error(f"Get staff delivery persons error: {e}")
        return internal_error_response("Failed to get delivery persons")


@admin_bp.route("/staff/delivery-persons/<int:person_id>", methods=["GET"])
@jwt_required()
@staff_or_higher_required
def get_staff_delivery_person_details(person_id):
    """Get detailed delivery person info with stats"""
    try:
        dp = DeliveryPerson.query.options(db.joinedload(DeliveryPerson.user)).get(person_id)
        if not dp:
            return not_found_response(resource_type="DeliveryPerson")

        data = _serialize_staff_delivery_person_data(dp)

        # Get performance stats
        from business_app.services.staff_service import StaffService

        stats = StaffService.get_delivery_stats(dp.user_id, period="month")
        data["stats"] = stats

        return success_response(data={"delivery_person": data})

    except Exception as e:
        current_app.logger.error(f"Get delivery person details error: {e}")
        return internal_error_response("Failed to get delivery person details")


@admin_bp.route("/staff/delivery-persons/<int:person_id>", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
def update_staff_delivery_person(person_id):
    """Edit delivery person profile fields."""
    try:
        from business_app.services.staff_service import StaffService

        payload = request.get_json() or {}
        actor_id = int(get_jwt_identity())
        delivery_person = StaffService.update_delivery_person(person_id, payload, updated_by=actor_id)

        return success_response(
            data={"delivery_person": _serialize_staff_delivery_person_data(delivery_person)},
            message="Delivery person updated successfully",
        )
    except (ValidationError, ConflictError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Update staff delivery person error: {e}")
        return internal_error_response("Failed to update delivery person")


@admin_bp.route("/staff/delivery-persons/<int:person_id>/mute", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
def toggle_staff_mute_notifications(person_id):
    """Toggle notification muting for a delivery person"""
    try:
        data = request.get_json()
        muted = data.get("muted", True)

        dp = DeliveryPerson.query.get(person_id)
        if not dp:
            return not_found_response(resource_type="DeliveryPerson")

        from business_app.services.staff_service import StaffService

        StaffService.mute_notifications(dp.user_id, muted)

        return success_response(
            data={"muted": muted}, message=f"Notifications {'muted' if muted else 'unmuted'} successfully"
        )

    except Exception as e:
        current_app.logger.error(f"Toggle mute notifications error: {e}")
        return internal_error_response("Failed to toggle notifications")


@admin_bp.route("/staff/operators", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_staff_operators():
    """List operators with filtering and pagination"""
    try:
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)
        search = request.args.get("search", "").strip()
        status = request.args.get("status")  # 'active', 'inactive'

        query = User.query.filter(_is_operator_staff_member())

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                or_(
                    User.first_name.ilike(search_term), User.last_name.ilike(search_term), User.phone.ilike(search_term)
                )
            )

        if status == "active":
            query = query.filter(User.status == UserStatus.ACTIVE.value)
        elif status == "inactive":
            query = query.filter(User.status != UserStatus.ACTIVE.value)

        query = query.order_by(User.created_at.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        operators_data = []
        for user in pagination.items:
            # Count orders created by this operator today
            orders_today = Order.query.filter(
                Order.created_by_staff_id == user.id, Order.created_at >= today_start
            ).count()

            total_orders = Order.query.filter(Order.created_by_staff_id == user.id).count()

            operators_data.append(
                {
                    "id": user.id,
                    "full_name": user.full_name,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "phone": user.phone,
                    "email": user.email,
                    "status": user.status.value if hasattr(user.status, "value") else user.status,
                    "role": user.role.value if hasattr(user.role, "value") else user.role,
                    "staff_roles": user.staff_roles or [],
                    "last_login": user.last_login.isoformat() if user.last_login else None,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "orders_today": orders_today,
                    "total_orders_created": total_orders,
                }
            )

        # Summary
        total_operators = User.query.filter(_is_operator_staff_member()).count()

        active_operators = User.query.filter(
            _is_operator_staff_member(), User.status == UserStatus.ACTIVE.value
        ).count()

        return paginated_response(
            items=operators_data,
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={
                "summary": {
                    "total_operators": total_operators,
                    "active_operators": active_operators,
                }
            },
        )

    except Exception as e:
        current_app.logger.error(f"Get staff operators error: {e}")
        return internal_error_response("Failed to get operators")


@admin_bp.route("/staff/operators", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def create_staff_operator():
    """Create operator account or grant operator role to an existing user."""
    try:
        from business_app.services.staff_service import StaffService

        payload = request.get_json() or {}
        actor_id = int(get_jwt_identity())
        user = StaffService.create_operator(payload, created_by=actor_id)

        return created_response(
            data={"operator": user.to_dict()},
            message="Operator created successfully",
        )
    except (ValidationError, ConflictError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Create staff operator error: {e}")
        return internal_error_response("Failed to create operator")


@admin_bp.route("/staff/operators/<int:user_id>", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
def update_staff_operator(user_id):
    """Edit operator profile fields and staff roles."""
    try:
        from business_app.services.staff_service import StaffService

        payload = request.get_json() or {}
        actor_id = int(get_jwt_identity())
        user = StaffService.update_operator(user_id, payload, updated_by=actor_id)

        return success_response(
            data={"operator": user.to_dict()},
            message="Operator updated successfully",
        )
    except (ValidationError, ConflictError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Update staff operator error: {e}")
        return internal_error_response("Failed to update operator")


@admin_bp.route("/staff/users/<int:user_id>/roles", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
@validate_json(["staff_roles"])
def update_staff_user_roles(user_id):
    """Update dual-role assignment for any staff user."""
    try:
        from business_app.services.staff_service import StaffService

        payload = request.get_json() or {}
        actor_id = int(get_jwt_identity())
        user = StaffService.update_staff_roles(
            user_id=user_id,
            staff_roles=payload.get("staff_roles"),
            updated_by=actor_id,
        )

        return success_response(
            data={"user": user.to_dict()},
            message="Staff roles updated successfully",
        )
    except (ValidationError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Update staff roles error: {e}")
        return internal_error_response("Failed to update staff roles")


@admin_bp.route("/staff/overview", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_admin_staff_overview():
    """Get unified staff overview for admin panel"""
    try:
        from business_app.services.staff_service import StaffService

        overview = StaffService.get_staff_overview()

        # Add operator count
        total_operators = User.query.filter(_is_operator_staff_member()).count()
        overview["total_operators"] = total_operators

        # All staff members (delivery + operators)
        delivery_persons = DeliveryPerson.query.filter_by(is_active=True).count()
        overview["total_delivery_persons"] = delivery_persons
        overview["total_staff"] = delivery_persons + total_operators

        return success_response(data={"overview": overview})

    except Exception as e:
        current_app.logger.error(f"Get admin staff overview error: {e}")
        return internal_error_response("Failed to get staff overview")


@admin_bp.route("/staff/delivery/assign/<int:delivery_id>", methods=["POST"])
@jwt_required()
@manager_or_higher_required
@validate_json(["delivery_person_id"])
def admin_assign_delivery(delivery_id):
    """Assign a delivery to a delivery person (admin action)"""
    try:
        data = request.get_json()
        delivery_person_id = data["delivery_person_id"]

        from business_app.services.staff_service import StaffService

        delivery = StaffService.accept_order(delivery_id, delivery_person_id)

        # Notify assigned delivery person via staff bot, if linked Telegram exists.
        try:
            assigned_user = User.query.get(delivery_person_id)
            if assigned_user and assigned_user.telegram_id:
                from business_app.tasks.staff_tasks import notify_staff_order_assigned

                notify_staff_order_assigned.delay(assigned_user.telegram_id, _build_staff_order_info(delivery))
        except Exception as notify_exc:
            current_app.logger.warning(
                "Failed to enqueue staff assignment notification for delivery %s: %s",
                delivery_id,
                notify_exc,
            )

        return success_response(data={"delivery": delivery.to_dict()}, message="Delivery assigned successfully")

    except (ValidationError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Admin assign delivery error: {e}")
        return internal_error_response("Failed to assign delivery")


@admin_bp.route("/staff/delivery/reassign/<int:delivery_id>", methods=["PUT"])
@jwt_required()
@manager_or_higher_required
@validate_json(["new_delivery_person_id"])
def admin_reassign_delivery(delivery_id):
    """Reassign a delivery to a different delivery person"""
    try:
        data = request.get_json()
        new_person_id = data["new_delivery_person_id"]
        existing_delivery = Delivery.query.get(delivery_id)
        if not existing_delivery:
            return not_found_response(resource_type="Delivery")

        old_person_id = existing_delivery.delivery_person_id
        if old_person_id == new_person_id:
            return success_response(
                data={"delivery": existing_delivery.to_dict()},
                message="Delivery is already assigned to this delivery person",
            )

        actor_id = int(get_jwt_identity())
        delivery = AdminDeliveryService.reassign_delivery(
            delivery_id=delivery_id,
            new_person_id=new_person_id,
            actor_id=actor_id,
        )

        current_app.logger.info(f"Delivery {delivery_id} reassigned from {old_person_id} to {new_person_id}")

        try:
            old_user = User.query.get(old_person_id) if old_person_id else None
            new_user = User.query.get(new_person_id) if new_person_id else None
            old_telegram_id = old_user.telegram_id if old_user else None
            new_telegram_id = new_user.telegram_id if new_user else None
            if old_telegram_id or new_telegram_id:
                from business_app.tasks.staff_tasks import notify_staff_order_reassigned

                notify_staff_order_reassigned.delay(
                    old_telegram_id,
                    new_telegram_id,
                    _build_staff_order_info(delivery),
                )
        except Exception as notify_exc:
            current_app.logger.warning(
                "Failed to enqueue staff reassignment notification for delivery %s: %s",
                delivery_id,
                notify_exc,
            )

        return success_response(data={"delivery": delivery.to_dict()}, message="Delivery reassigned successfully")

    except (ValidationError, NotFoundError) as e:
        return error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Admin reassign delivery error: {e}")
        return internal_error_response("Failed to reassign delivery")


@admin_bp.route("/staff/cash-reconciliation", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_cash_reconciliation_report():
    """Generate cash reconciliation report per driver/period"""
    try:
        period = request.args.get("period", "day")  # day, week, month
        driver_id = request.args.get("driver_id", type=int)
        status = request.args.get("status")
        blocked_only = request.args.get("blocked_only", "false").lower() == "true"
        warning_only = request.args.get("warning_only", "false").lower() == "true"
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        min_session_age_days = request.args.get("min_session_age_days", type=int)
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)

        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        report = DriverReconciliationService().get_report(
            period=period,
            driver_user_id=driver_id,
            page=page,
            per_page=per_page,
            status=status,
            blocked_only=blocked_only,
            warning_only=warning_only,
            start_date=start_date,
            end_date=end_date,
            min_session_age_days=min_session_age_days,
        )
        report["period"] = period

        return success_response(data=report)

    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Cash reconciliation report error: {e}")
        return internal_error_response("Failed to generate cash reconciliation report")


@admin_bp.route("/staff/cash-reconciliation/collections", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def record_cash_collection_admin():
    """Record a standalone COD collection or correction from admin workflows."""
    try:
        actor_user_id = int(get_jwt_identity())
        data = request.get_json() or {}
        if data.get("customer_id") is None:
            return validation_error_response("customer_id is required")
        if data.get("amount") is None:
            return validation_error_response("amount is required")

        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        event = CashCollectionService().post_collection(
            customer_id=data.get("customer_id"),
            amount=data.get("amount"),
            source=data.get("source") or "standalone_meeting",
            collector_user_id=data.get("collector_user_id"),
            recorded_by_user_id=actor_user_id,
            order_id=data.get("order_id"),
            delivery_id=data.get("delivery_id"),
            driver_cash_session_id=data.get("driver_cash_session_id"),
            notes=data.get("notes"),
            proof_data=data.get("proof_data") or {},
            occurred_at=data.get("occurred_at"),
            manual_allocations=data.get("manual_allocations"),
            allocation_mode=data.get("allocation_mode", "auto"),
            idempotency_key=data.get("idempotency_key"),
        )

        session_payload = None
        if event.driver_cash_session_id:
            session_payload = DriverReconciliationService().get_session_detail(event.driver_cash_session_id)

        return success_response(
            data={
                "cash_collection_event": event.to_dict(),
                "driver_cash_session": session_payload,
            },
            status_code=201,
        )
    except NotFoundError as e:
        return not_found_response(str(e))
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Admin cash collection record error: {e}")
        return internal_error_response("Failed to record cash collection")


@admin_bp.route("/staff/cash-reconciliation/users/search", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def search_cod_collection_users_admin():
    """Search users for COD collection workflows in admin surfaces."""
    try:
        from business_app.services.staff_service import StaffService

        query = request.args.get("q", "")
        search_type = request.args.get("type", "phone")
        only_with_open_cod = request.args.get("only_with_open_cod", "true").lower() != "false"

        items = StaffService.search_customers_for_cod_collection(
            query,
            search_type,
            only_with_open_cod=only_with_open_cod,
        )
        return success_response({"items": items, "total": len(items)})
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Admin COD collection user search error: {e}")
        return internal_error_response("Failed to search COD collection users")


@admin_bp.route("/staff/cash-reconciliation/users/with-open-cod", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def list_cod_collection_users_with_open_debts_admin():
    """List users that currently have open delivered COD debts."""
    try:
        from business_app.services.cash_collection_service import CashCollectionService

        limit = request.args.get("limit", 200, type=int)
        items = CashCollectionService().list_users_with_open_cod_debts(limit=limit)
        return success_response({"items": items, "total": len(items)})
    except Exception as e:
        current_app.logger.error(f"Admin COD debtors listing error: {e}")
        return internal_error_response("Failed to load users with open COD debts")


@admin_bp.route("/staff/cash-reconciliation/customers/with-prepayment-balance", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def list_customers_with_prepayment_balance_admin():
    """List customers carrying an unapplied COD over-collection (prepayment) balance."""
    try:
        from business_app.services.cash_collection_service import CashCollectionService

        limit = request.args.get("limit", 200, type=int)
        search = request.args.get("search", type=str)
        items = CashCollectionService().list_customers_with_prepayment_balance(
            limit=limit,
            search=search,
        )
        return success_response({"items": items, "total": len(items)})
    except Exception as e:
        current_app.logger.error(f"Admin prepayment customers listing error: {e}")
        return internal_error_response("Failed to load customers with prepayment balance")


@admin_bp.route("/staff/cash-reconciliation/sessions/<int:session_id>", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_cash_reconciliation_session(session_id):
    """Get one driver reconciliation session with collection drill-down."""
    try:
        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        payload = DriverReconciliationService().get_session_detail(session_id)
        return success_response(data=payload)
    except NotFoundError:
        return not_found_response("Driver cash session not found")
    except Exception as e:
        current_app.logger.error(f"Get cash reconciliation session error: {e}")
        return internal_error_response("Failed to load reconciliation session")


@admin_bp.route("/staff/cash-reconciliation/sessions/<int:session_id>/verify", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def verify_cash_reconciliation_session(session_id):
    """Verify a driver reconciliation session."""
    try:
        actor_user_id = int(get_jwt_identity())
        data = request.get_json() or {}
        if data.get("verified_cash") is None:
            return validation_error_response("verified_cash is required")
        if not data.get("reason_code"):
            return validation_error_response("reason_code is required")

        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        session = DriverReconciliationService().verify_session(
            session_id=session_id,
            verified_cash=data.get("verified_cash"),
            actor_user_id=actor_user_id,
            reason_code=data.get("reason_code"),
            notes=data.get("notes"),
        )
        payload = DriverReconciliationService().get_session_detail(session.id)
        return success_response(data=payload)
    except NotFoundError:
        return not_found_response("Driver cash session not found")
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Verify cash reconciliation session error: {e}")
        return internal_error_response("Failed to verify reconciliation session")


@admin_bp.route("/staff/cash-reconciliation/events/<int:event_id>/adjust", methods=["POST"])
@jwt_required()
@super_admin_required
def adjust_cash_collection_event(event_id):
    """Adjust the amount of a recorded cash collection event."""
    try:
        actor_user_id = int(get_jwt_identity())
        data = request.get_json() or {}
        if data.get("new_amount") is None:
            return validation_error_response("new_amount is required")
        reason = (data.get("reason") or "").strip()
        if len(reason) < 5:
            return validation_error_response("reason must be at least 5 characters")

        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        replacement = CashCollectionService().adjust_event_amount(
            event_id,
            new_amount=data.get("new_amount"),
            adjusted_by_user_id=actor_user_id,
            reason=reason,
        )

        session_payload = None
        if replacement.driver_cash_session_id:
            session_payload = DriverReconciliationService().get_session_detail(replacement.driver_cash_session_id)

        original_event = CashCollectionEvent.query.get(event_id)
        return success_response(
            data={
                "cash_collection_event": replacement.to_dict(),
                "replaced_event": original_event.to_dict() if original_event else None,
                "driver_cash_session": session_payload,
            },
            status_code=200,
        )
    except NotFoundError as e:
        return not_found_response(str(e))
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Adjust cash collection event error: {e}")
        return internal_error_response("Failed to adjust cash collection event")


@admin_bp.route("/staff/cash-reconciliation/sessions/<int:session_id>/resolve", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def resolve_cash_reconciliation_session(session_id):
    """Resolve a mismatched or overdue reconciliation session."""
    try:
        actor_user_id = int(get_jwt_identity())
        data = request.get_json() or {}
        resolution_notes = data.get("resolution_notes")
        reason_code = data.get("reason_code")
        if not resolution_notes:
            return validation_error_response("resolution_notes is required")
        if not reason_code:
            return validation_error_response("reason_code is required")

        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        session = DriverReconciliationService().resolve_session(
            session_id=session_id,
            actor_user_id=actor_user_id,
            reason_code=reason_code,
            resolution_notes=resolution_notes,
            verified_cash=data.get("verified_cash"),
        )
        payload = DriverReconciliationService().get_session_detail(session.id)
        return success_response(data=payload)
    except NotFoundError:
        return not_found_response("Driver cash session not found")
    except ValidationError as e:
        return validation_error_response(e.message)
    except Exception as e:
        current_app.logger.error(f"Resolve cash reconciliation session error: {e}")
        return internal_error_response("Failed to resolve reconciliation session")


@admin_bp.route("/staff/cash-reconciliation/customers/<int:customer_id>/statement", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_customer_cod_statement_admin(customer_id):
    """Get COD statement for a customer."""
    try:
        from business_app.services.cash_collection_service import CashCollectionService

        statement = CashCollectionService().get_customer_cod_statement(customer_id)
        return success_response(data=statement)
    except NotFoundError:
        return not_found_response("Customer not found")
    except Exception as e:
        current_app.logger.error(f"Get customer COD statement error: {e}")
        return internal_error_response("Failed to load customer COD statement")


@admin_bp.route(
    "/staff/cash-reconciliation/customers/<int:customer_id>/prepayment-history",
    methods=["GET"],
)
@jwt_required()
@manager_or_higher_required
def get_customer_prepayment_history_admin(customer_id):
    """Get a customer's COD cash-collection ledger with allocations."""
    try:
        from business_app.services.cash_collection_service import CashCollectionService

        def _coerce_bool(value, default=True):
            if value is None:
                return default
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        include_voided = _coerce_bool(request.args.get("include_voided"), default=True)
        include_fully_applied = _coerce_bool(request.args.get("include_fully_applied"), default=True)
        limit = request.args.get("limit", 200, type=int)

        history = CashCollectionService().get_customer_prepayment_history(
            customer_id,
            include_voided=include_voided,
            include_fully_applied=include_fully_applied,
            limit=limit,
        )
        return success_response(data=history)
    except NotFoundError:
        return not_found_response("Customer not found")
    except Exception as e:
        current_app.logger.error(f"Get customer prepayment history error: {e}")
        return internal_error_response("Failed to load customer prepayment history")


@admin_bp.route("/staff/cash-reconciliation/orders/<int:order_id>/timeline", methods=["GET"])
@jwt_required()
@manager_or_higher_required
def get_order_payment_timeline_admin(order_id):
    """Get COD payment timeline for an order."""
    try:
        from business_app.services.cash_collection_service import CashCollectionService

        timeline = CashCollectionService().get_order_payment_timeline(order_id)
        return success_response(data=timeline)
    except NotFoundError:
        return not_found_response("Order not found")
    except Exception as e:
        current_app.logger.error(f"Get order payment timeline error: {e}")
        return internal_error_response("Failed to load order payment timeline")


@admin_bp.route("/staff/invite-link", methods=["POST"])
@jwt_required()
@manager_or_higher_required
def generate_staff_invite_link():
    """Generate a one-time staff bot invite link for first-time Telegram binding."""
    try:
        data = request.get_json() or {}
        user_id = data.get("user_id")
        role = data.get("role")

        # Get bot username from config
        bot_username = current_app.config.get("STAFF_BOT_USERNAME", "blue_stream_group_staff_bot")
        ttl_seconds = int(data.get("ttl_seconds", 900))

        if not user_id:
            return validation_error_response("user_id is required for one-time staff invite link")
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return validation_error_response("user_id must be an integer")

        user = User.query.get(user_id)
        if not user:
            return not_found_response("Staff user not found")

        staff_roles = user.staff_roles or []
        if role and role not in staff_roles:
            return validation_error_response("Requested role is not assigned to this user")
        if not staff_roles:
            return validation_error_response("User has no staff roles assigned")

        import secrets
        import json
        import redis

        redis_url = current_app.config.get("REDIS_URL")
        if not redis_url:
            return internal_error_response("REDIS_URL is not configured")

        invite_token = secrets.token_urlsafe(24)
        redis_client = redis.from_url(redis_url, decode_responses=True)

        try:
            redis_client.setex(
                RedisKeyspace.staff_bot_invite(invite_token),
                ttl_seconds,
                json.dumps(
                    {
                        "user_id": user.id,
                        "role": role,
                        "staff_roles": staff_roles,
                        "issued_by": get_jwt_identity(),
                        "issued_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
        finally:
            redis_client.close()

        invite_link = f"https://t.me/{bot_username}?start=staff_invite_{invite_token}"

        return success_response(
            data={
                "invite_link": invite_link,
                "invite_token": invite_token,
                "user_id": user.id,
                "role": role,
                "staff_roles": staff_roles,
                "expires_in_seconds": ttl_seconds,
                "bot_username": bot_username,
            }
        )

    except Exception as e:
        current_app.logger.error(f"Generate invite link error: {e}")
        return internal_error_response("Failed to generate invite link")


# =====================================================================
# Marking-code utilisation task: schedule, config, runs, pool status
# =====================================================================

from business_app.models.marking_code_task_run import (  # noqa: E402
    MarkingCodeRunStatus,
    MarkingCodeTaskRun,
)
from business_app.models.product import ProductFiscalProfile  # noqa: E402
from business_app.serializers.marking_code_admin import (  # noqa: E402
    MarkingCodeTaskConfigUpdate,
    MarkingCodeTaskRunTrigger,
    ProductMarkingCodeOverridesUpdate,
)
from business_app.services.marking_code_config_service import (  # noqa: E402
    MarkingCodeConfigService,
)
from business_app.services.marking_code_pool_service import (  # noqa: E402
    MarkingCodePoolService,
)


def _serialize_pool_product(product, profile, metrics, effective):
    """Compact row for the Pool Status UI tab."""
    overrides_present = any(
        getattr(profile, attr, None) is not None
        for attr in (
            "override_target_min",
            "override_target_max",
            "override_trend_window_days",
            "override_runway_days",
            "override_safety_multiplier",
            "override_low_water_ratio",
            "override_asl_belgisi_utilisation_api_chunk_size",
        )
    )
    return {
        "product_id": product.id,
        "product_name": getattr(product, "name", None),
        "pre_utilised": metrics["pre_utilised"],
        "un_utilised": metrics["un_utilised"],
        "reserved": metrics["reserved"],
        "target": metrics["target"],
        "deficit": metrics["deficit"],
        "has_overrides": overrides_present,
        "effective_config": effective,
        "overrides": {
            "target_min": profile.override_target_min,
            "target_max": profile.override_target_max,
            "trend_window_days": profile.override_trend_window_days,
            "runway_days": profile.override_runway_days,
            "safety_multiplier": (
                float(profile.override_safety_multiplier) if profile.override_safety_multiplier is not None else None
            ),
            "low_water_ratio": (
                float(profile.override_low_water_ratio) if profile.override_low_water_ratio is not None else None
            ),
            "asl_belgisi_utilisation_api_chunk_size": (profile.override_asl_belgisi_utilisation_api_chunk_size),
        },
    }


@admin_bp.route("/marking-code-task/config", methods=["GET"])
@jwt_required()
@admin_required
def get_marking_code_task_config():
    """Return the global config row + every fiscalisable product's overrides."""
    try:
        cfg = MarkingCodeConfigService().get_config()
        profiles = (
            db.session.query(ProductFiscalProfile, Product)
            .join(Product, Product.id == ProductFiscalProfile.product_id)
            .filter(ProductFiscalProfile.requires_marking_codes.is_(True))
            .order_by(Product.name.asc())
            .all()
        )
        products = []
        for profile, product in profiles:
            payload = profile.to_dict()
            payload["product_name"] = getattr(product, "name", None)
            products.append(payload)
        return success_response(
            data={
                "global": cfg.to_dict(),
                "products": products,
            }
        )
    except Exception as e:
        current_app.logger.error(f"Get marking-code task config error: {e}")
        return internal_error_response("Failed to load marking-code task config")


@admin_bp.route("/marking-code-task/config", methods=["PUT"])
@jwt_required()
@admin_required
def update_marking_code_task_config():
    """Partial update of the global config row. Bumps schedule_version if any
    schedule field changes, which triggers the beat container to reload."""
    try:
        payload = request.get_json() or {}
        try:
            parsed = MarkingCodeTaskConfigUpdate(**payload).model_dump(exclude_none=True)
        except PydanticValidationError as exc:
            return validation_error_response(exc.errors())

        cfg = MarkingCodeConfigService().update_config(parsed, actor_user_id=get_jwt_identity())
        return success_response(
            data={"global": cfg.to_dict()},
            message="Marking-code task config updated",
        )
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update marking-code task config error: {e}")
        return internal_error_response("Failed to update marking-code task config")


@admin_bp.route("/marking-code-task/config/products/<int:product_id>", methods=["PUT"])
@jwt_required()
@admin_required
def update_marking_code_product_overrides(product_id):
    """Set or clear per-product override columns on ProductFiscalProfile."""
    try:
        payload = request.get_json() or {}
        try:
            # ``model_dump`` with exclude_unset preserves explicit ``null`` values
            # in the JSON body (used to clear an override), while skipping keys
            # the client didn't send.
            parsed = ProductMarkingCodeOverridesUpdate(**payload).model_dump(exclude_unset=True)
        except PydanticValidationError as exc:
            return validation_error_response(exc.errors())

        profile = MarkingCodeConfigService().update_product_overrides(
            product_id,
            parsed,
            actor_user_id=get_jwt_identity(),
        )
        return success_response(
            data={"profile": profile.to_dict()},
            message="Product marking-code overrides updated",
        )
    except ValidationError as e:
        db.session.rollback()
        return validation_error_response(str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update marking-code product overrides error: {e}")
        return internal_error_response("Failed to update product overrides")


@admin_bp.route("/marking-code-task/runs", methods=["GET"])
@jwt_required()
@admin_required
def list_marking_code_task_runs():
    """Paginated execution ledger with optional filters."""
    try:
        page = max(1, request.args.get("page", 1, type=int) or 1)
        per_page = min(200, max(1, request.args.get("per_page", 25, type=int) or 25))

        query = MarkingCodeTaskRun.query
        task_name = request.args.get("task_name")
        if task_name:
            query = query.filter(MarkingCodeTaskRun.task_name == task_name)
        status = request.args.get("status")
        if status:
            try:
                query = query.filter(MarkingCodeTaskRun.status == MarkingCodeRunStatus(status))
            except ValueError:
                return validation_error_response(f"Invalid status: {status!r}")
        run_kind = request.args.get("run_kind")
        if run_kind:
            query = query.filter(MarkingCodeTaskRun.run_kind == run_kind)
        product_id = request.args.get("product_id", type=int)
        if product_id:
            query = query.filter(MarkingCodeTaskRun.product_id == product_id)
        started_after = request.args.get("started_after")
        if started_after:
            try:
                query = query.filter(MarkingCodeTaskRun.started_at >= datetime.fromisoformat(started_after))
            except ValueError:
                return validation_error_response("Invalid started_after (use ISO-8601)")
        started_before = request.args.get("started_before")
        if started_before:
            try:
                query = query.filter(MarkingCodeTaskRun.started_at <= datetime.fromisoformat(started_before))
            except ValueError:
                return validation_error_response("Invalid started_before (use ISO-8601)")

        total = query.count()
        rows = query.order_by(MarkingCodeTaskRun.started_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
        return paginated_response(
            items=[r.to_dict() for r in rows],
            page=page,
            per_page=per_page,
            total=total,
        )
    except Exception as e:
        current_app.logger.error(f"List marking-code task runs error: {e}")
        return internal_error_response("Failed to list marking-code task runs")


@admin_bp.route("/marking-code-task/runs/<int:run_id>", methods=["GET"])
@jwt_required()
@admin_required
def get_marking_code_task_run(run_id):
    """Single run with its children (when it's a parent fan-out row)."""
    try:
        row = MarkingCodeTaskRun.query.get(run_id)
        if row is None:
            return not_found_response(message="Run not found")
        return success_response(data={"run": row.to_dict(include_children=True)})
    except Exception as e:
        current_app.logger.error(f"Get marking-code task run error: {e}")
        return internal_error_response("Failed to fetch run")


@admin_bp.route("/marking-code-task/stats", methods=["GET"])
@jwt_required()
@admin_required
def get_marking_code_task_stats():
    """Aggregate run statistics over the last N days (default 7)."""
    try:
        days = max(1, min(90, request.args.get("days", 7, type=int) or 7))
        cutoff = datetime.now(UTC) - timedelta(days=days)

        base = MarkingCodeTaskRun.query.filter(MarkingCodeTaskRun.started_at >= cutoff)
        total = base.count()
        success = base.filter(MarkingCodeTaskRun.status == MarkingCodeRunStatus.SUCCESS).count()
        failed = base.filter(MarkingCodeTaskRun.status == MarkingCodeRunStatus.FAILED).count()
        skipped = base.filter(MarkingCodeTaskRun.status == MarkingCodeRunStatus.SKIPPED).count()
        running = base.filter(MarkingCodeTaskRun.status == MarkingCodeRunStatus.RUNNING).count()

        sums = base.with_entities(
            func.coalesce(func.sum(MarkingCodeTaskRun.requested), 0),
            func.coalesce(func.sum(MarkingCodeTaskRun.utilised), 0),
            func.coalesce(func.sum(MarkingCodeTaskRun.skipped_invalid), 0),
            func.coalesce(func.sum(MarkingCodeTaskRun.errors), 0),
        ).one()

        last_parent = (
            MarkingCodeTaskRun.query.filter(MarkingCodeTaskRun.task_name == "pre_register_marking_codes_daily")
            .order_by(MarkingCodeTaskRun.started_at.desc())
            .first()
        )

        return success_response(
            data={
                "window_days": days,
                "total_runs": total,
                "success": success,
                "failed": failed,
                "skipped": skipped,
                "running": running,
                "success_rate": (float(success) / total) if total > 0 else None,
                "totals": {
                    "requested": int(sums[0] or 0),
                    "utilised": int(sums[1] or 0),
                    "skipped_invalid": int(sums[2] or 0),
                    "errors": int(sums[3] or 0),
                },
                "last_daily_run": last_parent.to_dict() if last_parent else None,
            }
        )
    except Exception as e:
        current_app.logger.error(f"Get marking-code task stats error: {e}")
        return internal_error_response("Failed to load marking-code task stats")


@admin_bp.route("/marking-code-task/pool-status", methods=["GET"])
@jwt_required()
@admin_required
def get_marking_code_pool_status():
    """Per-product pool snapshot (capped at 200 products for safety)."""
    try:
        rows = (
            db.session.query(Product, ProductFiscalProfile)
            .join(ProductFiscalProfile, ProductFiscalProfile.product_id == Product.id)
            .filter(
                ProductFiscalProfile.requires_marking_codes.is_(True),
                ProductFiscalProfile.fiscalization_enabled.is_(True),
            )
            .order_by(Product.name.asc())
            .limit(200)
            .all()
        )
        pool_service = MarkingCodePoolService()
        config_service = MarkingCodeConfigService()
        items = []
        for product, profile in rows:
            try:
                metrics = pool_service.get_pool_metrics(product)
                effective = config_service.get_effective_for_product(product)
                items.append(_serialize_pool_product(product, profile, metrics, effective))
            except Exception:
                current_app.logger.exception(
                    "marking_code_pool_status: skipping product %s due to error",
                    product.id,
                )
        return success_response(data={"items": items})
    except Exception as e:
        current_app.logger.error(f"Get marking-code pool status error: {e}")
        return internal_error_response("Failed to load pool status")


@admin_bp.route("/marking-code-task/run", methods=["POST"])
@jwt_required()
@admin_required
def trigger_marking_code_task_run():
    """Manual trigger: fan-out for all products, or replenish one product."""
    try:
        payload = request.get_json() or {}
        try:
            parsed = MarkingCodeTaskRunTrigger(**payload)
        except PydanticValidationError as exc:
            return validation_error_response(exc.errors())

        actor = get_jwt_identity()

        # Late import to avoid Celery <-> Flask boot cycle.
        from business_app.tasks.marking_code_tasks import (
            pre_register_marking_codes_daily,
            replenish_marking_codes_for_product,
        )

        if parsed.scope == "all":
            async_result = pre_register_marking_codes_daily.delay(
                triggered_by_user_id=actor,
                run_kind="manual",
            )
            return created_response(
                data={
                    "task_id": async_result.id,
                    "scope": "all",
                },
                message="Daily fan-out enqueued",
            )

        if parsed.product_id is None:
            return validation_error_response("product_id is required when scope='product'")

        async_result = replenish_marking_codes_for_product.delay(
            int(parsed.product_id),
            "manual",
            None,
            actor,
        )
        return created_response(
            data={
                "task_id": async_result.id,
                "scope": "product",
                "product_id": parsed.product_id,
            },
            message="Per-product replenish enqueued",
        )
    except Exception as e:
        current_app.logger.error(f"Trigger marking-code task run error: {e}")
        return internal_error_response("Failed to enqueue marking-code task")
