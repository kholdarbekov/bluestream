"""
Delivery-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/delivery_tasks.py
"""

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, Retry, SoftTimeLimitExceeded
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.user import User
from business_app.services.delivery_service import DeliveryService
from business_app.services.notification_service import NotificationService
from business_app.services.maps_service import MapsService
from shared.enums import DeliveryStatus, UserRole, OrderStatus
from business_app import db
from business_app.utils.exceptions import ValidationError

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
        from business_app.services.driver_reconciliation_service import DriverReconciliationService
        from shared.enums import PaymentMethod

        order = delivery.order
        order_pm = order.payment_method if order else None
        is_cash = order_pm == PaymentMethod.CASH or getattr(order_pm, "value", None) == "cash"
        recon = DriverReconciliationService() if is_cash else None

        for driver in available_drivers:
            if is_cash and recon.is_driver_blocked_from_cod(driver.user_id):
                continue
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
            try:
                raise self.retry(countdown=900)
            except MaxRetriesExceededError:
                logger.warning(
                    f"No available drivers for delivery {delivery_id}: "
                    "giving up after max retries, periodic re-enqueue will retry later"
                )
                return {"success": False, "error": "no_available_drivers_max_retries"}

    except Retry:
        # Retry scheduling is control flow, not a failure — let Celery handle it.
        raise
    except Exception as exc:
        logger.error(f"Auto-assignment failed for delivery {delivery_id}: {exc}")
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error(
                f"Auto-assignment for delivery {delivery_id} exhausted retries; " "failing with the original error"
            )
            # Surface the real cause instead of a MaxRetriesExceededError wrapper.
            raise exc


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

        # Calculate metrics via the shared helper (also used by
        # AnalyticsService._get_driver_performance_metrics for fleet-wide
        # aggregation) so both stay consistent.
        metrics = DeliveryService.compute_driver_metrics(deliveries)

        report = {
            "driver_id": driver_id,
            "period": {"start_date": start_date, "end_date": end_date},
            "metrics": metrics,
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
                    "total_deliveries": metrics["total_deliveries"],
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

                # Send alert to management for significant delays (>12 hours)
                if delay_minutes > 12 * 60:
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


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=300, soft_time_limit=270)
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
            # `last_skip_reason` is set only for the debounced-location_update
            # path (route-UX plan §4.5); every other None cause (no active
            # deliveries, no shared location) keeps the pre-existing generic
            # label. Distinguishing the debounce case lets debounce rate be
            # counted from task results instead of only from service logs.
            # getattr, not a direct attribute access: some tests substitute a
            # minimal service stub that only implements optimize_for_driver.
            skip_reason = getattr(service, "last_skip_reason", None)
            return {"optimized": False, "reason": skip_reason or "no_active_deliveries"}

        # THE single push gate (route-UX plan 2026-08-11 §5.2):
        #   sounded ⟺ head_changed AND NOT driver_initiated.
        # Missing materiality fails SILENT — this is a noise-reduction
        # project; never default to loud.
        materiality = (route.extra_data or {}).get("materiality") or {}
        sound = bool(materiality.get("head_changed")) and not bool(materiality.get("driver_initiated"))
        try:
            # Stable across a `self.retry()` re-run (Celery keeps the same
            # task id across retries of one dispatch), so a retried push
            # dedups against the original on the bot side instead of
            # minting a fresh event_id and double-sending (Task 8 review
            # fix 1). Mirrors `staff_tasks.py`'s `f"...:{self.request.id}"`.
            notify_route_updated(
                driver_id,
                sound=sound,
                materiality=materiality,
                trigger=trigger,
                event_id=f"route_updated:{self.request.id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("route_updated webhook push failed driver=%s: %s", driver_id, exc)

        return {
            "optimized": True,
            "driver_id": driver_id,
            "trigger": trigger,
            "sounded": sound,
            "delivery_count": len(route.optimized_order or []),
            "total_distance_km": route.total_distance_km,
            # TRAVEL + a flat per-stop service-time allowance, not travel
            # alone (route_optimization_service.py::_sum_route_metrics, spec
            # 8.4 — final review round, I5). No consumer of this task result
            # renders it to a user today (every caller uses `.delay()` and
            # discards the return value; only tests and Flower's task-result
            # view read it), so the number itself is left as-is — this note
            # exists so it isn't mistaken for pure drive time by whoever
            # looks at it next.
            "estimated_duration_minutes": route.estimated_duration_minutes,
            "matrix_source": (route.extra_data or {}).get("matrix_source"),
        }

    except SoftTimeLimitExceeded:
        # Soft limit hit: abort gracefully so the hard limit never SIGKILLs the
        # worker mid-commit. A blind retry would just re-run the same slow I/O.
        db.session.rollback()
        logger.warning("Route optimization for driver %s exceeded soft time limit; skipping", driver_id)
        return {"optimized": False, "reason": "time_budget_exceeded"}
    except Exception as exc:
        db.session.rollback()
        logger.error(f"Route optimization failed for driver {driver_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=60, soft_time_limit=50)
def evaluate_pool_insertion_suggestions_task(self, delivery_id: int):
    """Diversion evaluator + single broadcast fan-out for a new pool delivery.

    For each active driver WITH a committed stop, computes the §7 diversion
    gain; if the best gain clears ROUTE_DIVERSION_MIN_GAIN_MINUTES, that ONE
    driver gets the targeted offer. It then ALWAYS enqueues the new-order
    broadcast, excluding the diverted driver — so nobody ever receives two
    Accept buttons for the same order (§10 duplicate-message bug). Diversion
    failures degrade to a full broadcast, never to silence.
    """
    try:
        from flask import current_app
        from business_app.models.delivery import DeliveryPerson
        from business_app.services.route_optimization_service import RouteOptimizationService
        from business_app.utils.bot_webhook import notify_pool_insertion_suggestion
        from business_app.tasks.staff_tasks import notify_staff_new_order

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            return {"suggested": False, "reason": "delivery_not_found"}
        if delivery.delivery_person_id is not None:
            return {"suggested": False, "reason": "already_assigned"}

        best: Dict[str, Any] = {}
        try:
            min_gain = float(current_app.config.get("ROUTE_DIVERSION_MIN_GAIN_MINUTES", 8.0))
            # Candidate drivers: active + available + currently working hours
            # + not muted + account active -- the SAME filter set the
            # broadcast applies (staff_tasks.py's notify_staff_new_order),
            # so a muted/deactivated driver can't receive the targeted push
            # either (review fix 6: this is now the only targeted push).
            candidates = (
                DeliveryPerson.query.join(User, User.id == DeliveryPerson.user_id)
                .filter(
                    DeliveryPerson.is_active.is_(True),
                    DeliveryPerson.is_available.is_(True),
                    DeliveryPerson.notifications_muted.is_(False),
                    User.status == "active",
                )
                .all()
            )
            candidates = [c for c in candidates if c.is_working_now]
            service = RouteOptimizationService()
            for cand in candidates:
                gain_info = service.compute_diversion_gain(cand.user_id, delivery_id)
                if gain_info is None or gain_info["gain_minutes"] < min_gain:
                    continue
                if not best or gain_info["gain_minutes"] > best["gain_minutes"]:
                    best = {"driver_id": cand.user_id, **gain_info}
        except Exception as exc:  # noqa: BLE001 — degrade to full broadcast
            logger.error("diversion eval failed for delivery %s: %s", delivery_id, exc)
            best = {}

        # Only exclude the driver from the broadcast once the targeted push
        # actually reached them (review fix 1): `_send_staff_bot_webhook`
        # never raises, it returns False on a non-2xx/rate-limit/connection
        # failure, so blindly excluding on `best` alone could leave the
        # single best-fit driver with NEITHER message.
        diverted_driver_id = None
        try:
            if best:
                order_no = delivery.order.order_number if delivery.order else str(delivery.order_id)
                sent = False
                try:
                    sent = notify_pool_insertion_suggestion(
                        driver_id=best["driver_id"],
                        delivery_id=delivery_id,
                        order_no=order_no,
                        detour_km=0.0,
                        detour_minutes=round(best["gain_minutes"]),
                        gain_minutes=round(best["gain_minutes"], 1),
                        committed_order_number=best["committed_order_number"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("diversion offer webhook push failed: %s", exc)
                if sent:
                    diverted_driver_id = best["driver_id"]
                    logger.info(
                        "diversion_offered delivery=%s driver=%s gain_min=%.1f vs=%s",
                        delivery_id,
                        best["driver_id"],
                        best["gain_minutes"],
                        best["committed_order_number"],
                    )
                else:
                    logger.warning(
                        "diversion offer not delivered delivery=%s driver=%s -- " "falling back to full broadcast",
                        delivery_id,
                        best["driver_id"],
                    )
        finally:
            # ALWAYS hand off to the broadcast (review fix 4): the eval loop
            # above makes one real matrix call per candidate driver inside a
            # 50s soft time limit, so a worker kill or any unexpected
            # exception in the offer-push block must never strand this
            # enqueue -- a pool order must never go undiscoverable. The
            # diverted driver is excluded only when they actually received
            # the targeted offer, so exactly one message per driver carries
            # an Accept button.
            try:
                notify_staff_new_order.delay(delivery.order_id, exclude_driver_user_id=diverted_driver_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("new-order broadcast enqueue failed for order %s: %s", delivery.order_id, exc)

        if best:
            return {"suggested": True, **best}
        return {"suggested": False, "reason": "no_diversion_gain"}

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

        # Verify bottle-session continuity early so we fail fast instead of
        # retrying through the full OrderService path. Strict mode raises;
        # legacy mode logs a warning. Same guard runs again inside
        # delivery_service.update_delivery_status as a safety net.
        from business_app.services.bottle_tracking_service import BottleTrackingService

        BottleTrackingService().assert_driver_can_progress_delivery(delivery)

        # Always transition through OrderService so cash-order inventory deduction
        # and status history run consistently.
        order_status = delivery.order.status.value if hasattr(delivery.order.status, "value") else delivery.order.status
        if order_status != OrderStatus.DELIVERED.value:
            from business_app.services.order_service import OrderService

            # Pass the delivering driver as the actor so COD cash settled at
            # delivery always has a collector fallback.
            OrderService().update_order_status(
                delivery.order_id, OrderStatus.DELIVERED, updated_by=delivery.delivery_person_id
            )
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

    except ValidationError as exc:
        # Invariant violation (e.g. bottle-session mismatch) — retrying
        # won't fix it. Fail the task so the caller / operator sees it.
        logger.error(
            "Validation error processing delivery confirmation %s: %s",
            delivery_id,
            exc,
        )
        return {"success": False, "delivery_id": delivery_id, "error": str(exc)}
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
