"""
Delivery service for the Water Business Platform
Handles delivery scheduling, route optimization, and tracking
"""

from datetime import datetime, timezone, timedelta, UTC
from typing import List, Dict, Any, Optional, Tuple
from flask import current_app

from business_app.models.delivery import (
    Delivery,
    DeliveryPerson,
    DeliveryStatusHistory,
    DeliveryTimeSlot,
)
from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils.exceptions import ValidationError, NotFoundError, DeliveryError
from business_app.utils.state_validators import assert_delivery_person_for_status
from business_app.utils.constants import DeliveryType, DELIVERY_ZONES
from shared.enums import DeliveryStatus, OrderStatus
from shared.status_transitions import is_valid_delivery_transition
from business_app.utils.helpers import (
    calculate_distance,
    get_time_slots,
)
from shared.constants import TASHKENT_COORDINATES
from business_app import db


class DeliveryService:
    """Service for managing deliveries"""

    def __init__(self):
        self.default_delivery_fee = current_app.config.get("DEFAULT_DELIVERY_FEE", 5000)
        self.free_delivery_threshold = current_app.config.get("FREE_DELIVERY_THRESHOLD", 50000)
        self.max_delivery_distance = current_app.config.get("DELIVERY_RADIUS_KM", 50)
        self.store_latitude = TASHKENT_COORDINATES["latitude"]
        self.store_longitude = TASHKENT_COORDINATES["longitude"]

    @staticmethod
    def _normalize_actor_id(actor_user_id: Optional[int]) -> Optional[int]:
        """Normalize JWT/string actor ids to integers for model comparisons."""
        if actor_user_id is None:
            return None
        return int(actor_user_id)

    def create_delivery(
        self, order_id: int, delivery_type: DeliveryType = DeliveryType.STANDARD, scheduled_time_slot: str = None
    ) -> Delivery:
        """
        Create delivery for an order

        Args:
            order_id: Order ID
            delivery_type: Type of delivery
            scheduled_time_slot: Scheduled delivery time slot

        Returns:
            Delivery object

        Raises:
            NotFoundError: If order not found
            ValidationError: If delivery cannot be created
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        # Check if delivery already exists
        existing_delivery = Delivery.query.filter_by(order_id=order_id).first()
        if existing_delivery:
            raise ValidationError("Delivery already exists for this order")

        # Calculate delivery distance
        if not order.delivery_address:
            raise ValidationError("Order has no delivery address")

        distance = calculate_distance(
            self.store_latitude, self.store_longitude, order.delivery_address.latitude, order.delivery_address.longitude
        )

        # Check if within delivery range
        if distance > self.max_delivery_distance:
            raise DeliveryError(f"Delivery address is outside our delivery range ({self.max_delivery_distance} km)")

        # Determine delivery zone
        self._get_delivery_zone(distance)

        # Estimate delivery time
        estimated_time = self._calculate_estimated_delivery_time(distance, delivery_type)

        # Create delivery record
        delivery = Delivery(
            order_id=order_id,
            status=DeliveryStatus.SCHEDULED,
            distance_km=round(distance, 2),
            estimated_delivery_time=estimated_time,
            scheduled_date=order.delivery_date or datetime.now(UTC),
            scheduled_time_slot=scheduled_time_slot or "09:00-12:00",
        )

        db.session.add(delivery)
        db.session.commit()

        # Schedule delivery assignment
        self._schedule_delivery_assignment(delivery.id)

        # Broadcast the new pool order to every eligible driver with an
        # inline Accept/Decline UX. First driver to Accept wins (server-side
        # row lock in StaffService.accept_order returns 409 to the rest).
        if delivery.delivery_person_id is None:
            try:
                from ..tasks.staff_tasks import notify_staff_new_order

                notify_staff_new_order.delay(order_id)
            except Exception as exc:  # noqa: BLE001
                current_app.logger.warning("Failed to enqueue new-order broadcast for order %s: %s", order_id, exc)

            # Also evaluate whether this delivery is a particularly cheap
            # detour for one specific active driver — if so, that driver
            # gets an additional targeted suggestion message with detour
            # info (+km, +min). The broadcast above gives everyone visibility;
            # this targeted message just adds context for the best fit.
            try:
                from ..tasks.delivery_tasks import evaluate_pool_insertion_suggestions_task

                evaluate_pool_insertion_suggestions_task.delay(delivery.id)
            except Exception as exc:  # noqa: BLE001
                current_app.logger.warning(
                    "Failed to enqueue pool insertion eval for delivery %s: %s", delivery.id, exc
                )

        return delivery

    def assign_delivery_driver(self, delivery_id: int, driver_id: int) -> Delivery:
        """Assign delivery to a driver"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        driver = User.query.filter_by(id=driver_id, role="delivery_driver").first()
        if not driver:
            raise NotFoundError("Driver not found")

        # Check if driver is available
        if not self._is_driver_available(driver_id):
            raise ValidationError("Driver is not available")

        # ARCH-006: enforce assigned-person invariant before flipping status.
        assert_delivery_person_for_status(
            delivery,
            DeliveryStatus.ASSIGNED,
            delivery_person_id=driver_id,
        )

        # Assign driver
        delivery.driver_id = driver_id
        delivery.status = DeliveryStatus.ASSIGNED
        delivery.assigned_at = datetime.now(timezone.utc)

        db.session.commit()

        # Notify driver
        self._notify_driver(delivery)

        # Re-optimize route now that a new delivery has joined the driver's set.
        self._optimize_driver_route(driver_id, trigger="accept")

        return delivery

    def update_delivery_status(
        self,
        delivery_id: int,
        new_status: DeliveryStatus,
        driver_id: int = None,
        notes: str = None,
        current_location: Tuple[float, float] = None,
        sync_order_status: bool = True,
        commit: bool = True,
    ) -> Delivery:
        """Update delivery status

        Args:
            delivery_id: ID of the delivery to update
            new_status: New delivery status
            driver_id: Optional driver ID who made the update
            notes: Optional notes about the status change
            current_location: Optional current location tuple (lat, lon)
            sync_order_status: If True, update associated order status when delivery
                             is completed. Set to False when called from OrderService
                             to prevent circular callbacks.
            commit: When False the caller owns the transaction boundary —
                no commit is issued and post-commit side-effects
                (notification dispatch) are skipped so a rolled-back
                outer transaction does not fire stale events.
        """
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        # Validate status transition
        if not self._is_valid_delivery_status_transition(delivery.status, new_status):
            raise ValidationError(f"Cannot change status from {delivery.status.value} to {new_status.value}")

        # ARCH-006: any state from ASSIGNED onward needs a delivery person on file.
        assert_delivery_person_for_status(delivery, new_status)

        # Bottle-session continuity guard. Mirrors the check in
        # StaffService.update_delivery_status — covers the driver-app and any
        # other callers that reach this path directly.
        if new_status in (
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
        ):
            from .bottle_tracking_service import BottleTrackingService

            BottleTrackingService().assert_driver_can_progress_delivery(delivery)

        # Update delivery
        old_status = delivery.status
        delivery.status = new_status
        delivery.updated_at = datetime.now(timezone.utc)

        # Update status-specific fields
        self._update_delivery_status_fields(delivery, new_status, current_location)

        # Create status history
        history = self._create_delivery_status_history(delivery_id, old_status, new_status, driver_id, notes)
        db.session.flush()

        if commit:
            db.session.commit()

            # Handle status-specific actions (notification dispatch + optional
            # order-status sync). Skipped when commit is deferred to the
            # caller so the orchestrator owns the post-commit work.
            self._handle_delivery_status_change(
                delivery,
                new_status,
                sync_order_status,
                history_id=history.id,
            )

        return delivery

    def begin_delivery_in_transit(
        self,
        delivery_id: int,
        *,
        actor_user_id: int = None,
        required_driver_id: int = None,
        notes: str = None,
    ) -> Delivery:
        """Advance an assigned delivery directly to in-transit for legacy entrypoints."""
        actor_user_id = self._normalize_actor_id(actor_user_id)
        required_driver_id = self._normalize_actor_id(required_driver_id)
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        if required_driver_id is not None and delivery.delivery_person_id != required_driver_id:
            raise NotFoundError("Delivery not found or not assigned")

        if delivery.status != DeliveryStatus.ASSIGNED:
            raise ValidationError("Cannot start delivery at the current stage")

        from .bottle_tracking_service import BottleTrackingService

        BottleTrackingService().assert_driver_can_progress_delivery(delivery)

        now = datetime.now(timezone.utc)
        delivery.status = DeliveryStatus.IN_TRANSIT
        delivery.updated_at = now
        delivery.route_data = delivery.route_data or {}
        delivery.route_data.setdefault("picked_up_at", now.isoformat())
        delivery.route_data["in_transit_at"] = now.isoformat()

        history = DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=DeliveryStatus.ASSIGNED,
            new_status=DeliveryStatus.IN_TRANSIT,
            changed_by=actor_user_id,
            changed_at=now,
            notes=notes or "Delivery started",
        )
        db.session.add(history)
        db.session.flush()
        db.session.commit()

        self._enqueue_delivery_status_notification(history.id)
        return delivery

    def _capture_arrival_position(
        self,
        delivery: Delivery,
        history: DeliveryStatusHistory,
    ) -> Optional[Tuple[float, float]]:
        """Stamp the delivery's destination coords onto the status-history row
        and refresh the driver's live location *if it's stale or missing*.

        Called from ARRIVED/DELIVERED transitions. The delivery address is the
        best available proxy for where the driver physically is at this
        moment. Fresh GPS readings (within ``DRIVER_LOCATION_FRESH_SECONDS``)
        are NOT overwritten — real GPS is more precise than an address
        centroid.

        Returns the (lat, lng) it stamped, or None if no destination coords
        are available on the delivery's order address.
        """
        order = delivery.order
        addr = order.delivery_address if order else None
        if addr is None or addr.latitude is None or addr.longitude is None:
            return None

        lat, lng = addr.latitude, addr.longitude

        # Always populate the history columns — `RouteOptimizationService.
        # _resolve_start_point` already reads them for its `last_completed`
        # fallback, so until now that fallback path was effectively dead.
        history.location_lat = lat
        history.location_lng = lng

        # Refresh DeliveryPerson live location only if not already fresh.
        if delivery.delivery_person_id is None:
            return (lat, lng)
        person = DeliveryPerson.query.filter_by(user_id=delivery.delivery_person_id).first()
        if person is None:
            return (lat, lng)

        fresh_seconds = current_app.config.get("DRIVER_LOCATION_FRESH_SECONDS", 1800)
        last_update = person.last_location_update
        if last_update is not None and last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=timezone.utc)
        is_fresh = last_update is not None and last_update >= datetime.now(timezone.utc) - timedelta(
            seconds=fresh_seconds
        )
        if not is_fresh:
            person.update_location(lat, lng)
        return (lat, lng)

    def mark_delivery_arrived(
        self,
        delivery_id: int,
        *,
        actor_user_id: int = None,
        required_driver_id: int = None,
        notes: str = None,
        automatic: bool = False,
    ) -> Delivery:
        """Mark an in-transit delivery as arrived via a canonical history event."""
        actor_user_id = self._normalize_actor_id(actor_user_id)
        required_driver_id = self._normalize_actor_id(required_driver_id)
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        if required_driver_id is not None and delivery.delivery_person_id != required_driver_id:
            raise NotFoundError("Delivery not found or not assigned")

        if delivery.status != DeliveryStatus.IN_TRANSIT:
            raise ValidationError("Delivery must be in transit to mark as arrived")

        from .bottle_tracking_service import BottleTrackingService

        BottleTrackingService().assert_driver_can_progress_delivery(delivery)

        now = datetime.now(timezone.utc)
        delivery.status = DeliveryStatus.ARRIVED
        delivery.updated_at = now
        delivery.route_data = delivery.route_data or {}
        delivery.route_data["arrived_at"] = now.isoformat()

        history = DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=DeliveryStatus.IN_TRANSIT,
            new_status=DeliveryStatus.ARRIVED,
            changed_by=actor_user_id,
            changed_at=now,
            notes=notes or "Marked as arrived",
            automatic=automatic,
        )
        db.session.add(history)
        # Stamp the arrival position onto the history row and (if stale) the
        # driver's live location, before flush so it's persisted atomically.
        self._capture_arrival_position(delivery, history)
        db.session.flush()
        db.session.commit()

        self._enqueue_delivery_status_notification(history.id)

        # Re-optimize remaining stops from the new origin (best-effort).
        if delivery.delivery_person_id is not None:
            try:
                from business_app.tasks.delivery_tasks import optimize_driver_route_task

                optimize_driver_route_task.delay(delivery.delivery_person_id, "arrival")
            except Exception as exc:  # noqa: BLE001 — non-critical
                current_app.logger.warning(
                    "post-arrival route optimization enqueue failed for driver=%s: %s",
                    delivery.delivery_person_id,
                    exc,
                )
        return delivery

    def calculate_delivery_fee(self, latitude: float, longitude: float, order_total: int) -> int:
        """Calculate delivery fee based on location and order total"""
        if order_total >= self.free_delivery_threshold:
            return 0

        # distance = calculate_distance(
        #     self.store_latitude, self.store_longitude,
        #     latitude, longitude
        # )

        # # Get zone-based fee
        # zone = self._get_delivery_zone(distance)
        # zone_info = DELIVERY_ZONES.get(zone, DELIVERY_ZONES['OUTER'])

        # return zone_info['fee']

        # We are offering free delivery for all orders for now
        return 0

    def get_available_time_slots(
        self, date: datetime = None, delivery_type: DeliveryType = DeliveryType.STANDARD
    ) -> List[str]:
        """Get available delivery time slots for a date"""
        if date is None:
            date = datetime.now(UTC).date()

        # Get base time slots
        time_slots = get_time_slots()

        # For express delivery, filter to next few hours
        if delivery_type == DeliveryType.EXPRESS:
            now = datetime.now(UTC)
            if date == now.date():
                # Only show slots 2+ hours from now for express
                now.time()
                time_slots = [
                    slot for slot in time_slots if self._parse_time_slot(slot)[0] >= (now + timedelta(hours=2)).time()
                ]

        # Check capacity for each slot
        available_slots = []
        for slot in time_slots:
            if self._check_slot_capacity(date, slot):
                available_slots.append(slot)

        return available_slots

    def get_time_slot_availability(self, target_date) -> List[Dict[str, Any]]:
        """Return delivery slot capacity details for a specific booking date."""
        booking_rows = (
            db.session.query(
                Order.delivery_time_slot,
                db.func.count(Order.id),
            )
            .filter(
                Order.delivery_date >= target_date,
                Order.delivery_date < target_date + timedelta(days=1),
                Order.delivery_time_slot.isnot(None),
            )
            .group_by(Order.delivery_time_slot)
            .all()
        )
        bookings_by_slot = {slot_label: count for slot_label, count in booking_rows}

        slots_data = []
        for slot in DeliveryTimeSlot.query.filter_by(is_active=True).all():
            if not slot.is_available_on_date(target_date):
                continue

            current_bookings = bookings_by_slot.get(
                f"{slot.start_time}-{slot.end_time}",
                0,
            )
            available_capacity = slot.max_orders - current_bookings
            delivery_fee = float(slot.delivery_fee)
            premium_fee = float(slot.premium_fee) if slot.is_premium else 0

            slots_data.append(
                {
                    "id": slot.id,
                    "name": slot.name,
                    "start_time": slot.start_time,
                    "end_time": slot.end_time,
                    "time_range": f"{slot.start_time}-{slot.end_time}",
                    "delivery_fee": delivery_fee,
                    "is_premium": slot.is_premium,
                    "premium_fee": premium_fee,
                    "total_fee": delivery_fee + premium_fee,
                    "available_capacity": available_capacity,
                    "is_available": available_capacity > 0,
                }
            )

        return slots_data

    def track_delivery(self, tracking_code: str) -> Dict[str, Any]:
        """Get delivery tracking information"""
        delivery = Delivery.query.filter_by(tracking_code=tracking_code).first()
        if not delivery:
            raise NotFoundError("Delivery not found")

        return {
            "tracking_code": delivery.tracking_code,
            "status": delivery.status.value,
            "order_number": delivery.order.order_number,
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "current_location": (
                {"latitude": delivery.current_latitude, "longitude": delivery.current_longitude}
                if delivery.current_latitude and delivery.current_longitude
                else None
            ),
            "delivery_address": {"street": delivery.delivery_address_street, "city": delivery.delivery_address_city},
            "driver": (
                {"name": f"{delivery.driver.first_name} {delivery.driver.last_name}", "phone": delivery.driver.phone}
                if delivery.driver
                else None
            ),
            "timeline": [
                {
                    "status": history.new_status.value,
                    "timestamp": history.changed_at.isoformat(),
                    "notes": history.notes,
                }
                for history in delivery.status_history
            ],
        }

    def get_delivery_metrics(self, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
        """Get delivery performance metrics"""
        query = Delivery.query

        if start_date:
            query = query.filter(Delivery.created_at >= start_date)
        if end_date:
            query = query.filter(Delivery.created_at <= end_date)

        deliveries = query.all()

        # Calculate metrics
        total_deliveries = len(deliveries)
        completed_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.DELIVERED])
        failed_deliveries = len([d for d in deliveries if d.status == DeliveryStatus.FAILED])

        # Average delivery time
        completed_with_times = [d for d in deliveries if d.delivered_at and d.assigned_at]
        avg_delivery_time = None
        if completed_with_times:
            total_time = sum((d.delivered_at - d.assigned_at).total_seconds() for d in completed_with_times)
            avg_delivery_time = total_time / len(completed_with_times) / 60  # in minutes

        # On-time delivery rate
        on_time_deliveries = len([d for d in completed_with_times if d.delivered_at <= d.estimated_delivery_time])
        on_time_rate = (on_time_deliveries / len(completed_with_times)) * 100 if completed_with_times else 0

        return {
            "total_deliveries": total_deliveries,
            "completed_deliveries": completed_deliveries,
            "failed_deliveries": failed_deliveries,
            "completion_rate": (completed_deliveries / total_deliveries) * 100 if total_deliveries > 0 else 0,
            "failure_rate": (failed_deliveries / total_deliveries) * 100 if total_deliveries > 0 else 0,
            "average_delivery_time_minutes": round(avg_delivery_time, 2) if avg_delivery_time else None,
            "on_time_delivery_rate": round(on_time_rate, 2),
            "zone_breakdown": self._get_zone_breakdown(deliveries),
        }

    def optimize_routes(self, date: datetime = None) -> Dict[str, Any]:
        """Optimize delivery routes for a given date"""
        if date is None:
            date = datetime.now(UTC).date()

        # Get pending deliveries for the date
        start_of_day = datetime.combine(date, datetime.min.time())
        end_of_day = datetime.combine(date, datetime.max.time())

        deliveries = Delivery.query.filter(
            Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.ASSIGNED]),
            Delivery.created_at.between(start_of_day, end_of_day),
        ).all()

        if not deliveries:
            return {"message": "No deliveries to optimize", "routes": []}

        # Group deliveries by zone and create optimized routes
        routes = self._create_optimized_routes(deliveries)

        return {
            "date": date.isoformat(),
            "total_deliveries": len(deliveries),
            "routes": routes,
            "optimization_summary": {
                "total_routes": len(routes),
                "total_distance_km": sum(route["total_distance_km"] for route in routes),
                "estimated_total_time_hours": sum(route["estimated_time_hours"] for route in routes),
            },
        }

    def complete_delivery(
        self,
        delivery_id: int,
        driver_id: int = None,
        proof_photo: str = None,
        customer_signature: str = None,
        sync_order_status: bool = True,
        commit: bool = True,
    ) -> Delivery:
        """Mark delivery as completed

        Args:
            delivery_id: ID of the delivery to complete
            driver_id: Optional driver ID who completed the delivery
            proof_photo: Optional proof of delivery photo
            customer_signature: Optional customer signature
            sync_order_status: If True, update associated order status to DELIVERED.
                             Set to False when called from OrderService to prevent
                             circular callbacks.
            commit: When False the caller owns the transaction boundary;
                changes are flushed but not committed.
        """
        delivery = self.update_delivery_status(
            delivery_id,
            DeliveryStatus.DELIVERED,
            driver_id,
            "Delivery completed successfully",
            sync_order_status=sync_order_status,
            commit=commit,
        )

        # Add completion details
        delivery.delivered_at = datetime.now(timezone.utc)
        delivery.proof_of_delivery_photo = proof_photo
        delivery.customer_signature = customer_signature

        if commit:
            db.session.commit()

        return delivery

    def cancel_delivery(self, delivery_id: int, reason: str = None) -> Delivery:
        """Cancel delivery"""
        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        if delivery.status in [
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.RETURNED,
        ]:
            raise ValidationError("Cannot cancel delivery once it is in progress or completed")

        delivery = self.update_delivery_status(
            delivery_id,
            DeliveryStatus.CANCELLED,
            notes=reason or "Delivery cancelled because the order was cancelled",
            sync_order_status=False,
        )
        pending_commit = False
        if reason:
            delivery.delivery_notes = reason
            pending_commit = True

        cancelled_driver_id = delivery.delivery_person_id
        if cancelled_driver_id:
            from business_app.services.staff_service import StaffService

            StaffService.sync_active_delivery_counters([cancelled_driver_id])
            pending_commit = True

        if pending_commit:
            db.session.commit()

        # Notify customer and driver
        self._notify_delivery_cancellation(delivery)

        # Re-optimize the affected driver's remaining route now that this stop
        # has been pulled.
        if cancelled_driver_id:
            self._optimize_driver_route(cancelled_driver_id, trigger="cancel")

        return delivery

    # Private helper methods
    def _get_delivery_zone(self, distance_km: float) -> str:
        """Determine delivery zone based on distance"""
        for zone, info in DELIVERY_ZONES.items():
            if distance_km <= info["radius"]:
                return zone
        return "OUTER"

    def _calculate_estimated_delivery_time(self, distance_km: float, delivery_type: DeliveryType) -> datetime:
        """Calculate estimated delivery time"""
        base_time = datetime.now(timezone.utc)

        if delivery_type == DeliveryType.EXPRESS:
            # Express: 1-2 hours
            estimated_minutes = 60 + (distance_km * 2)
        elif delivery_type == DeliveryType.EMERGENCY:
            # Emergency: 30-60 minutes
            estimated_minutes = 30 + distance_km
        else:
            # Standard: 2-4 hours
            estimated_minutes = 120 + (distance_km * 3)

        return base_time + timedelta(minutes=estimated_minutes)

    def _is_driver_available(self, driver_id: int) -> bool:
        """Check if driver is available for assignment.

        The historical concurrent-deliveries cap was removed when implicit
        route optimization was introduced — drivers may now claim as many
        deliveries as needed and the optimizer handles ordering. The
        `DeliveryPerson.max_concurrent_deliveries` column is preserved for a
        possible future per-driver admin override but is no longer enforced.
        """
        return True

    def _is_valid_delivery_status_transition(self, current: DeliveryStatus, new: DeliveryStatus) -> bool:
        """Check if delivery status transition is valid (delegates to shared.status_transitions)."""
        return is_valid_delivery_transition(current, new)

    def _update_delivery_status_fields(
        self, delivery: Delivery, new_status: DeliveryStatus, current_location: Tuple[float, float] = None
    ):
        """Update status-specific fields"""
        now = datetime.now(timezone.utc)

        if new_status == DeliveryStatus.ASSIGNED:
            delivery.assigned_at = now
        elif new_status == DeliveryStatus.PICKED_UP:
            delivery.picked_up_at = now
        elif new_status == DeliveryStatus.IN_TRANSIT:
            delivery.in_transit_at = now
        elif new_status == DeliveryStatus.ARRIVED:
            delivery.arrived_at = now
        elif new_status == DeliveryStatus.DELIVERED:
            delivery.delivered_at = now

        # Update current location if provided
        if current_location:
            delivery.current_latitude, delivery.current_longitude = current_location
            delivery.last_location_update = now

    def _create_delivery_status_history(
        self,
        delivery_id: int,
        old_status: DeliveryStatus,
        new_status: DeliveryStatus,
        changed_by: int = None,
        notes: str = None,
    ):
        """Create delivery status history record"""
        history = DeliveryStatusHistory(
            delivery_id=delivery_id,
            old_status=old_status,
            new_status=new_status,
            changed_by=changed_by,
            notes=notes,
            changed_at=datetime.now(timezone.utc),
        )

        db.session.add(history)
        return history

    def _handle_delivery_status_change(
        self, delivery: Delivery, new_status: DeliveryStatus, sync_order_status: bool = True, history_id: int = None
    ):
        """Handle actions when delivery status changes

        Args:
            delivery: The delivery object
            new_status: The new delivery status
            sync_order_status: If True, update associated order status when delivery
                             is completed. Set to False when this was triggered by
                             OrderService to prevent circular callbacks.
            history_id: Committed delivery status history ID for event-driven notifications.
        """
        # Send notifications
        if history_id is not None:
            self._enqueue_delivery_status_notification(history_id)

        # Update order status if delivery is completed AND sync is enabled
        if new_status == DeliveryStatus.DELIVERED and sync_order_status:
            from .order_service import OrderService

            order_service = OrderService()
            order_service.update_order_status(delivery.order_id, OrderStatus.DELIVERED)

    def _enqueue_delivery_status_notification(self, history_id: int):
        """Enqueue one canonical delivery-status notification for a committed history event."""
        from ..tasks.notification_tasks import send_delivery_update_task

        send_delivery_update_task.delay(history_id)

    def _schedule_delivery_assignment(self, delivery_id: int):
        """Schedule automatic delivery assignment"""
        from ..tasks.delivery_tasks import auto_assign_delivery_task

        # Assign delivery automatically after 5 minutes
        auto_assign_delivery_task.apply_async(args=[delivery_id], countdown=300)

    def _notify_driver(self, delivery: Delivery):
        """Notify driver of new delivery assignment"""
        from ..tasks.notification_tasks import notify_driver_assignment_task

        notify_driver_assignment_task.delay(delivery.id)

    def _optimize_driver_route(self, driver_id: int, trigger: str = "auto"):
        """Optimize route for a specific driver (async)."""
        from ..tasks.delivery_tasks import optimize_driver_route_task

        optimize_driver_route_task.delay(driver_id, trigger)

    def _parse_time_slot(self, time_slot: str) -> Tuple[datetime.time, datetime.time]:
        """Parse time slot string into start and end times"""
        start_str, end_str = time_slot.split("-")
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()
        return start_time, end_time

    def _check_slot_capacity(self, date: datetime.date, time_slot: str) -> bool:
        """Check if time slot has available capacity"""
        # Get deliveries scheduled for this slot
        start_of_day = datetime.combine(date, datetime.min.time())
        end_of_day = datetime.combine(date, datetime.max.time())

        slot_deliveries = Delivery.query.filter(
            Delivery.scheduled_time_slot == time_slot,
            Delivery.created_at.between(start_of_day, end_of_day),
            Delivery.status.notin_([DeliveryStatus.FAILED, DeliveryStatus.CANCELLED]),
        ).count()

        # Allow up to 20 deliveries per time slot
        return slot_deliveries < 20

    def _get_zone_breakdown(self, deliveries: List[Delivery]) -> Dict[str, int]:
        """Get delivery count breakdown by zone"""
        breakdown = {}
        for delivery in deliveries:
            zone = delivery.delivery_zone
            breakdown[zone] = breakdown.get(zone, 0) + 1
        return breakdown

    def _create_optimized_routes(self, deliveries: List[Delivery]) -> List[Dict[str, Any]]:
        """Create optimized delivery routes"""
        # Group by zone first
        zone_groups = {}
        for delivery in deliveries:
            zone = delivery.delivery_zone
            if zone not in zone_groups:
                zone_groups[zone] = []
            zone_groups[zone].append(delivery)

        routes = []
        for zone, zone_deliveries in zone_groups.items():
            # Simple optimization: sort by proximity
            optimized_order = self._optimize_delivery_order(zone_deliveries)

            route = {
                "zone": zone,
                "deliveries": [
                    {
                        "id": d.id,
                        "tracking_code": d.tracking_code,
                        "order_number": d.order.order_number,
                        "address": d.delivery_address_street,
                        "latitude": d.delivery_address_latitude,
                        "longitude": d.delivery_address_longitude,
                        "estimated_time": d.estimated_delivery_time.isoformat(),
                    }
                    for d in optimized_order
                ],
                "total_distance_km": self._calculate_route_distance(optimized_order),
                "estimated_time_hours": len(optimized_order) * 0.5,  # 30 minutes per delivery
            }
            routes.append(route)

        return routes

    def _optimize_delivery_order(self, deliveries: List[Delivery]) -> List[Delivery]:
        """Optimize order of deliveries using simple nearest neighbor algorithm"""
        if not deliveries:
            return []

        # Start from store location
        current_lat, current_lon = self.store_latitude, self.store_longitude
        remaining = deliveries.copy()
        optimized = []

        while remaining:
            # Find nearest delivery
            nearest = min(
                remaining,
                key=lambda d: calculate_distance(
                    current_lat, current_lon, d.delivery_address_latitude, d.delivery_address_longitude
                ),
            )

            optimized.append(nearest)
            remaining.remove(nearest)
            current_lat, current_lon = nearest.delivery_address_latitude, nearest.delivery_address_longitude

        return optimized

    def _calculate_route_distance(self, deliveries: List[Delivery]) -> float:
        """Calculate total distance for a delivery route"""
        if not deliveries:
            return 0

        total_distance = 0
        current_lat, current_lon = self.store_latitude, self.store_longitude

        for delivery in deliveries:
            distance = calculate_distance(
                current_lat, current_lon, delivery.delivery_address_latitude, delivery.delivery_address_longitude
            )
            total_distance += distance
            current_lat, current_lon = delivery.delivery_address_latitude, delivery.delivery_address_longitude

        # Add return distance to store
        total_distance += calculate_distance(current_lat, current_lon, self.store_latitude, self.store_longitude)

        return round(total_distance, 2)

    def get_delivery_zones(self) -> List[Dict[str, Any]]:
        """Return configured delivery zones for API/UI consumption."""
        zones = []
        for zone_name, info in DELIVERY_ZONES.items():
            zones.append(
                {
                    "name": zone_name,
                    "max_distance_km": info.get("max_distance", 0),
                    "fee": info.get("fee", self.default_delivery_fee),
                    "estimated_time_minutes": info.get("estimated_time", 0),
                }
            )
        return zones

    def _notify_delivery_cancellation(self, delivery: Delivery):
        """Notify about delivery cancellation"""
        from ..tasks.notification_tasks import notify_delivery_cancellation_task

        notify_delivery_cancellation_task.delay(delivery.id)
