"""
Delivery-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/delivery_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.user import User
from business_app.services.delivery_service import DeliveryService
from business_app.services.analytics_service import AnalyticsService
from business_app.services.notification_service import NotificationService
from business_app.services.maps_service import MapsService
from shared.enums import DeliveryStatus, UserRole, OrderStatus
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300, time_limit=600, soft_time_limit=540)
def auto_assign_delivery_task(self, delivery_id: int):
    """Automatically assign delivery to available driver"""
    try:
        logger.info(f"Auto-assigning delivery {delivery_id}")

        # Lock delivery row to prevent concurrent assignment
        delivery = Delivery.query.with_for_update().get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        # Check if delivery is still pending
        if delivery.status != DeliveryStatus.SCHEDULED:
            logger.info(f"Delivery {delivery_id} is no longer scheduled")
            return {"success": False, "error": "Delivery no longer scheduled"}

        # Find available drivers via the DeliveryPerson profile (matches the
        # pattern used by `evaluate_pool_insertion_suggestions_task`).
        candidate_drivers = DeliveryPerson.query.filter(
            DeliveryPerson.is_active.is_(True),
            DeliveryPerson.is_available.is_(True),
        ).all()
        available_drivers = [d for d in candidate_drivers if d.is_working_now]

        delivery_service = DeliveryService()

        # Find best driver based on proximity and availability
        best_driver = None
        min_distance = float("inf")

        import random

        for driver in available_drivers:
            if delivery_service._is_driver_available(driver.user_id):
                # TODO: Replace with real GPS-based distance calculation when driver location tracking is implemented
                distance = random.uniform(1.0, 10.0)

                if distance < min_distance:
                    min_distance = distance
                    best_driver = driver

        if best_driver:
            # Assign delivery (assign_delivery_driver expects the User id)
            delivery_service.assign_delivery_driver(delivery_id, best_driver.user_id)

            logger.info(f"Delivery {delivery_id} auto-assigned to driver {best_driver.user_id}")
            return {
                "success": True,
                "delivery_id": delivery_id,
                "driver_id": best_driver.user_id,
                "driver_name": best_driver.full_name,
            }
        else:
            logger.warning(f"No available drivers found for delivery {delivery_id}")
            # Retry after 15 minutes
            raise self.retry(countdown=900)

    except Exception as exc:
        logger.error(f"Auto-assignment failed for delivery {delivery_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def cleanup_completed_deliveries():
    """Clean up old completed delivery records"""
    try:
        logger.info("Cleaning up old completed deliveries")

        # Archive deliveries completed more than 1 year ago
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=365)

        old_deliveries = Delivery.query.filter(
            Delivery.delivered_at < cutoff_date, Delivery.status == DeliveryStatus.DELIVERED
        ).all()

        archived_count = 0

        for delivery in old_deliveries:
            # Move to archive table or update status (assuming you have an is_archived field)
            # delivery.is_archived = True
            # delivery.archived_at = datetime.now(timezone.utc)

            # For now, just update a field to mark as archived
            delivery.route_data = delivery.route_data or {}
            delivery.route_data["archived"] = True
            delivery.route_data["archived_at"] = datetime.now(timezone.utc).isoformat()
            archived_count += 1

        db.session.commit()

        logger.info(f"Archived {archived_count} old delivery records")
        return {"archived_count": archived_count}

    except Exception as e:
        logger.error(f"Failed to clean up completed deliveries: {e}")
        db.session.rollback()
        return {"error": str(e)}


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def generate_driver_performance_report(self, driver_id: int, start_date: str, end_date: str):
    """Generate performance report for a specific driver"""
    try:
        logger.info(f"Generating performance report for driver {driver_id}")

        start_dt = datetime.fromisoformat(start_date)
        end_dt = datetime.fromisoformat(end_date)

        # Get driver's deliveries
        deliveries = Delivery.query.filter(
            Delivery.delivery_person_id == driver_id, Delivery.created_at.between(start_dt, end_dt)
        ).all()

        if not deliveries:
            return {"success": False, "error": "No deliveries found for this period"}

        # Calculate metrics
        total_deliveries = len(deliveries)
        successful_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.DELIVERED])
        failed_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.FAILED])

        # Average delivery time
        completed_deliveries = [d for d in deliveries if d.delivered_at and d.status == DeliveryStatus.DELIVERED]
        avg_delivery_time = 0

        if completed_deliveries:
            total_time = sum(
                (d.actual_delivery_time - d.scheduled_date).total_seconds()
                for d in completed_deliveries
                if d.actual_delivery_time
            )
            avg_delivery_time = total_time / len(completed_deliveries) / 60  # in minutes

        # Distance traveled
        total_distance = sum(d.distance_km for d in deliveries if d.distance_km)

        # Customer ratings average
        ratings = [d.customer_rating for d in deliveries if d.customer_rating]
        avg_rating = sum(ratings) / len(ratings) if ratings else 0

        report = {
            "driver_id": driver_id,
            "period": {"start_date": start_date, "end_date": end_date},
            "metrics": {
                "total_deliveries": total_deliveries,
                "successful_deliveries": successful_deliveries,
                "failed_deliveries": failed_deliveries,
                "success_rate": round(
                    (successful_deliveries / total_deliveries * 100) if total_deliveries > 0 else 0, 2
                ),
                "average_delivery_time_minutes": round(avg_delivery_time, 2),
                "total_distance_km": round(total_distance, 2),
                "average_rating": round(avg_rating, 2),
                "total_attempts": sum(d.delivery_attempts for d in deliveries),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Send report to driver and management
        driver = User.query.get(driver_id)
        if driver:
            notification_service = NotificationService()
            notification_service.send_notification(
                driver_id,
                "performance_report",
                template_data={
                    "report_period": f"{start_date} to {end_date}",
                    "total_deliveries": total_deliveries,
                    "success_rate": report["metrics"]["success_rate"],
                    "avg_rating": report["metrics"]["average_rating"],
                },
            )

        logger.info(f"Performance report generated for driver {driver_id}")
        return report

    except Exception as exc:
        logger.error(f"Failed to generate driver performance report: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def monitor_delivery_delays():
    """Monitor deliveries for delays and send alerts"""
    try:
        logger.info("Monitoring delivery delays")

        # Get deliveries that are overdue
        now = datetime.now(timezone.utc)
        overdue_deliveries = Delivery.query.filter(
            Delivery.estimated_delivery_time < now,
            Delivery.status.in_([DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.PICKED_UP]),
        ).all()

        notification_service = NotificationService()
        alerts_sent = 0

        for delivery in overdue_deliveries:
            try:
                delay_minutes = (now - delivery.estimated_delivery_time).total_seconds() / 60

                # Send alert to management for significant delays (>30 minutes)
                if delay_minutes > 30:
                    # Send alert to operations team
                    admin_users = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER])).all()

                    for admin in admin_users:
                        notification_service.send_notification(
                            admin.id,
                            "delivery_delay_alert",
                            template_data={
                                "delivery_id": delivery.id,
                                "order_number": delivery.order.order_number,
                                "delay_minutes": int(delay_minutes),
                                "driver_name": (
                                    f"{delivery.delivery_person.first_name} {delivery.delivery_person.last_name}"
                                    if delivery.delivery_person
                                    else "Unassigned"
                                ),
                                "customer_phone": delivery.order.user.phone,
                            },
                        )

                    alerts_sent += 1

            except Exception as e:
                logger.error(f"Failed to send delay alert for delivery {delivery.id}: {e}")
                continue

        logger.info(f"Sent {alerts_sent} delivery delay alerts")
        return {"alerts_sent": alerts_sent, "overdue_deliveries": len(overdue_deliveries)}

    except Exception as e:
        logger.error(f"Failed to monitor delivery delays: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def update_delivery_zones_task(self):
    """Update delivery zones based on demand and performance"""
    try:
        logger.info("Updating delivery zones")

        # Analyze delivery patterns from last 30 days
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)

        from sqlalchemy import func

        # Get delivery density by area (using rounded coordinates as zones)
        delivery_density = (
            db.session.query(
                func.round(func.avg(Delivery.order.delivery_latitude), 3).label("avg_lat"),
                func.round(func.avg(Delivery.order.delivery_longitude), 3).label("avg_lng"),
                func.count(Delivery.id).label("delivery_count"),
                func.avg(func.extract("epoch", Delivery.actual_delivery_time - Delivery.created_at)).label("avg_time"),
                func.avg(Delivery.distance_km).label("avg_distance"),
            )
            .filter(Delivery.created_at.between(start_date, end_date), Delivery.status == DeliveryStatus.DELIVERED)
            .group_by(func.round(Delivery.order.delivery_latitude, 2), func.round(Delivery.order.delivery_longitude, 2))
            .having(func.count(Delivery.id) >= 5)
            .all()
        )  # Minimum 5 deliveries to consider

        zone_updates = []

        for avg_lat, avg_lng, count, avg_time, avg_distance in delivery_density:
            avg_time_hours = (avg_time / 3600) if avg_time else 0

            zone_data = {
                "center_lat": float(avg_lat),
                "center_lng": float(avg_lng),
                "delivery_count": count,
                "avg_delivery_time_hours": round(avg_time_hours, 2),
                "avg_distance_km": round(avg_distance, 2) if avg_distance else 0,
                "recommended_fee": 5000 if avg_time_hours > 2 else 3000,  # Higher fee for longer delivery times
                "zone_priority": "high" if count > 20 else "medium" if count > 10 else "low",
            }

            zone_updates.append(zone_data)

        # Store zone recommendations
        analytics_service = AnalyticsService()
        analytics_service.store_delivery_zone_analysis(zone_updates)

        logger.info(f"Updated delivery zones analysis for {len(zone_updates)} areas")
        return {"updated_zones": len(zone_updates), "zone_data": zone_updates}

    except Exception as exc:
        logger.error(f"Failed to update delivery zones: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=120, soft_time_limit=100)
def optimize_driver_route_task(self, driver_id: int, trigger: str = "auto"):
    """Recompute the optimal delivery sequence for a driver.

    Persists the result to `DeliveryRoute.optimized_order` and pushes a
    "/internal/route-updated" webhook to the staff bot so any open menu
    refreshes. `trigger` is a human-readable label ("accept", "manual",
    "cancel", "pool_insert") for log/observability.
    """
    try:
        from business_app.services.route_optimization_service import RouteOptimizationService
        from business_app.utils.bot_webhook import notify_route_updated

        service = RouteOptimizationService()
        route = service.optimize_for_driver(driver_id, trigger=trigger)

        if route is None:
            return {"optimized": False, "reason": "no_active_deliveries"}

        try:
            notify_route_updated(driver_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("route_updated webhook push failed driver=%s: %s", driver_id, exc)

        return {
            "optimized": True,
            "driver_id": driver_id,
            "trigger": trigger,
            "delivery_count": len(route.optimized_order or []),
            "total_distance_km": route.total_distance_km,
            "estimated_duration_minutes": route.estimated_duration_minutes,
            "matrix_source": (route.extra_data or {}).get("matrix_source"),
        }

    except Exception as exc:
        db.session.rollback()
        logger.error(f"Route optimization failed for driver {driver_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=60, soft_time_limit=50)
def evaluate_pool_insertion_suggestions_task(self, delivery_id: int):
    """For a freshly-pooled delivery, find an active driver it can be slipped
    into and push a suggestion to the staff bot.

    Skips silently if:
      - Delivery is already assigned to a driver.
      - No active driver's route can absorb it within the configured detour
        thresholds.
    """
    try:
        from flask import current_app
        from business_app.models.delivery import DeliveryPerson
        from business_app.services.route_optimization_service import RouteOptimizationService
        from business_app.utils.bot_webhook import notify_pool_insertion_suggestion

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            return {"suggested": False, "reason": "delivery_not_found"}
        if delivery.delivery_person_id is not None:
            return {"suggested": False, "reason": "already_assigned"}

        max_km = float(current_app.config.get("ROUTE_INSERTION_MAX_DETOUR_KM", 5.0))
        max_min = float(current_app.config.get("ROUTE_INSERTION_MAX_DETOUR_MIN", 15.0))

        # Candidate drivers: active + available + currently working hours.
        candidates = DeliveryPerson.query.filter(
            DeliveryPerson.is_active.is_(True),
            DeliveryPerson.is_available.is_(True),
        ).all()
        candidates = [c for c in candidates if c.is_working_now]
        if not candidates:
            return {"suggested": False, "reason": "no_active_drivers"}

        service = RouteOptimizationService()
        best: Dict[str, Any] = {}
        for cand in candidates:
            cost = service.compute_insertion_cost(cand.user_id, delivery_id)
            if cost is None:
                continue
            if cost["delta_km"] > max_km or cost["delta_minutes"] > max_min:
                continue
            if not best or cost["delta_km"] < best["delta_km"]:
                best = {
                    "driver_id": cand.user_id,
                    "delta_km": cost["delta_km"],
                    "delta_minutes": cost["delta_minutes"],
                    "position": cost["position"],
                }

        if not best:
            return {"suggested": False, "reason": "no_fit_within_thresholds"}

        order_no = delivery.order.order_number if delivery.order else str(delivery.order_id)
        try:
            notify_pool_insertion_suggestion(
                driver_id=best["driver_id"],
                delivery_id=delivery_id,
                order_no=order_no,
                detour_km=round(best["delta_km"], 1),
                detour_minutes=round(best["delta_minutes"]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pool_insertion_suggestion webhook push failed: %s", exc)

        logger.info(
            "insertion_suggested delivery=%s driver=%s delta_km=%.2f delta_min=%.1f position=%d",
            delivery_id,
            best["driver_id"],
            best["delta_km"],
            best["delta_minutes"],
            best["position"],
        )
        return {"suggested": True, **best}

    except Exception as exc:
        logger.error(f"Pool insertion eval failed for delivery {delivery_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def optimize_daily_delivery_routes():
    """Optimize delivery routes for all drivers daily"""
    try:
        logger.info("Optimizing daily delivery routes")

        # Get all active drivers with pending deliveries
        active_drivers = (
            db.session.query(Delivery.delivery_person_id)
            .filter(
                Delivery.status.in_([DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.PICKED_UP]),
                Delivery.delivery_person_id.isnot(None),
            )
            .distinct()
            .all()
        )

        optimized_count = 0

        for (driver_id,) in active_drivers:
            try:
                optimize_driver_route_task.delay(driver_id)
                optimized_count += 1
            except Exception as e:
                logger.error(f"Failed to optimize route for driver {driver_id}: {e}")
                continue

        logger.info(f"Initiated route optimization for {optimized_count} drivers")
        return {"optimized_drivers": optimized_count}

    except Exception as e:
        logger.error(f"Daily route optimization failed: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def send_delivery_reminders():
    """Send delivery reminders to customers and drivers"""
    try:
        logger.info("Sending delivery reminders")

        # Get deliveries scheduled for next 2 hours
        now = datetime.now(timezone.utc)
        reminder_window = now + timedelta(hours=2)

        upcoming_deliveries = Delivery.query.filter(
            Delivery.estimated_delivery_time.between(now, reminder_window),
            Delivery.status.in_([DeliveryStatus.ASSIGNED, DeliveryStatus.IN_TRANSIT, DeliveryStatus.PICKED_UP]),
        ).all()

        notification_service = NotificationService()
        customer_reminders = 0
        driver_reminders = 0

        for delivery in upcoming_deliveries:
            try:
                # Send customer reminder
                customer_template_data = {
                    "order_number": delivery.order.order_number,
                    "tracking_number": delivery.tracking_number,
                    "estimated_delivery_time": delivery.estimated_delivery_time.strftime("%H:%M"),
                    "driver_name": (
                        f"{delivery.delivery_person.first_name} {delivery.delivery_person.last_name}"
                        if delivery.delivery_person
                        else None
                    ),
                    "driver_phone": delivery.delivery_person.phone if delivery.delivery_person else None,
                }

                notification_service.send_notification(
                    delivery.order.user_id, "delivery_reminder", template_data=customer_template_data
                )
                customer_reminders += 1

                # Send driver reminder
                if delivery.delivery_person_id:
                    driver_template_data = {
                        "delivery_id": delivery.id,
                        "order_number": delivery.order.order_number,
                        "customer_name": f"{delivery.order.user.first_name} {delivery.order.user.last_name}",
                        "delivery_address": delivery.order.delivery_address,
                        "estimated_time": delivery.estimated_delivery_time.strftime("%H:%M"),
                    }

                    notification_service.send_notification(
                        delivery.delivery_person_id, "delivery_reminder", template_data=driver_template_data
                    )
                    driver_reminders += 1

            except Exception as e:
                logger.error(f"Failed to send reminder for delivery {delivery.id}: {e}")
                continue

        logger.info(f"Sent {customer_reminders} customer reminders and {driver_reminders} driver reminders")
        return {"customer_reminders": customer_reminders, "driver_reminders": driver_reminders}

    except Exception as e:
        logger.error(f"Failed to send delivery reminders: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def track_delivery_location_task(self, delivery_id: int, latitude: float, longitude: float):
    """Update delivery location tracking"""
    try:
        logger.info(f"Updating location for delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        # Update location
        delivery.update_location(latitude, longitude)

        # Calculate distance to destination
        maps_service = MapsService()
        destination_latitude = None
        destination_longitude = None
        if delivery.order and delivery.order.delivery_address:
            destination_latitude = delivery.order.delivery_address.latitude
            destination_longitude = delivery.order.delivery_address.longitude
        else:
            destination_latitude = getattr(delivery.order, "delivery_latitude", None)
            destination_longitude = getattr(delivery.order, "delivery_longitude", None)

        if destination_latitude is None or destination_longitude is None:
            logger.warning(
                "Skipping distance-based arrival check for delivery %s: destination coordinates unavailable",
                delivery_id,
            )
            db.session.commit()
            return {
                "success": True,
                "delivery_id": delivery_id,
                "distance_to_destination": None,
                "status": delivery.status.value,
            }

        distance_to_destination = maps_service.calculate_distance(
            latitude, longitude, destination_latitude, destination_longitude
        )

        # Update status if driver is close to destination (within 100m)
        if distance_to_destination < 0.1 and delivery.status == DeliveryStatus.IN_TRANSIT:
            delivery_service = DeliveryService()
            delivery = delivery_service.mark_delivery_arrived(
                delivery.id,
                actor_user_id=None,
                notes="Automatically marked as arrived based on location tracking",
                automatic=True,
            )

        db.session.commit()

        logger.info(f"Location updated for delivery {delivery_id}")
        return {
            "success": True,
            "delivery_id": delivery_id,
            "distance_to_destination": distance_to_destination,
            "status": delivery.status.value,
        }

    except Exception as exc:
        logger.error(f"Location tracking failed for delivery {delivery_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def calculate_delivery_eta_task(self, delivery_id: int):
    """Calculate and update delivery ETA"""
    try:
        logger.info(f"Calculating ETA for delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        if not delivery.current_location_lat or not delivery.current_location_lng:
            logger.warning(f"No current location for delivery {delivery_id}")
            return {"success": False, "error": "No current location"}

        maps_service = MapsService()

        # Calculate travel time from current location to destination
        travel_time = maps_service.calculate_travel_time(
            delivery.current_location_lat,
            delivery.current_location_lng,
            delivery.order.delivery_latitude,
            delivery.order.delivery_longitude,
        )

        # Calculate new ETA
        current_eta = delivery.estimated_delivery_time
        new_eta = datetime.now(timezone.utc) + timedelta(minutes=travel_time.get("duration_minutes", 30))

        # Update ETA
        delivery.estimated_delivery_time = new_eta
        delivery.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        # Notify customer if ETA changed significantly (more than 15 minutes)
        if current_eta and abs((new_eta - current_eta).total_seconds()) > 900:
            notification_service = NotificationService()
            notification_service.send_notification(
                delivery.order.user_id,
                "delivery_eta_updated",
                template_data={
                    "order_number": delivery.order.order_number,
                    "new_eta": new_eta.strftime("%H:%M"),
                    "tracking_number": delivery.tracking_number,
                },
            )

        logger.info(f"ETA calculated for delivery {delivery_id}: {new_eta}")
        return {
            "success": True,
            "delivery_id": delivery_id,
            "eta": new_eta.isoformat(),
            "travel_time_minutes": travel_time.get("duration_minutes"),
        }

    except Exception as exc:
        logger.error(f"ETA calculation failed for delivery {delivery_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def process_delivery_analytics():
    """Process delivery analytics and generate insights"""
    try:
        logger.info("Processing delivery analytics")

        from business_app.utils.helpers import get_analytics_date_range

        start_date, end_date = get_analytics_date_range(days=1)

        from sqlalchemy import func

        delivery_service = DeliveryService()
        # Basic delivery metrics
        analytics_data = delivery_service.get_delivery_metrics(start_date, end_date)

        # Average delivery time by zone
        zone_metrics = (
            db.session.query(
                Delivery.delivery_zone,
                func.avg(func.extract("epoch", Delivery.delivered_at - Delivery.created_at)).label("avg_time"),
                func.count(Delivery.id).label("delivery_count"),
            )
            .filter(Delivery.created_at.between(start_date, end_date), Delivery.status == DeliveryStatus.DELIVERED)
            .group_by(Delivery.delivery_zone)
            .all()
        )

        analytics_data["zone_metrics"] = [
            {
                "zone": zone,
                "average_time_hours": round((avg_time / 3600) if avg_time else 0, 2),
                "delivery_count": count,
            }
            for zone, avg_time, count in zone_metrics
        ]

        # Driver performance
        driver_metrics = (
            db.session.query(
                Delivery.driver_id,
                func.count(Delivery.id).label("delivery_count"),
                func.avg(func.extract("epoch", Delivery.delivered_at - Delivery.picked_up_at)).label(
                    "avg_delivery_time"
                ),
            )
            .filter(
                Delivery.created_at.between(start_date, end_date),
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.driver_id.isnot(None),
            )
            .group_by(Delivery.driver_id)
            .all()
        )

        analytics_data["driver_performance"] = [
            {
                "driver_id": driver_id,
                "delivery_count": count,
                "avg_delivery_time_hours": round((avg_time / 3600) if avg_time else 0, 2),
                "avg_rating": round(avg_rating, 2) if avg_rating else 0,
            }
            for driver_id, count, avg_time, avg_rating in driver_metrics
        ]

        # Geographic delivery distribution
        geographic_metrics = (
            db.session.query(
                func.round(Delivery.order.delivery_latitude, 2).label("lat_zone"),
                func.round(Delivery.order.delivery_longitude, 2).label("lng_zone"),
                func.count(Delivery.id).label("delivery_count"),
                func.avg(Delivery.distance_km).label("avg_distance"),
            )
            .filter(Delivery.created_at.between(start_date, end_date), Delivery.status == DeliveryStatus.DELIVERED)
            .group_by("lat_zone", "lng_zone")
            .all()
        )

        analytics_data["geographic_metrics"] = [
            {
                "lat_zone": float(lat_zone),
                "lng_zone": float(lng_zone),
                "delivery_count": count,
                "avg_distance_km": round(avg_distance, 2) if avg_distance else 0,
            }
            for lat_zone, lng_zone, count, avg_distance in geographic_metrics
        ]

        # Store analytics data
        analytics_service = AnalyticsService()
        analytics_service.store_delivery_analytics(analytics_data)

        logger.info("Delivery analytics processed successfully")
        return analytics_data

    except Exception as e:
        logger.error(f"Failed to process delivery analytics: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, time_limit=600, soft_time_limit=540)
def handle_delivery_exception_task(self, delivery_id: int, exception_type: str, details: Dict[str, Any]):
    """Handle delivery exceptions (delays, issues, etc.)"""
    try:
        logger.info(f"Handling delivery exception for delivery {delivery_id}: {exception_type}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        notification_service = NotificationService()

        if exception_type == "delay":
            # Handle delivery delay
            new_eta = datetime.fromisoformat(details.get("new_eta", datetime.now(timezone.utc).isoformat()))
            delay_reason = details.get("reason", "Traffic conditions")

            delivery.estimated_delivery_time = new_eta
            delivery.updated_at = datetime.now(timezone.utc)

            # Notify customer about delay
            notification_service.send_notification(
                delivery.order.user_id,
                "delivery_delayed",
                template_data={
                    "order_number": delivery.order.order_number,
                    "new_eta": new_eta.strftime("%H:%M"),
                    "delay_reason": delay_reason,
                    "tracking_number": delivery.tracking_number,
                },
            )

        elif exception_type == "failed_attempt":
            # Handle failed delivery attempt
            attempt_reason = details.get("reason", "Customer not available")

            # Update delivery status and increment attempts
            delivery.status = DeliveryStatus.FAILED
            delivery.delivery_attempts += 1
            delivery.failed_delivery_reason = attempt_reason
            delivery.updated_at = datetime.now(timezone.utc)

            # Notify customer and schedule retry
            notification_service.send_notification(
                delivery.order.user_id,
                "delivery_failed_attempt",
                template_data={
                    "order_number": delivery.order.order_number,
                    "failure_reason": attempt_reason,
                    "retry_info": "We will contact you to reschedule delivery",
                },
            )

            # Auto-reschedule if attempts < 3
            if delivery.delivery_attempts < 3:
                reschedule_failed_delivery_task.delay(delivery_id)

        elif exception_type == "vehicle_breakdown":
            # Reassign to another driver
            auto_assign_delivery_task.delay(delivery_id)

            # Notify customer about reassignment
            notification_service.send_notification(
                delivery.order.user_id,
                "delivery_reassigned",
                template_data={
                    "order_number": delivery.order.order_number,
                    "reason": "Technical issue with delivery vehicle",
                },
            )

        db.session.commit()

        logger.info(f"Delivery exception handled for delivery {delivery_id}: {exception_type}")
        return {"success": True, "exception_type": exception_type, "delivery_id": delivery_id}

    except Exception as exc:
        logger.error(f"Failed to handle delivery exception: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=600, time_limit=600, soft_time_limit=540)
def reschedule_failed_delivery_task(self, delivery_id: int):
    """Reschedule a failed delivery attempt"""
    try:
        logger.info(f"Rescheduling failed delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        # Reset delivery status and schedule for next available slot
        delivery.status = DeliveryStatus.SCHEDULED

        # Schedule for next day, same time slot
        next_day = delivery.scheduled_date + timedelta(days=1)
        delivery.scheduled_date = next_day
        delivery.estimated_delivery_time = next_day.replace(
            hour=delivery.estimated_delivery_time.hour, minute=delivery.estimated_delivery_time.minute
        )

        # Clear driver assignment for reassignment
        delivery.delivery_person_id = None
        delivery.updated_at = datetime.now(timezone.utc)

        db.session.commit()

        # Notify customer about rescheduling
        notification_service = NotificationService()
        notification_service.send_notification(
            delivery.order.user_id,
            "delivery_rescheduled",
            template_data={
                "order_number": delivery.order.order_number,
                "new_date": next_day.strftime("%Y-%m-%d"),
                "tracking_number": delivery.tracking_number,
            },
        )

        # Auto-assign to new driver
        auto_assign_delivery_task.delay(delivery_id)

        logger.info(f"Delivery {delivery_id} rescheduled to {next_day}")
        return {"success": True, "delivery_id": delivery_id, "new_date": next_day.isoformat()}

    except Exception as exc:
        logger.error(f"Failed to reschedule delivery {delivery_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def process_delivery_confirmation_task(self, delivery_id: int, confirmation_data: Dict[str, Any]):
    """Process delivery confirmation with photos and signature"""
    try:
        logger.info(f"Processing delivery confirmation for delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            logger.error(f"Delivery {delivery_id} not found")
            return {"success": False, "error": "Delivery not found"}

        # Extract confirmation data
        photos = confirmation_data.get("photos", [])
        signature = confirmation_data.get("signature")
        notes = confirmation_data.get("notes")
        customer_present = confirmation_data.get("customer_present", True)

        # Always transition through OrderService so cash-order inventory deduction
        # and status history run consistently.
        order_status = delivery.order.status.value if hasattr(delivery.order.status, "value") else delivery.order.status
        if order_status != OrderStatus.DELIVERED.value:
            from business_app.services.order_service import OrderService

            OrderService().update_order_status(delivery.order_id, OrderStatus.DELIVERED)
            db.session.refresh(delivery)

        # Persist delivery confirmation artifacts after status transition.
        if photos:
            delivery.delivery_confirmation_photos = photos
        if signature:
            delivery.recipient_signature = signature
        if notes:
            delivery.delivery_notes = notes
        if not customer_present:
            note_suffix = "Customer not present at handoff"
            delivery.delivery_notes = (
                f"{delivery.delivery_notes}; {note_suffix}" if delivery.delivery_notes else note_suffix
            )

        db.session.commit()

        # Send completion notification to customer
        notification_service = NotificationService()
        notification_service.send_notification(
            delivery.order.user_id,
            "delivery_completed",
            template_data={
                "order_number": delivery.order.order_number,
                "delivered_at": delivery.delivered_at.strftime("%Y-%m-%d %H:%M"),
                "tracking_number": delivery.tracking_number,
            },
        )

        # Request customer feedback
        request_delivery_feedback_task.delay(delivery_id)

        logger.info(f"Delivery {delivery_id} marked as completed")
        return {"success": True, "delivery_id": delivery_id, "completed_at": delivery.delivered_at.isoformat()}

    except Exception as exc:
        logger.error(f"Failed to process delivery confirmation: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=600, soft_time_limit=540)
def request_delivery_feedback_task(delivery_id: int):
    """Request customer feedback for completed delivery"""
    try:
        logger.info(f"Requesting feedback for delivery {delivery_id}")

        delivery = Delivery.query.get(delivery_id)
        if not delivery or delivery.status != DeliveryStatus.DELIVERED:
            return {"success": False, "error": "Invalid delivery for feedback"}

        # Send feedback request after 1 hour delay
        notification_service = NotificationService()
        notification_service.send_notification(
            delivery.order.user_id,
            "delivery_feedback_request",
            template_data={
                "order_number": delivery.order.order_number,
                "driver_name": f"{delivery.delivery_person.first_name} {delivery.delivery_person.last_name}",
                "feedback_link": f"/feedback/delivery/{delivery.tracking_number}",
            },
            delay_minutes=60,
        )

        logger.info(f"Feedback request sent for delivery {delivery_id}")
        return {"success": True, "delivery_id": delivery_id}

    except Exception as e:
        logger.error(f"Failed to request delivery feedback: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def generate_delivery_heatmap_data():
    """Generate delivery heatmap data for admin dashboard"""
    try:
        logger.info("Generating delivery heatmap data")

        # Get delivery data from last 7 days
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=7)

        from sqlalchemy import func

        # Get delivery coordinates with density
        heatmap_data = (
            db.session.query(
                Delivery.order.delivery_latitude.label("lat"),
                Delivery.order.delivery_longitude.label("lng"),
                func.count(Delivery.id).label("intensity"),
                func.avg(func.extract("epoch", Delivery.actual_delivery_time - Delivery.created_at)).label("avg_time"),
            )
            .filter(
                Delivery.created_at.between(start_date, end_date),
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.order.delivery_latitude.isnot(None),
                Delivery.order.delivery_longitude.isnot(None),
            )
            .group_by(Delivery.order.delivery_latitude, Delivery.order.delivery_longitude)
            .all()
        )

        heatmap_points = []
        for lat, lng, intensity, avg_time in heatmap_data:
            heatmap_points.append(
                {
                    "lat": float(lat),
                    "lng": float(lng),
                    "intensity": intensity,
                    "avg_delivery_time_hours": round((avg_time / 3600) if avg_time else 0, 2),
                }
            )

        # Store heatmap data
        analytics_service = AnalyticsService()
        analytics_service.store_heatmap_data(
            {
                "type": "delivery_performance",
                "period": f"{start_date.date()} to {end_date.date()}",
                "data_points": heatmap_points,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        logger.info(f"Generated heatmap data with {len(heatmap_points)} points")
        return {"heatmap_points": len(heatmap_points), "data": heatmap_points}

    except Exception as e:
        logger.error(f"Failed to generate delivery heatmap data: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def optimize_time_slots():
    """Optimize delivery time slots based on historical data"""
    try:
        logger.info("Optimizing delivery time slots")

        # Analyze time slot performance from last 30 days
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=30)

        from sqlalchemy import func

        time_slot_analysis = (
            db.session.query(
                Delivery.scheduled_time_slot,
                func.count(Delivery.id).label("total_deliveries"),
                func.avg(func.extract("epoch", Delivery.actual_delivery_time - Delivery.scheduled_date)).label(
                    "avg_delivery_time"
                ),
                func.count(func.nullif(Delivery.status != DeliveryStatus.DELIVERED, False)).label("failed_deliveries"),
                func.avg(Delivery.customer_rating).label("avg_rating"),
            )
            .filter(Delivery.created_at.between(start_date, end_date))
            .group_by(Delivery.scheduled_time_slot)
            .all()
        )

        optimization_results = []

        for slot, total, avg_time, failed, rating in time_slot_analysis:
            if total > 0:
                success_rate = ((total - (failed or 0)) / total) * 100
                avg_time_hours = (avg_time / 3600) if avg_time else 0

                # Determine if slot needs optimization
                needs_optimization = (
                    success_rate < 90  # Low success rate
                    or avg_time_hours > 3  # Takes too long
                    or (rating and rating < 4.0)  # Low customer satisfaction
                )

                optimization_results.append(
                    {
                        "time_slot": slot,
                        "total_deliveries": total,
                        "success_rate": round(success_rate, 2),
                        "avg_delivery_time_hours": round(avg_time_hours, 2),
                        "avg_rating": round(rating, 2) if rating else None,
                        "needs_optimization": needs_optimization,
                        "recommended_action": (
                            "reduce_capacity"
                            if avg_time_hours > 3
                            else "maintain" if success_rate > 95 else "review_process"
                        ),
                    }
                )

        # Store optimization analysis
        analytics_service = AnalyticsService()
        analytics_service.store_time_slot_optimization(optimization_results)

        logger.info(f"Time slot optimization completed for {len(optimization_results)} slots")
        return {"analyzed_slots": len(optimization_results), "recommendations": optimization_results}

    except Exception as e:
        logger.error(f"Failed to optimize time slots: {e}")
        return {"error": str(e)}


@shared_task(time_limit=600, soft_time_limit=540)
def send_daily_delivery_summary():
    """Send daily delivery summary to management"""
    try:
        logger.info("Sending daily delivery summary")

        # Get yesterday's data
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        start_date = datetime.combine(yesterday, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_date = start_date + timedelta(days=1)

        from sqlalchemy import func

        # Calculate summary metrics
        total_deliveries = Delivery.query.filter(Delivery.created_at.between(start_date, end_date)).count()

        successful_deliveries = Delivery.query.filter(
            Delivery.created_at.between(start_date, end_date), Delivery.status == DeliveryStatus.DELIVERED
        ).count()

        failed_deliveries = Delivery.query.filter(
            Delivery.created_at.between(start_date, end_date), Delivery.status == DeliveryStatus.FAILED
        ).count()

        avg_rating = (
            db.session.query(func.avg(Delivery.customer_rating))
            .filter(Delivery.created_at.between(start_date, end_date), Delivery.customer_rating.isnot(None))
            .scalar()
            or 0
        )

        summary_data = {
            "date": yesterday.isoformat(),
            "total_deliveries": total_deliveries,
            "successful_deliveries": successful_deliveries,
            "failed_deliveries": failed_deliveries,
            "success_rate": round((successful_deliveries / total_deliveries * 100) if total_deliveries > 0 else 0, 2),
            "average_rating": round(avg_rating, 2),
        }

        # Send to management
        notification_service = NotificationService()
        admin_users = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER])).all()

        for admin in admin_users:
            notification_service.send_notification(admin.id, "daily_delivery_summary", template_data=summary_data)

        logger.info(f"Daily delivery summary sent: {summary_data}")
        return summary_data

    except Exception as e:
        logger.error(f"Failed to send daily delivery summary: {e}")
        return {"error": str(e)}
