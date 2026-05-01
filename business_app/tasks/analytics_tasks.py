"""
Analytics-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/analytics_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

from business_app.models.analytics import AnalyticsReport, UserBehavior, SalesMetric
from business_app.models.user import User
from business_app.models.order import Order
from business_app.models.delivery import Delivery
from business_app.services.analytics_service import AnalyticsService
from business_app.services.notification_service import NotificationService
from shared.enums import UserRole
from business_app import db

logger = get_task_logger(__name__)


@shared_task(time_limit=3600, soft_time_limit=3300)
def generate_daily_analytics_report():
    """Generate daily analytics report"""
    try:
        logger.info("Generating daily analytics report")

        analytics_service = AnalyticsService()

        # Generate comprehensive daily report
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=1)

        report_data = analytics_service.generate_business_report("daily", start_date, end_date)

        # Store report in database
        report = AnalyticsReport(
            report_type="daily",
            period_start=start_date,
            period_end=end_date,
            data=report_data,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        # Send report to management
        admin_users = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.is_active == True).all()

        notification_service = NotificationService()

        for admin in admin_users:
            notification_service.send_notification(
                admin.id,
                "daily_report",
                template_data={
                    "report_date": start_date.date().isoformat(),
                    "total_revenue": report_data["overview"]["revenue"]["total_revenue"],
                    "total_orders": report_data["overview"]["orders"]["total_orders"],
                    "new_customers": report_data["overview"]["customers"]["new_customers"],
                },
            )

        logger.info(f"Daily analytics report generated for {start_date.date()}")
        return {"success": True, "report_id": report.id, "period": start_date.date().isoformat()}

    except Exception as e:
        logger.error(f"Failed to generate daily analytics report: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def generate_weekly_business_report():
    """Generate weekly business report"""
    try:
        logger.info("Generating weekly business report")

        analytics_service = AnalyticsService()

        # Generate weekly report (last 7 days)
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=7)

        report_data = analytics_service.generate_business_report("weekly", start_date, end_date)

        # Store report
        report = AnalyticsReport(
            report_type="weekly",
            period_start=start_date,
            period_end=end_date,
            data=report_data,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        # Generate insights and recommendations
        insights = generate_business_insights(report_data)

        # Send comprehensive report to management
        admin_users = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.is_active == True).all()

        notification_service = NotificationService()

        for admin in admin_users:
            notification_service.send_notification(
                admin.id,
                "weekly_business_report",
                template_data={
                    "week_ending": end_date.date().isoformat(),
                    "total_revenue": report_data["overview"]["revenue"]["total_revenue"],
                    "revenue_growth": report_data["overview"]["revenue"]["growth_rate"],
                    "total_orders": report_data["overview"]["orders"]["total_orders"],
                    "customer_acquisition": report_data["overview"]["customers"]["new_customers"],
                    "key_insights": insights[:3],  # Top 3 insights
                },
            )

        logger.info(f"Weekly business report generated for week ending {end_date.date()}")
        return {"success": True, "report_id": report.id, "insights_count": len(insights)}

    except Exception as e:
        logger.error(f"Failed to generate weekly business report: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, time_limit=3600, soft_time_limit=3300)
def track_user_activity_task(
    self, user_id: int, activity_type: str, endpoint: str, method: str, ip_address: str, user_agent: str
):
    """Track user activity for analytics"""
    try:
        logger.info(f"Tracking user activity: {activity_type} for user {user_id}")

        # Store user behavior data
        behavior = UserBehavior(
            user_id=user_id,
            action=activity_type,
            endpoint=endpoint,
            method=method,
            ip_address=ip_address,
            user_agent=user_agent,
            timestamp=datetime.now(timezone.utc),
            metadata={"endpoint": endpoint, "method": method},
        )

        db.session.add(behavior)
        db.session.commit()

        # Trigger real-time analytics updates if needed
        if activity_type in ["order_placed", "payment_completed", "user_registered"]:
            update_real_time_metrics.delay(activity_type, user_id)

        logger.info(f"User activity tracked: {activity_type}")
        return {"success": True, "activity_type": activity_type}

    except Exception as exc:
        logger.error(f"Failed to track user activity: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=3600, soft_time_limit=3300)
def update_real_time_metrics(activity_type: str, user_id: int = None):
    """Update real-time business metrics"""
    try:
        logger.info(f"Updating real-time metrics for activity: {activity_type}")

        today = datetime.now(timezone.utc).date()

        # Get or create today's metrics
        sales_metric = SalesMetric.query.filter_by(date=today).first()

        if not sales_metric:
            sales_metric = SalesMetric(date=today, total_orders=0, total_revenue=0, new_customers=0, active_customers=0)
            db.session.add(sales_metric)

        # Update metrics based on activity type
        if activity_type == "order_placed":
            sales_metric.total_orders += 1

            # Update revenue if order has amount
            if user_id:
                latest_order = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).first()
                if latest_order:
                    sales_metric.total_revenue += latest_order.total_amount

        elif activity_type == "user_registered":
            sales_metric.new_customers += 1

        elif activity_type == "payment_completed":
            # Revenue already updated in order_placed, but we can update other metrics
            pass

        # Update active customers count
        active_customers_today = (
            db.session.query(Order.user_id).filter(db.func.date(Order.created_at) == today).distinct().count()
        )

        sales_metric.active_customers = active_customers_today
        sales_metric.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        logger.info(f"Real-time metrics updated for {activity_type}")
        return {"success": True, "metric_date": today.isoformat()}

    except Exception as e:
        logger.error(f"Failed to update real-time metrics: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def update_customer_segments():
    """Update customer segments based on behavior and purchase patterns"""
    try:
        logger.info("Updating customer segments")

        segment_updates = {"high_value": 0, "medium_value": 0, "low_value": 0, "at_risk": 0, "new": 0}

        # Single aggregate query instead of 3N+1 per-user queries
        customer_metrics = (
            db.session.query(
                User.id.label("user_id"),
                db.func.coalesce(
                    db.func.sum(db.case((Order.status != "cancelled", Order.total_amount), else_=0)), 0
                ).label("total_spent"),
                db.func.count(Order.id).label("order_count"),
                db.func.max(Order.created_at).label("last_order_date"),
            )
            .outerjoin(Order, Order.user_id == User.id)
            .filter(User.is_active == True)
            .group_by(User.id)
            .all()
        )

        now = datetime.now(timezone.utc)
        for row in customer_metrics:
            try:
                total_spent = float(row.total_spent or 0)
                order_count = row.order_count
                days_since_last_order = 999

                if row.last_order_date:
                    days_since_last_order = (now - row.last_order_date).days

                # Determine segment
                if total_spent >= 100000:  # High value: >100k UZS
                    segment = "high_value"
                elif total_spent >= 25000:  # Medium value: 25k-100k UZS
                    segment = "medium_value"
                elif order_count > 0:
                    if days_since_last_order > 30:  # At risk: no order in 30 days
                        segment = "at_risk"
                    else:
                        segment = "low_value"
                else:  # New customers with no orders
                    segment = "new"

                # Update customer segment
                User.query.filter_by(id=row.user_id).update(
                    {"customer_segment": segment, "segment_updated_at": now}, synchronize_session=False
                )

                segment_updates[segment] += 1

            except Exception as e:
                logger.error(f"Failed to update segment for customer {row.user_id}: {e}")
                continue

        db.session.commit()

        logger.info(f"Customer segments updated: {segment_updates}")
        return segment_updates

    except Exception as e:
        logger.error(f"Failed to update customer segments: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def generate_churn_prediction_report():
    """Generate customer churn prediction report"""
    try:
        logger.info("Generating churn prediction report")

        analytics_service = AnalyticsService()

        # Get churn predictions for all customers
        churn_predictions = analytics_service.predict_customer_churn()

        if "error" in churn_predictions:
            logger.error(f"Churn prediction failed: {churn_predictions['error']}")
            return churn_predictions

        # Store churn analysis results
        report_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "high_risk_customers": churn_predictions["high_risk_customers"],
            "medium_risk_customers": churn_predictions["medium_risk_customers"],
            "predictions": churn_predictions["predictions"],
        }

        report = AnalyticsReport(
            report_type="churn_prediction",
            period_start=datetime.now(timezone.utc) - timedelta(days=30),
            period_end=datetime.now(timezone.utc),
            data=report_data,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        # Send alerts for high-risk customers
        high_risk_customers = [p for p in churn_predictions["predictions"] if p["risk_level"] == "high"]

        if high_risk_customers:
            # Send alert to management
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.is_active == True
            ).all()

            notification_service = NotificationService()

            for admin in admin_users:
                notification_service.send_notification(
                    admin.id,
                    "churn_alert",
                    template_data={
                        "high_risk_count": len(high_risk_customers),
                        "total_at_risk": churn_predictions["high_risk_customers"]
                        + churn_predictions["medium_risk_customers"],
                        "top_customers": high_risk_customers[:5],  # Top 5 at-risk customers
                    },
                )

        logger.info(f"Churn prediction report generated: {len(high_risk_customers)} high-risk customers identified")
        return {"success": True, "report_id": report.id, "high_risk_customers": len(high_risk_customers)}

    except Exception as e:
        logger.error(f"Failed to generate churn prediction report: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def generate_demand_forecast():
    """Generate demand forecast for inventory planning"""
    try:
        logger.info("Generating demand forecast")

        analytics_service = AnalyticsService()

        # Generate 30-day demand forecast
        forecast = analytics_service.predict_demand(30)

        if "error" in forecast:
            logger.error(f"Demand forecast failed: {forecast['error']}")
            return forecast

        # Store forecast results
        report_data = {
            "forecast_period_days": 30,
            "model_accuracy": forecast.get("model_accuracy"),
            "historical_avg": forecast.get("historical_avg"),
            "predictions": forecast["predictions"],
        }

        report = AnalyticsReport(
            report_type="demand_forecast",
            period_start=datetime.now(timezone.utc),
            period_end=datetime.now(timezone.utc) + timedelta(days=30),
            data=report_data,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        # Calculate total predicted demand
        total_predicted_orders = sum(p["predicted_orders"] for p in forecast["predictions"])

        # Send forecast to operations team
        admin_users = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.is_active == True).all()

        notification_service = NotificationService()

        for admin in admin_users:
            notification_service.send_notification(
                admin.id,
                "demand_forecast",
                template_data={
                    "forecast_period": "30 days",
                    "total_predicted_orders": total_predicted_orders,
                    "daily_average": round(total_predicted_orders / 30, 1),
                    "model_accuracy": forecast.get("model_accuracy", "N/A"),
                },
            )

        logger.info(f"Demand forecast generated: {total_predicted_orders} orders predicted for next 30 days")
        return {"success": True, "report_id": report.id, "total_predicted_orders": total_predicted_orders}

    except Exception as e:
        logger.error(f"Failed to generate demand forecast: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def cleanup_old_analytics_data():
    """Clean up old analytics data to manage database size"""
    try:
        logger.info("Cleaning up old analytics data")

        # Delete user behavior data older than 6 months
        behavior_cutoff = datetime.now(timezone.utc) - timedelta(days=180)
        deleted_behaviors = UserBehavior.query.filter(UserBehavior.timestamp < behavior_cutoff).delete()

        # Delete old analytics reports (keep for 2 years)
        report_cutoff = datetime.now(timezone.utc) - timedelta(days=730)
        deleted_reports = AnalyticsReport.query.filter(AnalyticsReport.generated_at < report_cutoff).delete()

        # Archive old sales metrics (keep for 3 years)
        metrics_cutoff = datetime.now(timezone.utc) - timedelta(days=1095)
        old_metrics = SalesMetric.query.filter(SalesMetric.date < metrics_cutoff.date()).all()

        archived_metrics = 0
        for metric in old_metrics:
            metric.is_archived = True
            archived_metrics += 1

        db.session.commit()

        cleanup_results = {
            "deleted_behaviors": deleted_behaviors,
            "deleted_reports": deleted_reports,
            "archived_metrics": archived_metrics,
        }

        logger.info(f"Analytics cleanup completed: {cleanup_results}")
        return cleanup_results

    except Exception as e:
        logger.error(f"Failed to clean up analytics data: {e}")
        db.session.rollback()
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def calculate_customer_lifetime_value():
    """Calculate and update customer lifetime value metrics"""
    try:
        logger.info("Calculating customer lifetime value")

        analytics_service = AnalyticsService()
        clv_data = analytics_service._get_customer_lifetime_value_analysis()

        # Update individual customer CLV scores
        customers = User.query.filter_by(is_active=True).all()
        updated_customers = 0

        for customer in customers:
            try:
                # Calculate individual CLV
                customer_orders = Order.query.filter(Order.user_id == customer.id, Order.status != "cancelled").all()

                if customer_orders:
                    total_value = sum(order.total_amount for order in customer_orders)
                    order_count = len(customer_orders)

                    # Calculate customer lifespan
                    first_order = min(customer_orders, key=lambda x: x.created_at)
                    last_order = max(customer_orders, key=lambda x: x.created_at)
                    lifespan_days = max(1, (last_order.created_at - first_order.created_at).days)

                    # Simple CLV calculation
                    avg_order_value = total_value / order_count
                    purchase_frequency = order_count / (lifespan_days / 30)  # orders per month

                    # Estimate future value (simplified)
                    estimated_clv = avg_order_value * purchase_frequency * 12  # Annual CLV

                    # Update customer record
                    customer.lifetime_value = total_value
                    customer.estimated_clv = estimated_clv
                    customer.clv_updated_at = datetime.now(timezone.utc)

                    updated_customers += 1

            except Exception as e:
                logger.error(f"Failed to calculate CLV for customer {customer.id}: {e}")
                continue

        db.session.commit()

        logger.info(f"CLV calculated for {updated_customers} customers")
        return {
            "updated_customers": updated_customers,
            "average_clv": clv_data["average_clv"],
            "total_customers": clv_data["total_customers"],
        }

    except Exception as e:
        logger.error(f"Failed to calculate customer lifetime value: {e}")
        return {"error": str(e)}


def generate_business_insights(report_data: Dict[str, Any]) -> List[str]:
    """Generate business insights from report data"""
    insights = []

    try:
        # Revenue insights
        revenue_growth = report_data["overview"]["revenue"].get("growth_rate", 0)
        if revenue_growth > 10:
            insights.append(f"Strong revenue growth of {revenue_growth:.1f}% indicates healthy business expansion")
        elif revenue_growth < -5:
            insights.append(
                f"Revenue declined by {abs(revenue_growth):.1f}% - investigate market factors and customer satisfaction"
            )

        # Order insights
        completion_rate = report_data["overview"]["orders"].get("completion_rate", 0)
        if completion_rate < 85:
            insights.append(
                f"Order completion rate of {completion_rate:.1f}% is below target - review fulfillment processes"
            )

        # Customer insights
        repeat_rate = report_data["overview"]["customers"].get("repeat_rate", 0)
        if repeat_rate > 40:
            insights.append(f"High customer retention rate of {repeat_rate:.1f}% shows strong customer loyalty")
        elif repeat_rate < 20:
            insights.append(f"Low repeat rate of {repeat_rate:.1f}% suggests need for customer retention programs")

        # Delivery insights
        delivery_success_rate = report_data["overview"]["delivery"].get("success_rate", 0)
        if delivery_success_rate < 95:
            insights.append(
                f"Delivery success rate of {delivery_success_rate:.1f}% needs improvement - review delivery operations"
            )

        avg_delivery_time = report_data["overview"]["delivery"].get("average_delivery_time_hours", 0)
        if avg_delivery_time > 4:
            insights.append(
                f"Average delivery time of {avg_delivery_time:.1f} hours exceeds target - optimize routes and scheduling"  # noqa: E501
            )

    except Exception as e:
        logger.error(f"Failed to generate insights: {e}")
        insights.append("Unable to generate detailed insights due to data processing error")

    return insights


@shared_task(time_limit=3600, soft_time_limit=3300)
def monitor_business_kpis():
    """Monitor key performance indicators and send alerts"""
    try:
        logger.info("Monitoring business KPIs")

        # Get today's metrics
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)

        # Current day metrics
        today_orders = Order.query.filter(db.func.date(Order.created_at) == today).count()
        today_revenue = (
            db.session.query(db.func.sum(Order.total_amount))
            .filter(db.func.date(Order.created_at) == today, Order.status != "cancelled")
            .scalar()
            or 0
        )

        # Yesterday metrics for comparison
        yesterday_orders = Order.query.filter(db.func.date(Order.created_at) == yesterday).count()
        yesterday_revenue = (
            db.session.query(db.func.sum(Order.total_amount))
            .filter(db.func.date(Order.created_at) == yesterday, Order.status != "cancelled")
            .scalar()
            or 0
        )

        alerts = []

        # Check for significant drops
        if yesterday_orders > 0 and today_orders < yesterday_orders * 0.5:
            alerts.append(f"Order volume dropped significantly: {today_orders} today vs {yesterday_orders} yesterday")

        if yesterday_revenue > 0 and today_revenue < yesterday_revenue * 0.5:
            alerts.append(f"Revenue dropped significantly: {today_revenue} today vs {yesterday_revenue} yesterday")

        # Check delivery performance
        failed_deliveries_today = Delivery.query.filter(
            db.func.date(Delivery.created_at) == today, Delivery.status == "failed"
        ).count()

        total_deliveries_today = Delivery.query.filter(db.func.date(Delivery.created_at) == today).count()

        if total_deliveries_today > 0:
            failure_rate = (failed_deliveries_today / total_deliveries_today) * 100
            if failure_rate > 10:  # More than 10% failure rate
                alerts.append(
                    f"High delivery failure rate: {failure_rate:.1f}% ({failed_deliveries_today}/{total_deliveries_today})"  # noqa: E501
                )

        # Send alerts if any issues found
        if alerts:
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.is_active == True
            ).all()

            notification_service = NotificationService()

            for admin in admin_users:
                notification_service.send_notification(
                    admin.id,
                    "kpi_alert",
                    template_data={
                        "date": today.isoformat(),
                        "alerts": alerts,
                        "today_orders": today_orders,
                        "today_revenue": today_revenue,
                    },
                )

        logger.info(f"KPI monitoring completed: {len(alerts)} alerts generated")
        return {
            "alerts_count": len(alerts),
            "alerts": alerts,
            "today_orders": today_orders,
            "today_revenue": float(today_revenue),
        }

    except Exception as e:
        logger.error(f"Failed to monitor business KPIs: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def generate_product_performance_analysis():
    """Analyze product performance and generate recommendations"""
    try:
        logger.info("Generating product performance analysis")

        # Analyze last 30 days
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)

        analytics_service = AnalyticsService()
        product_performance = analytics_service._get_product_performance(start_date, end_date)

        # Analyze performance
        total_revenue = sum(p["revenue"] for p in product_performance)

        analysis = {
            "period": {"start_date": start_date.date().isoformat(), "end_date": end_date.date().isoformat()},
            "total_products_sold": len(product_performance),
            "total_revenue": total_revenue,
            "top_performers": product_performance[:5],  # Top 5 products
            "poor_performers": [
                p for p in product_performance if p["revenue"] < total_revenue * 0.01
            ],  # Products with <1% of total revenue
            "recommendations": [],
        }

        # Generate recommendations
        if analysis["poor_performers"]:
            analysis["recommendations"].append(
                f"Consider discontinuing or promoting {len(analysis['poor_performers'])} underperforming products"
            )

        # Check for inventory needs
        high_demand_products = [p for p in product_performance if p["quantity_sold"] > 100]
        if high_demand_products:
            analysis["recommendations"].append(
                f"Ensure adequate inventory for {len(high_demand_products)} high-demand products"
            )

        # Store analysis
        report = AnalyticsReport(
            report_type="product_performance",
            period_start=start_date,
            period_end=end_date,
            data=analysis,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        logger.info(f"Product performance analysis completed: {len(product_performance)} products analyzed")
        return {
            "success": True,
            "report_id": report.id,
            "products_analyzed": len(product_performance),
            "recommendations_count": len(analysis["recommendations"]),
        }

    except Exception as e:
        logger.error(f"Failed to generate product performance analysis: {e}")
        return {"error": str(e)}


@shared_task(time_limit=3600, soft_time_limit=3300)
def update_geographic_analytics():
    """Update geographic analytics for delivery optimization"""
    try:
        logger.info("Updating geographic analytics")

        # Analyze delivery patterns by area
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)

        analytics_service = AnalyticsService()
        geographic_data = analytics_service._get_geographic_sales_distribution(start_date, end_date)

        # Calculate metrics by area
        area_metrics = []

        for area in geographic_data:
            city = area["city"]
            orders = area["orders"]
            revenue = area["revenue"]

            # Get delivery metrics for this area
            avg_delivery_time = (
                db.session.query(db.func.avg(db.func.extract("epoch", Delivery.delivered_at - Delivery.created_at)))
                .join(Order)
                .filter(
                    Order.delivery_address_city == city,
                    Delivery.created_at.between(start_date, end_date),
                    Delivery.status == "delivered",
                )
                .scalar()
            )

            delivery_success_rate = (
                db.session.query(
                    db.func.count(db.case([(Delivery.status == "delivered", 1)])) * 100.0 / db.func.count(Delivery.id)
                )
                .join(Order)
                .filter(Order.delivery_address_city == city, Delivery.created_at.between(start_date, end_date))
                .scalar()
                or 0
            )

            area_metrics.append(
                {
                    "city": city,
                    "orders": orders,
                    "revenue": revenue,
                    "avg_delivery_time_hours": round((avg_delivery_time / 3600) if avg_delivery_time else 0, 2),
                    "delivery_success_rate": round(delivery_success_rate, 2),
                    "revenue_per_order": round(revenue / orders if orders > 0 else 0, 2),
                }
            )

        # Store geographic analytics
        report_data = {
            "period": {"start_date": start_date.date().isoformat(), "end_date": end_date.date().isoformat()},
            "area_metrics": area_metrics,
            "total_areas": len(area_metrics),
            "top_revenue_areas": sorted(area_metrics, key=lambda x: x["revenue"], reverse=True)[:5],
        }

        report = AnalyticsReport(
            report_type="geographic_analytics",
            period_start=start_date,
            period_end=end_date,
            data=report_data,
            generated_at=datetime.now(timezone.utc),
        )

        db.session.add(report)
        db.session.commit()

        logger.info(f"Geographic analytics updated for {len(area_metrics)} areas")
        return {"success": True, "report_id": report.id, "areas_analyzed": len(area_metrics)}

    except Exception as e:
        logger.error(f"Failed to update geographic analytics: {e}")
        return {"error": str(e)}
