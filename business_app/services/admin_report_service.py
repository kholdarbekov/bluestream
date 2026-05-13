"""Admin report generation service."""

from datetime import datetime, UTC
from decimal import Decimal
from typing import Any, Dict

from sqlalchemy import desc, func, or_

from business_app import db
from business_app.models.audit import AuditEventType, AuditLog
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, DriverCashSession, Payment
from business_app.models.product import Product
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.utils.api_responses import success_response
from shared.enums import (
    DeliveryStatus,
    DriverCashSessionStatus,
    PaymentMethod,
    PaymentStatus,
    SubscriptionFrequency,
    UserRole,
)
from business_app.utils.exceptions import ValidationError


class AdminReportService:
    """Service for admin report generation/formatting."""

    VALID_REPORTS = {
        "sales_summary",
        "customer_report",
        "product_performance",
        "delivery_report",
        "financial_summary",
        "user_activity",
        "inventory_report",
        "subscription_report",
        "loyalty_report",
    }

    @staticmethod
    def validate_report_type(report_type: str) -> None:
        if report_type not in AdminReportService.VALID_REPORTS:
            valid = ", ".join(sorted(AdminReportService.VALID_REPORTS))
            raise ValidationError(
                f"Invalid report type. Valid types: {valid}",
                error_code="INVALID_REPORT_TYPE",
            )

    @staticmethod
    def generate(report_type: str, start_dt: datetime, end_dt: datetime, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Generate report payload for requested report type."""
        AdminReportService.validate_report_type(report_type)
        filters = filters or {}

        if report_type == "sales_summary":
            return AdminReportService._generate_sales_summary_report(start_dt, end_dt, filters)
        if report_type == "customer_report":
            return AdminReportService._generate_customer_report(start_dt, end_dt, filters)
        if report_type == "product_performance":
            return AdminReportService._generate_product_performance_report(start_dt, end_dt, filters)
        if report_type == "delivery_report":
            return AdminReportService._generate_delivery_report(start_dt, end_dt, filters)
        if report_type == "financial_summary":
            return AdminReportService._generate_financial_summary_report(start_dt, end_dt, filters)
        if report_type == "user_activity":
            return AdminReportService._generate_user_activity_report(start_dt, end_dt, filters)
        if report_type == "inventory_report":
            return AdminReportService._generate_inventory_report(start_dt, end_dt, filters)
        if report_type == "subscription_report":
            return AdminReportService._generate_subscription_report(start_dt, end_dt, filters)
        if report_type == "loyalty_report":
            return AdminReportService._generate_loyalty_report(start_dt, end_dt, filters)

        raise ValidationError("Unsupported report type", error_code="UNSUPPORTED_REPORT_TYPE")

    @staticmethod
    def format_report(report_data: Dict[str, Any], report_type: str, format_type: str):
        """Format generated report output."""
        if format_type == "csv":
            return AdminReportService._format_report_as_csv(report_data, report_type)
        if format_type == "excel":
            return AdminReportService._format_report_as_excel(report_data, report_type)
        return success_response(
            data={"report": report_data},
            message=f"{report_type} report generated successfully",
        )

    @staticmethod
    def _generate_sales_summary_report(start_dt, end_dt, filters):
        """Generate sales summary report."""
        orders_query = Order.query.filter(Order.created_at >= start_dt, Order.created_at <= end_dt)

        if filters.get("status"):
            orders_query = orders_query.filter_by(status=filters["status"])

        total_orders = orders_query.count()
        total_revenue = (
            db.session.query(func.sum(Order.total_amount))
            .filter(
                Order.created_at >= start_dt, Order.created_at <= end_dt, Order.status.in_(["delivered", "completed"])
            )
            .scalar()
            or 0
        )

        avg_order_value = total_revenue / total_orders if total_orders > 0 else 0

        orders_by_status = (
            db.session.query(Order.status, func.count(Order.id), func.sum(Order.total_amount))
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .group_by(Order.status)
            .all()
        )

        status_breakdown = [
            {"status": status, "count": count, "revenue": float(revenue or 0)}
            for status, count, revenue in orders_by_status
        ]

        top_products = (
            db.session.query(
                Product.id,
                Product.name,
                func.count(OrderItem.id).label("order_count"),
                func.sum(OrderItem.quantity).label("total_quantity"),
                func.sum(OrderItem.total_price).label("total_revenue"),
            )
            .join(OrderItem, Product.id == OrderItem.product_id)
            .join(Order, OrderItem.order_id == Order.id)
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .group_by(Product.id, Product.name)
            .order_by(desc("total_revenue"))
            .limit(10)
            .all()
        )

        top_products_list = [
            {
                "product_id": prod_id,
                "product_name": name,
                "order_count": order_count,
                "total_quantity": total_quantity,
                "total_revenue": float(total_revenue),
            }
            for prod_id, name, order_count, total_quantity, total_revenue in top_products
        ]

        daily_sales = (
            db.session.query(
                func.date(Order.created_at).label("date"),
                func.count(Order.id).label("orders"),
                func.sum(Order.total_amount).label("revenue"),
            )
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .group_by("date")
            .order_by("date")
            .all()
        )

        sales_trend = [
            {"date": date.isoformat() if date else None, "orders": orders, "revenue": float(revenue or 0)}
            for date, orders, revenue in daily_sales
        ]

        return {
            "summary": {
                "total_orders": total_orders,
                "total_revenue": float(total_revenue),
                "average_order_value": round(avg_order_value, 2),
                "period_days": (end_dt - start_dt).days,
            },
            "status_breakdown": status_breakdown,
            "top_products": top_products_list,
            "sales_trend": sales_trend,
        }

    @staticmethod
    def _generate_customer_report(start_dt, end_dt, filters):
        """Generate customer activity report."""
        new_customers = User.query.filter(
            User.created_at >= start_dt, User.created_at <= end_dt, User.role == UserRole.CUSTOMER
        ).count()

        active_customers = (
            db.session.query(func.count(func.distinct(Order.user_id)))
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .scalar()
            or 0
        )

        top_customers = (
            db.session.query(
                User.id,
                User.name,
                User.email,
                func.count(Order.id).label("order_count"),
                func.sum(Order.total_amount).label("total_spent"),
            )
            .join(Order, User.id == Order.user_id)
            .filter(Order.created_at >= start_dt, Order.created_at <= end_dt)
            .group_by(User.id, User.name, User.email)
            .order_by(desc("total_spent"))
            .limit(20)
            .all()
        )

        top_customers_list = [
            {
                "user_id": user_id,
                "name": name,
                "email": email,
                "order_count": order_count,
                "total_spent": float(total_spent),
            }
            for user_id, name, email, order_count, total_spent in top_customers
        ]

        daily_signups = (
            db.session.query(func.date(User.created_at).label("date"), func.count(User.id).label("signups"))
            .filter(User.created_at >= start_dt, User.created_at <= end_dt, User.role == UserRole.CUSTOMER)
            .group_by("date")
            .order_by("date")
            .all()
        )

        acquisition_trend = [
            {"date": date.isoformat() if date else None, "signups": signups} for date, signups in daily_signups
        ]

        return {
            "summary": {
                "new_customers": new_customers,
                "active_customers": active_customers,
                "total_customers": User.query.filter_by(role=UserRole.CUSTOMER).count(),
            },
            "top_customers": top_customers_list,
            "acquisition_trend": acquisition_trend,
        }

    @staticmethod
    def _generate_product_performance_report(start_dt, end_dt, filters):
        """Generate product performance report."""
        return AdminReportService._generate_sales_summary_report(start_dt, end_dt, filters)

    @staticmethod
    def _generate_delivery_report(start_dt, end_dt, filters):
        """Generate delivery performance report."""
        deliveries = Delivery.query.filter(Delivery.created_at >= start_dt, Delivery.created_at <= end_dt)

        total_deliveries = deliveries.count()

        by_status = (
            db.session.query(Delivery.status, func.count(Delivery.id))
            .filter(Delivery.created_at >= start_dt, Delivery.created_at <= end_dt)
            .group_by(Delivery.status)
            .all()
        )

        status_breakdown = [{"status": status, "count": count} for status, count in by_status]

        on_time = Delivery.query.filter(
            Delivery.created_at >= start_dt,
            Delivery.created_at <= end_dt,
            Delivery.status == DeliveryStatus.DELIVERED,
            Delivery.delivered_at <= Delivery.scheduled_delivery_time,
        ).count()

        on_time_rate = (on_time / total_deliveries * 100) if total_deliveries > 0 else 0

        top_personnel = (
            db.session.query(
                DeliveryPerson.id,
                DeliveryPerson.name,
                func.count(Delivery.id).label("delivery_count"),
                func.avg(func.extract("epoch", Delivery.delivered_at - Delivery.created_at) / 3600).label(
                    "avg_delivery_time_hours"
                ),
            )
            .join(Delivery, DeliveryPerson.id == Delivery.delivery_person_id)
            .filter(
                Delivery.created_at >= start_dt,
                Delivery.created_at <= end_dt,
                Delivery.status == DeliveryStatus.DELIVERED,
            )
            .group_by(DeliveryPerson.id, DeliveryPerson.name)
            .order_by(desc("delivery_count"))
            .limit(10)
            .all()
        )

        personnel_list = [
            {
                "person_id": person_id,
                "name": name,
                "delivery_count": delivery_count,
                "avg_delivery_time_hours": round(float(avg_time or 0), 2),
            }
            for person_id, name, delivery_count, avg_time in top_personnel
        ]

        return {
            "summary": {
                "total_deliveries": total_deliveries,
                "on_time_rate": round(on_time_rate, 2),
                "on_time_count": on_time,
            },
            "status_breakdown": status_breakdown,
            "top_personnel": personnel_list,
        }

    @staticmethod
    def _generate_financial_summary_report(start_dt, end_dt, filters):
        """Generate financial summary report from ledger/session truth."""
        settled_at = func.coalesce(Payment.paid_at, Payment.created_at)

        electronic_total = (
            db.session.query(func.sum(Payment.amount))
            .filter(
                settled_at >= start_dt,
                settled_at <= end_dt,
                Payment.payment_method != PaymentMethod.CASH,
                Payment.status == PaymentStatus.COMPLETED,
            )
            .scalar()
            or 0
        )

        electronic_by_method = (
            db.session.query(
                Payment.payment_method,
                func.count(Payment.id),
                func.sum(Payment.amount),
            )
            .filter(
                settled_at >= start_dt,
                settled_at <= end_dt,
                Payment.payment_method != PaymentMethod.CASH,
                Payment.status == PaymentStatus.COMPLETED,
            )
            .group_by(Payment.payment_method)
            .all()
        )

        cash_allocations_query = (
            db.session.query(
                CashCollectionEvent.source,
                func.count(func.distinct(CashCollectionEvent.id)),
                func.sum(CashCollectionAllocation.allocated_amount),
            )
            .join(
                CashCollectionAllocation,
                CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
            )
            .filter(
                CashCollectionEvent.occurred_at >= start_dt,
                CashCollectionEvent.occurred_at <= end_dt,
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionAllocation.reversed_at.is_(None),
            )
            .group_by(CashCollectionEvent.source)
            .all()
        )

        cash_collected_total = sum(Decimal(str(amount or 0)) for _source, _count, amount in cash_allocations_query)

        delivered_revenue_total = (
            db.session.query(func.sum(Order.total_amount))
            .join(Delivery, Delivery.order_id == Order.id)
            .filter(
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.delivered_at >= start_dt,
                Delivery.delivered_at <= end_dt,
            )
            .scalar()
            or 0
        )

        total_refunds = (
            db.session.query(func.sum(Payment.amount))
            .filter(
                settled_at >= start_dt,
                settled_at <= end_dt,
                Payment.status == PaymentStatus.REFUNDED,
            )
            .scalar()
            or 0
        )

        outstanding_rows = (
            db.session.query(Payment.outstanding_amount, Delivery.delivered_at)
            .join(Order, Payment.order_id == Order.id)
            .join(Delivery, Delivery.order_id == Order.id)
            .filter(
                Payment.payment_method == PaymentMethod.CASH,
                Payment.outstanding_amount > 0,
                Delivery.status == DeliveryStatus.DELIVERED,
            )
            .all()
        )

        outstanding_total = sum(Decimal(str(amount or 0)) for amount, _ in outstanding_rows)
        aging = {"0_7_days": 0.0, "8_30_days": 0.0, "31_plus_days": 0.0}
        now = datetime.now(UTC)
        for amount, delivered_at in outstanding_rows:
            bucket_amount = float(amount or 0)
            if delivered_at and delivered_at.tzinfo is None:
                delivered_at = delivered_at.replace(tzinfo=UTC)
            age_days = (now - delivered_at).days if delivered_at else 0
            if age_days <= 7:
                aging["0_7_days"] += bucket_amount
            elif age_days <= 30:
                aging["8_30_days"] += bucket_amount
            else:
                aging["31_plus_days"] += bucket_amount

        daily_collected = (
            db.session.query(
                func.date(CashCollectionEvent.occurred_at).label("date"),
                func.sum(CashCollectionAllocation.allocated_amount).label("revenue"),
            )
            .join(
                CashCollectionAllocation,
                CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
            )
            .filter(
                CashCollectionEvent.occurred_at >= start_dt,
                CashCollectionEvent.occurred_at <= end_dt,
                CashCollectionEvent.voided_at.is_(None),
                CashCollectionAllocation.reversed_at.is_(None),
            )
            .group_by("date")
            .order_by("date")
            .all()
        )

        daily_delivered_revenue = (
            db.session.query(
                func.date(Delivery.delivered_at).label("date"),
                func.sum(Order.total_amount).label("revenue"),
            )
            .join(Order, Delivery.order_id == Order.id)
            .filter(
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.delivered_at >= start_dt,
                Delivery.delivered_at <= end_dt,
            )
            .group_by("date")
            .order_by("date")
            .all()
        )

        reconciliation_summary = (
            db.session.query(
                func.count(DriverCashSession.id),
                func.coalesce(func.sum(DriverCashSession.expected_cash), 0),
                func.count().filter(DriverCashSession.status == DriverCashSessionStatus.SUBMITTED),
                func.count().filter(DriverCashSession.status == DriverCashSessionStatus.MISMATCH),
                func.count().filter(DriverCashSession.status == DriverCashSessionStatus.OVERDUE),
                func.count().filter(
                    DriverCashSession.status == DriverCashSessionStatus.OVERDUE,
                    DriverCashSession.blocked_from_cod.is_(False),
                ),
            )
            .filter(
                DriverCashSession.business_date <= end_dt.date(),
                or_(
                    DriverCashSession.session_ended_at.is_(None),
                    func.date(DriverCashSession.session_ended_at) >= start_dt.date(),
                ),
            )
            .one()
        )

        method_breakdown = [
            {
                "method": method.value if hasattr(method, "value") else method,
                "count": count,
                "amount": float(amount or 0),
            }
            for method, count, amount in electronic_by_method
        ]
        method_breakdown.append(
            {
                "method": PaymentMethod.CASH.value,
                "count": sum(int(count or 0) for _source, count, _amount in cash_allocations_query),
                "amount": float(cash_collected_total),
            }
        )

        cash_collection_source_breakdown = [
            {
                "source": source.value if hasattr(source, "value") else source,
                "count": int(count or 0),
                "amount": float(amount or 0),
            }
            for source, count, amount in cash_allocations_query
        ]

        revenue_trend = [
            {
                "date": date.isoformat() if hasattr(date, "isoformat") else (str(date) if date else None),
                "revenue": float(revenue or 0),
            }
            for date, revenue in daily_collected
        ]

        delivered_revenue_trend = [
            {
                "date": date.isoformat() if hasattr(date, "isoformat") else (str(date) if date else None),
                "revenue": float(revenue or 0),
            }
            for date, revenue in daily_delivered_revenue
        ]

        total_collected = Decimal(str(electronic_total or 0)) + cash_collected_total

        return {
            "summary": {
                "total_revenue": float(total_collected),
                "total_cash_collected": float(cash_collected_total),
                "total_electronic_collected": float(electronic_total or 0),
                "delivered_order_revenue": float(delivered_revenue_total),
                "total_refunds": float(total_refunds),
                "net_revenue": float(total_collected - Decimal(str(total_refunds or 0))),
                "outstanding_cod_total": float(outstanding_total),
                "outstanding_cod_count": len(outstanding_rows),
            },
            "payment_method_breakdown": method_breakdown,
            "cash_collection_source_breakdown": cash_collection_source_breakdown,
            "revenue_trend": revenue_trend,
            "delivered_revenue_trend": delivered_revenue_trend,
            "outstanding_cod_aging": aging,
            "reconciliation_summary": {
                "session_count": int(reconciliation_summary[0] or 0),
                "expected_cash_total": float(reconciliation_summary[1] or 0),
                "submitted_session_count": int(reconciliation_summary[2] or 0),
                "mismatch_session_count": int(reconciliation_summary[3] or 0),
                "overdue_session_count": int(reconciliation_summary[4] or 0),
                "warning_session_count": int(reconciliation_summary[5] or 0),
            },
        }

    @staticmethod
    def _generate_user_activity_report(start_dt, end_dt, filters):
        """Generate user activity report."""
        login_events = AuditLog.query.filter(
            AuditLog.created_at >= start_dt,
            AuditLog.created_at <= end_dt,
            AuditLog.event_type == AuditEventType.LOGIN_SUCCESS,
        ).count()

        active_users = (
            db.session.query(func.count(func.distinct(AuditLog.user_id)))
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt, AuditLog.user_id.isnot(None))
            .scalar()
            or 0
        )

        top_active = (
            db.session.query(User.id, User.name, User.email, func.count(AuditLog.id).label("activity_count"))
            .join(AuditLog, User.id == AuditLog.user_id)
            .filter(AuditLog.created_at >= start_dt, AuditLog.created_at <= end_dt)
            .group_by(User.id, User.name, User.email)
            .order_by(desc("activity_count"))
            .limit(20)
            .all()
        )

        active_users_list = [
            {"user_id": user_id, "name": name, "email": email, "activity_count": activity_count}
            for user_id, name, email, activity_count in top_active
        ]

        return {
            "summary": {"total_logins": login_events, "active_users": active_users},
            "most_active_users": active_users_list,
        }

    @staticmethod
    def _generate_inventory_report(start_dt, end_dt, filters):
        """Generate inventory status report."""
        products = Product.query.filter_by(is_active=True).all()

        inventory_data = []
        low_stock_items = []

        for product in products:
            stock_level = product.stock_quantity or 0

            item_data = {
                "product_id": product.id,
                "product_name": product.name,
                "current_stock": stock_level,
                "is_active": product.is_active,
            }

            inventory_data.append(item_data)

            if stock_level < 10:
                low_stock_items.append(item_data)

        return {
            "summary": {
                "total_products": len(products),
                "low_stock_count": len(low_stock_items),
                "out_of_stock_count": sum(1 for p in products if (p.stock_quantity or 0) == 0),
            },
            "inventory": inventory_data[:100],
            "low_stock_items": low_stock_items,
        }

    @staticmethod
    def _generate_subscription_report(start_dt, end_dt, filters):
        """Generate subscription report."""
        active_subs = Subscription.query.filter_by(status="active").count()

        new_subs = Subscription.query.filter(
            Subscription.created_at >= start_dt, Subscription.created_at <= end_dt
        ).count()

        by_status = (
            db.session.query(Subscription.status, func.count(Subscription.id), func.sum(Subscription.billing_amount))
            .group_by(Subscription.status)
            .all()
        )

        status_breakdown = [
            {"status": status, "count": count, "total_value": float(total_value or 0)}
            for status, count, total_value in by_status
        ]

        mrr = (
            db.session.query(func.sum(Subscription.billing_amount))
            .filter_by(status="active", billing_cycle=SubscriptionFrequency.MONTHLY)
            .scalar()
            or 0
        )

        return {
            "summary": {
                "active_subscriptions": active_subs,
                "new_subscriptions": new_subs,
                "monthly_recurring_revenue": float(mrr),
            },
            "status_breakdown": status_breakdown,
        }

    @staticmethod
    def _generate_loyalty_report(start_dt, end_dt, filters):
        """Generate loyalty program report."""
        total_points = db.session.query(func.sum(User.loyalty_points)).filter_by(role=UserRole.CUSTOMER).scalar() or 0

        top_members = (
            db.session.query(User.id, User.name, User.email, User.loyalty_points)
            .filter(User.role == UserRole.CUSTOMER, User.loyalty_points > 0)
            .order_by(User.loyalty_points.desc())
            .limit(20)
            .all()
        )

        top_members_list = [
            {"user_id": user_id, "name": name, "email": email, "points": points}
            for user_id, name, email, points in top_members
        ]

        return {
            "summary": {"total_points_in_system": total_points, "members_with_points": len(top_members)},
            "top_members": top_members_list,
        }

    @staticmethod
    def _format_report_as_csv(report_data, report_type):
        """Format report as CSV."""
        import csv
        from io import StringIO

        output = StringIO()

        if "summary" in report_data:
            writer = csv.DictWriter(output, fieldnames=report_data["summary"].keys())
            writer.writeheader()
            writer.writerow(report_data["summary"])

        csv_data = output.getvalue()

        return success_response(data={"report": csv_data, "format": "csv", "metadata": report_data.get("metadata", {})})

    @staticmethod
    def _format_report_as_excel(report_data, report_type):
        """Format report as Excel (placeholder)."""
        return success_response(
            data={
                "report": report_data,
                "format": "excel",
                "note": "Excel format requires openpyxl library - returning JSON format",
            }
        )
