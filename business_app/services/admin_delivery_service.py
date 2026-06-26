"""Admin delivery management service."""

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import aliased, joinedload, selectinload

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order, OrderItem
from business_app.models.user import User, UserAddress
from business_app.services.staff_service import StaffService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from business_app.utils.exceptions import NotFoundError, ValidationError


ACTIVE_DELIVERY_STATUSES = {
    DeliveryStatus.ASSIGNED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT,
    DeliveryStatus.ARRIVED,
}

TERMINAL_DELIVERY_STATUSES = {
    DeliveryStatus.DELIVERED,
    DeliveryStatus.FAILED,
    DeliveryStatus.CANCELLED,
    DeliveryStatus.RETURNED,
}


class AdminDeliveryService:
    """Business/query logic for admin delivery management."""

    STATUS_ALIASES = {
        "scheduled": DeliveryStatus.SCHEDULED,
        "pending": DeliveryStatus.PENDING,
        "assigned": DeliveryStatus.ASSIGNED,
        "picked_up": DeliveryStatus.PICKED_UP,
        "in_transit": DeliveryStatus.IN_TRANSIT,
        "arrived": DeliveryStatus.ARRIVED,
        "delivered": DeliveryStatus.DELIVERED,
        "failed": DeliveryStatus.FAILED,
        "cancelled": DeliveryStatus.CANCELLED,
        "returned": DeliveryStatus.RETURNED,
    }

    ADMIN_ALLOWED_TRANSITIONS = {
        DeliveryStatus.SCHEDULED: {
            DeliveryStatus.PENDING,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.PENDING: {
            DeliveryStatus.ASSIGNED,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.ASSIGNED: {
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.PICKED_UP: {
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.FAILED,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.IN_TRANSIT: {
            DeliveryStatus.ARRIVED,
            DeliveryStatus.FAILED,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.ARRIVED: {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
            DeliveryStatus.RETURNED,
        },
        DeliveryStatus.DELIVERED: set(),
        DeliveryStatus.FAILED: set(),
        DeliveryStatus.CANCELLED: set(),
        DeliveryStatus.RETURNED: set(),
    }

    @staticmethod
    def list_deliveries(
        *,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        status: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List deliveries with filters and summary stats for the admin UI."""
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)

        query = AdminDeliveryService._build_filtered_query(
            search=search,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )

        ordered_query = query.options(
            joinedload(Delivery.order).joinedload(Order.user),
            joinedload(Delivery.order).joinedload(Order.delivery_address),
            joinedload(Delivery.order).selectinload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Delivery.delivery_person),
            selectinload(Delivery.status_history).joinedload(DeliveryStatusHistory.changed_by_user),
        ).order_by(Delivery.scheduled_date.desc(), Delivery.id.desc())

        pagination = ordered_query.paginate(page=page, per_page=per_page, error_out=False)
        summary = AdminDeliveryService._build_summary(query)

        return {
            "items": [AdminDeliveryService.serialize_delivery(delivery) for delivery in pagination.items],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "summary": summary,
        }

    @staticmethod
    def update_delivery(delivery_id: int, payload: Dict[str, Any], actor_id: int) -> Dict[str, Any]:
        """Update delivery notes and/or status from the admin panel."""
        payload = payload or {}
        delivery = Delivery.query.options(
            joinedload(Delivery.order),
            joinedload(Delivery.status_history).joinedload(DeliveryStatusHistory.changed_by_user),
            joinedload(Delivery.delivery_person),
        ).get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")

        notes_provided = "notes" in payload
        new_notes = payload.get("notes") if notes_provided else None
        requested_status = payload.get("status")
        status_changed = False

        if notes_provided:
            delivery.delivery_notes = (new_notes or "").strip() or None

        if requested_status:
            new_status = AdminDeliveryService._normalize_status(requested_status)
            if new_status != delivery.status:
                AdminDeliveryService._apply_status_update(
                    delivery=delivery,
                    new_status=new_status,
                    actor_id=actor_id,
                    notes=delivery.delivery_notes,
                    fail_reason=(payload.get("fail_reason") or "").strip() or None,
                    cash_collected=payload.get("cash_collected"),
                )
                status_changed = True

        if notes_provided and not status_changed:
            delivery.updated_at = datetime.now(UTC)
            db.session.commit()

        return AdminDeliveryService.serialize_delivery(delivery)

    @staticmethod
    def redispatch_delivery(delivery_id: int, actor_id: int, *, reason: Optional[str] = None) -> Dict[str, Any]:
        """Re-dispatch a failed delivery back to the unassigned pool (admin panel
        entry point). Delegates the status check + return-to-pool to
        StaffService so the rule lives in a single place, then returns the
        refreshed serialized delivery for the admin UI."""
        StaffService.redispatch_failed_delivery(delivery_id, actor_id, reason=reason)
        delivery = Delivery.query.options(
            joinedload(Delivery.order),
            joinedload(Delivery.status_history).joinedload(DeliveryStatusHistory.changed_by_user),
            joinedload(Delivery.delivery_person),
        ).get(delivery_id)
        return AdminDeliveryService.serialize_delivery(delivery)

    @staticmethod
    def reassign_delivery(delivery_id: int, new_person_id: int, actor_id: int) -> Delivery:
        """Reassign a delivery to a different delivery person (admin override).

        Thin wrapper over the canonical DeliveryAssignmentService.assign_driver
        SSOT (source=REASSIGN, allow_in_progress=True) so the bottle binding,
        COD-block, capacity, counter sync, and history are handled once."""
        from business_app.services.delivery_assignment_service import DeliveryAssignmentService
        from shared.enums import AssignmentSource

        delivery = Delivery.query.get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found")
        if delivery.delivery_person_id == new_person_id:
            return delivery

        old_person_id = delivery.delivery_person_id

        new_profile = DeliveryPerson.query.filter_by(user_id=new_person_id).first()
        if not new_profile:
            raise NotFoundError("Delivery person not found")
        max_concurrent = new_profile.max_concurrent_deliveries or 3
        if StaffService.get_active_delivery_count(new_person_id) >= max_concurrent:
            raise ValidationError(f"Delivery person has reached max concurrent deliveries ({max_concurrent})")

        result = DeliveryAssignmentService.assign_driver(
            delivery_id,
            driver_user_id=new_person_id,
            actor_id=actor_id,
            source=AssignmentSource.REASSIGN,
            note=f"Reassigned from user {old_person_id} to user {new_person_id} by admin",
            allow_in_progress=True,
        )
        return result.delivery

    @staticmethod
    def serialize_delivery(delivery: Delivery) -> Dict[str, Any]:
        """Serialize a delivery record for the admin deliveries UI."""
        order = delivery.order
        customer = order.user if order else None
        address = order.delivery_address if order else None
        driver = delivery.delivery_person
        driver_profile = None
        if driver and getattr(driver, "delivery_person_profile", None):
            profile = driver.delivery_person_profile
            driver_profile = profile[0] if isinstance(profile, list) else profile

        history = sorted(
            list(delivery.status_history or []),
            key=lambda item: item.changed_at or datetime.min.replace(tzinfo=UTC),
        )

        return {
            "id": delivery.id,
            "delivery_id": AdminDeliveryService._format_delivery_code(delivery.id),
            "tracking_number": delivery.tracking_number,
            "order_id": delivery.order_id,
            "order_number": order.order_number if order else None,
            "status": AdminDeliveryService._status_value(delivery.status),
            "priority": AdminDeliveryService._derive_priority(order, delivery),
            "customer_name": customer.full_name if customer else None,
            "customer_phone": customer.phone if customer else None,
            "driver_id": driver.id if driver else None,
            "driver_name": (getattr(driver_profile, "full_name", None) or (driver.full_name if driver else None)),
            "driver_phone": (getattr(driver_profile, "phone", None) or (driver.phone if driver else None)),
            "delivery_address": address.full_address if address else None,
            "delivery_instructions": address.delivery_instructions if address else None,
            "scheduled_date": delivery.scheduled_date.isoformat() if delivery.scheduled_date else None,
            "scheduled_time_slot": delivery.scheduled_time_slot,
            "estimated_delivery_time": (
                delivery.estimated_delivery_time.isoformat() if delivery.estimated_delivery_time else None
            ),
            "actual_delivery_time": (
                delivery.actual_delivery_time.isoformat() if delivery.actual_delivery_time else None
            ),
            "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
            "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
            "updated_at": delivery.updated_at.isoformat() if delivery.updated_at else None,
            "distance_km": float(delivery.distance_km) if delivery.distance_km is not None else None,
            "estimated_duration_minutes": delivery.estimated_duration_minutes,
            "delivery_attempts": delivery.delivery_attempts or 0,
            "failed_delivery_reason": delivery.failed_delivery_reason,
            "notes": delivery.delivery_notes,
            "cash_collected": float(delivery.cash_collected) if delivery.cash_collected is not None else None,
            "order_total_amount": float(order.total_amount) if order and order.total_amount is not None else 0.0,
            "payment_method": (
                order.payment_method.value if order and getattr(order, "payment_method", None) else None
            ),
            "payment_status": (
                order.payment.status.value
                if order and getattr(order, "payment", None) and hasattr(order.payment.status, "value")
                else (str(order.payment.status) if order and getattr(order, "payment", None) else None)
            ),
            "amount_collected": (
                float(order.payment.amount_collected or 0) if order and getattr(order, "payment", None) else 0.0
            ),
            "outstanding_amount": (
                float(order.payment.outstanding_amount or 0) if order and getattr(order, "payment", None) else 0.0
            ),
            "items_summary": AdminDeliveryService._build_items_summary(order),
            "current_location": (
                {
                    "lat": delivery.current_location_lat,
                    "lng": delivery.current_location_lng,
                    "last_update": delivery.last_location_update.isoformat() if delivery.last_location_update else None,
                }
                if delivery.current_location_lat is not None and delivery.current_location_lng is not None
                else None
            ),
            "status_history": [
                {
                    "id": item.id,
                    "old_status": AdminDeliveryService._status_value(item.old_status),
                    "new_status": AdminDeliveryService._status_value(item.new_status),
                    "changed_at": item.changed_at.isoformat() if item.changed_at else None,
                    "notes": item.notes,
                    "reason": item.reason,
                    "changed_by": item.changed_by,
                    "changed_by_name": item.changed_by_user.full_name if item.changed_by_user else None,
                }
                for item in history
            ],
        }

    @staticmethod
    def _apply_status_update(
        *,
        delivery: Delivery,
        new_status: DeliveryStatus,
        actor_id: int,
        notes: Optional[str],
        fail_reason: Optional[str],
        cash_collected: Optional[Any],
    ) -> None:
        current_status = delivery.status
        if current_status not in AdminDeliveryService.ADMIN_ALLOWED_TRANSITIONS:
            raise ValidationError("Current delivery status is not supported")

        allowed_statuses = AdminDeliveryService.ADMIN_ALLOWED_TRANSITIONS[current_status]
        if new_status not in allowed_statuses:
            allowed = ", ".join(status.value for status in sorted(allowed_statuses, key=lambda item: item.value))
            raise ValidationError(
                f"Cannot transition delivery from {current_status.value} to {new_status.value}. "
                f"Allowed transitions: {allowed or 'none'}"
            )

        if (
            new_status in ACTIVE_DELIVERY_STATUSES.union({DeliveryStatus.DELIVERED, DeliveryStatus.FAILED})
            and not delivery.delivery_person_id
        ):
            raise ValidationError("Assign a driver before updating this delivery status")

        if new_status in {
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            DeliveryStatus.DELIVERED,
            DeliveryStatus.FAILED,
        }:
            metadata: Dict[str, Any] = {"notes": notes}
            if fail_reason:
                metadata["fail_reason"] = fail_reason
            if cash_collected is not None:
                metadata["cash_collected"] = cash_collected
            StaffService.update_delivery_status(
                delivery_id=delivery.id,
                new_status=new_status.value,
                staff_user_id=actor_id,
                metadata=metadata,
            )
            return

        now = datetime.now(UTC)
        old_status = delivery.status
        previous_driver_id = delivery.delivery_person_id
        delivery.status = new_status
        delivery.updated_at = now

        # Moving a delivery back toward the pool (returned / scheduled / pending)
        # must release its driver, otherwise it becomes a stranded row: hidden
        # from the driver's active list (status filter) and from the pool
        # (unassigned filter). Mirrors StaffService.return_delivery_to_pool and
        # the assert_unassigned_for_pool_status invariant.
        if new_status in {DeliveryStatus.RETURNED, DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING}:
            delivery.delivery_person_id = None

        if new_status == DeliveryStatus.RETURNED:
            AdminDeliveryService._release_driver_workload(delivery, driver_id=previous_driver_id)
            if delivery.order:
                delivery.order.status = OrderStatus.RETURNED
                delivery.order.updated_at = now
                if delivery.order.payment_method == PaymentMethod.CASH:
                    from business_app.services.cash_collection_service import CashCollectionService

                    cash_collection_service = CashCollectionService()
                    # Refund prepaid credit applied at order creation (full
                    # coverage) for an order returned before it was ever
                    # delivered; no-op for delivered-then-returned orders.
                    cash_collection_service.release_pre_delivery_prepaid_settlement_for_order(
                        order_id=delivery.order.id,
                        actor_user_id=actor_id,
                        reason="Order marked as returned via admin delivery workflow",
                    )
                    cash_collection_service.release_reserved_prepayment_for_order(
                        order_id=delivery.order.id,
                        actor_user_id=actor_id,
                        reason="Order marked as returned via admin delivery workflow",
                    )

        history = DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=old_status,
            new_status=new_status,
            changed_by=actor_id,
            changed_at=now,
            notes=notes or f"Updated via admin panel to {new_status.value}",
            reason=fail_reason,
        )
        db.session.add(history)
        db.session.commit()

    @staticmethod
    def _release_driver_workload(delivery: Delivery, *, driver_id: Optional[int] = None) -> None:
        # The caller may have already cleared delivery.delivery_person_id (when
        # returning the delivery toward the pool), so accept the previous driver
        # id explicitly and fall back to the live value otherwise.
        target = driver_id if driver_id is not None else delivery.delivery_person_id
        if not target:
            return
        StaffService.sync_active_delivery_counters([target])

    @staticmethod
    def _build_filtered_query(
        *,
        search: str,
        status: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
    ):
        customer_user = aliased(User)
        driver_user = aliased(User)

        query = Delivery.query.join(Order, Delivery.order_id == Order.id)
        query = query.join(customer_user, Order.user_id == customer_user.id)
        query = query.outerjoin(UserAddress, Order.delivery_address_id == UserAddress.id)
        query = query.outerjoin(driver_user, Delivery.delivery_person_id == driver_user.id)

        if status:
            query = query.filter(Delivery.status == AdminDeliveryService._normalize_status(status))

        start_dt = AdminDeliveryService._parse_date_boundary(start_date, end_of_day=False)
        if start_dt:
            query = query.filter(Delivery.scheduled_date >= start_dt)

        end_dt = AdminDeliveryService._parse_date_boundary(end_date, end_of_day=True)
        if end_dt:
            query = query.filter(Delivery.scheduled_date <= end_dt)

        normalized_search = (search or "").strip()
        if normalized_search:
            term = f"%{normalized_search}%"
            query = query.filter(
                or_(
                    Delivery.tracking_number.ilike(term),
                    Order.order_number.ilike(term),
                    customer_user.first_name.ilike(term),
                    customer_user.last_name.ilike(term),
                    customer_user.phone.ilike(term),
                    driver_user.first_name.ilike(term),
                    driver_user.last_name.ilike(term),
                    driver_user.phone.ilike(term),
                    UserAddress.full_address.ilike(term),
                )
            )

        return query

    @staticmethod
    def _build_summary(query) -> Dict[str, Any]:
        total = query.count()
        grouped = query.with_entities(Delivery.status, func.count(Delivery.id)).group_by(Delivery.status).all()
        counts = {AdminDeliveryService._status_value(status): count for status, count in grouped}

        scheduled = counts.get(DeliveryStatus.SCHEDULED.value, 0)
        pending = counts.get(DeliveryStatus.PENDING.value, 0)
        active = sum(counts.get(status.value, 0) for status in ACTIVE_DELIVERY_STATUSES)
        delivered = counts.get(DeliveryStatus.DELIVERED.value, 0)
        failed = counts.get(DeliveryStatus.FAILED.value, 0)
        cancelled = counts.get(DeliveryStatus.CANCELLED.value, 0)
        returned = counts.get(DeliveryStatus.RETURNED.value, 0)
        terminal_total = delivered + failed + cancelled + returned

        unassigned_count = (
            query.filter(Delivery.delivery_person_id.is_(None))
            .filter(Delivery.status.notin_(list(TERMINAL_DELIVERY_STATUSES)))
            .count()
        )

        return {
            "total_deliveries": total,
            "scheduled_deliveries": scheduled,
            "pending_deliveries": pending,
            "assigned_deliveries": counts.get(DeliveryStatus.ASSIGNED.value, 0),
            "picked_up_deliveries": counts.get(DeliveryStatus.PICKED_UP.value, 0),
            "in_transit_deliveries": counts.get(DeliveryStatus.IN_TRANSIT.value, 0),
            "arrived_deliveries": counts.get(DeliveryStatus.ARRIVED.value, 0),
            "active_deliveries": active,
            "completed_deliveries": delivered,
            "failed_deliveries": failed,
            "cancelled_deliveries": cancelled,
            "returned_deliveries": returned,
            "unassigned_deliveries": unassigned_count,
            "completion_rate": round((delivered / terminal_total) * 100, 2) if terminal_total else 0.0,
            "status_breakdown": counts,
        }

    @staticmethod
    def _build_items_summary(order: Optional[Order]) -> str:
        if not order or not order.order_items:
            return ""

        parts = []
        for item in order.order_items[:3]:
            product_name = item.product.name if item.product else f"Product #{item.product_id}"
            parts.append(f"{product_name} x{item.quantity}")

        suffix = ""
        if len(order.order_items) > 3:
            suffix = f" +{len(order.order_items) - 3} more"
        return ", ".join(parts) + suffix

    @staticmethod
    def _format_delivery_code(delivery_id: int) -> str:
        return f"DLV-{delivery_id:06d}"

    @staticmethod
    def _derive_priority(order: Optional[Order], delivery: Delivery) -> str:
        if order and getattr(order, "is_urgent", False):
            return "high"
        if delivery.delivery_attempts and delivery.delivery_attempts > 0:
            return "medium"
        if delivery.scheduled_date:
            now = datetime.now(UTC)
            if delivery.scheduled_date <= now + timedelta(hours=2):
                return "medium"
        return "low"

    @staticmethod
    def _normalize_status(raw_status: str) -> DeliveryStatus:
        normalized = (raw_status or "").strip().lower()
        status = AdminDeliveryService.STATUS_ALIASES.get(normalized)
        if not status:
            valid_statuses = ", ".join(sorted(AdminDeliveryService.STATUS_ALIASES))
            raise ValidationError(f"Invalid delivery status. Valid values: {valid_statuses}")
        return status

    @staticmethod
    def _status_value(status: Optional[DeliveryStatus]) -> Optional[str]:
        if status is None:
            return None
        return status.value if hasattr(status, "value") else str(status)

    @staticmethod
    def _parse_date_boundary(value: Optional[str], *, end_of_day: bool) -> Optional[datetime]:
        if not value:
            return None

        try:
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                parsed = datetime.fromisoformat(value)
                if end_of_day:
                    parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
                else:
                    parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError as exc:
            field = "end_date" if end_of_day else "start_date"
            raise ValidationError(f"Invalid {field} format") from exc
