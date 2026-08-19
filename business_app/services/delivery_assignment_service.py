"""Single source of truth for driver→delivery ownership transitions.

Every path that gives a driver ownership of a delivery — bot self-accept,
auto-assign, admin single/bulk assign, admin reassign — MUST delegate here so
the assignment invariants (lock, claimable guard, idempotency, COD-block,
bottle binding, ARCH-006, status history, counter sync) are enforced once.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from flask import current_app

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.state_validators import assert_delivery_person_for_status
from shared.enums import AssignmentSource, DeliveryStatus, PaymentMethod


@dataclass
class AssignmentResult:
    delivery: Delivery
    history_id: Optional[int]
    changed: bool


class DeliveryAssignmentService:
    """The canonical assign-driver primitive. All paths delegate here."""

    @staticmethod
    def assign_driver(
        delivery_id: int,
        *,
        driver_user_id: int,
        actor_id: int,
        source: AssignmentSource,
        note: Optional[str] = None,
        require_session: bool = False,
        allow_in_progress: bool = False,
    ) -> AssignmentResult:
        from business_app.services.staff_service import StaffService
        from business_app.services.bottle_tracking_service import BottleTrackingService

        # 1. Lock the delivery row.
        delivery = Delivery.query.with_for_update().get(delivery_id)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        old_person_id = delivery.delivery_person_id
        old_status = delivery.status

        # 2. Idempotent re-assign to the same driver — no-op.
        if old_person_id == driver_user_id and old_status in StaffService.ACTIVE_DELIVERY_STATUSES:
            return AssignmentResult(delivery=delivery, history_id=None, changed=False)

        # 3. Claimable-status guard. In-progress/terminal deliveries are only
        #    re-assignable by an explicit admin reassign (allow_in_progress).
        if delivery.status not in StaffService.CLAIMABLE_DELIVERY_STATUSES and not allow_in_progress:
            raise ValidationError(
                f"This delivery can no longer be assigned (status: {delivery.status.value})",
                error_code="STAFF_DELIVERY_NOT_CLAIMABLE",
            )

        # 4. Already owned by a different driver and not an explicit reassign → taken.
        if old_person_id is not None and old_person_id != driver_user_id and not allow_in_progress:
            raise ValidationError(
                "This delivery has already been accepted by another driver",
                error_code="STAFF_DELIVERY_ALREADY_TAKEN",
            )

        # 5. Resolve + lock the driver by user_id (NOT DeliveryPerson.id).
        dp = DeliveryPerson.query.filter_by(user_id=driver_user_id, is_active=True).with_for_update().first()
        if not dp:
            raise NotFoundError("Driver not found", error_code="STAFF_DRIVER_NOT_FOUND")

        # 6. COD-block check for CASH orders.
        order = delivery.order
        order_payment_method = order.payment_method if order else None
        is_cash = order_payment_method == PaymentMethod.CASH or getattr(order_payment_method, "value", None) == "cash"
        if is_cash:
            from business_app.services.driver_reconciliation_service import DriverReconciliationService

            if DriverReconciliationService().is_driver_blocked_from_cod(driver_user_id):
                raise ValidationError(
                    "Driver is blocked from new cash on delivery assignments until reconciliation issues are resolved",
                    error_code="STAFF_DRIVER_COD_BLOCKED",
                )

        # 7. Bottle binding: (re)bind the order onto the driver's open session.
        #    Strictness depends on require_session (see below); anything not bound
        #    here defers to the progress-time guard (assert_driver_can_progress_delivery).
        #
        #    require_session=True  (bot self-accept): session missing → raise
        #    BOTTLE_SESSION_REQUIRED; session present but cannot cover the load →
        #    raise BOTTLE_SESSION_CAPACITY_EXCEEDED. [strict]
        #
        #    require_session=False (auto/admin/bulk/reassign): bind ONLY when a
        #    session exists AND it can cover the load; otherwise skip silently —
        #    the progress-time guard enforces capacity/binding when the driver
        #    actually progresses. [best-effort — restores old assign_delivery_driver]
        if order:
            bottle_svc = BottleTrackingService()
            bottles_needed = bottle_svc.calculate_bottles_for_order(order)
            if bottles_needed > 0:
                session = bottle_svc.get_effective_session(driver_user_id)
                if session is not None and session.current_inventory >= int(bottles_needed):
                    bottle_svc.rebind_order_to_session(order.id, session.id, accepted_by_driver_id=driver_user_id)
                elif require_session:
                    if session is None:
                        raise ValidationError(
                            "A bottle session is required to accept this order. "
                            "Please start your own session or join a colleague's.",
                            error_code="BOTTLE_SESSION_REQUIRED",
                        )
                    bottle_svc.assert_delivery_within_session_capacity(
                        session, int(bottles_needed)
                    )  # raises CAPACITY_EXCEEDED
                # else require_session=False and (no session or insufficient capacity): best-effort skip

        # 8. Compute the new status: pool→ASSIGNED; in-progress reassign keeps status.
        new_status = (
            DeliveryStatus.ASSIGNED if old_status in (DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING) else old_status
        )

        # 9. ARCH-006: person must be set before/at the assigned status.
        assert_delivery_person_for_status(delivery, new_status, delivery_person_id=driver_user_id)

        now = datetime.now(timezone.utc)
        delivery.delivery_person_id = driver_user_id
        delivery.status = new_status
        delivery.updated_at = now
        # Record the assignment time on route_data too (merge, don't clobber).
        delivery.route_data = {**(delivery.route_data or {}), "assigned_at": now.isoformat()}

        # 10. Status-history row.
        history = DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=old_status,
            new_status=new_status,
            changed_by=actor_id,
            changed_at=now,
            notes=note or f"Assigned via {source.value}",
        )
        db.session.add(history)
        db.session.flush()
        history_id = history.id

        # 11. Counter sync for affected drivers (old + new).
        affected = [driver_user_id]
        if old_person_id and old_person_id != driver_user_id:
            affected.append(old_person_id)
        StaffService.sync_active_delivery_counters(affected)

        # 11b. Route bookkeeping for the same two drivers.
        #
        # `DeliveryRoute.optimized_order` is the other place a delivery's owner
        # is written down. It has no foreign key and no trigger, so a delivery
        # that changes hands here used to stay listed on the losing driver's
        # planned sequence indefinitely — the same order rendered under two
        # drivers on the dispatch board, both polylines drawn through it, and
        # the losing driver's route became unsaveable (the save guard checks
        # real ownership, so its `expected_delivery_ids` could never match).
        #
        # Done HERE, beside the counter sync and inside the same transaction,
        # for the same reason the counter sync is: this is the one function
        # every assign path goes through. Doing it in the callers is what
        # produced the divergence — only one of them ever did.
        from business_app.services.route_optimization_service import RouteOptimizationService

        RouteOptimizationService.drop_from_route(old_person_id, delivery.id)
        RouteOptimizationService.splice_into_route(driver_user_id, delivery.id)

        # 12. Commit.
        db.session.commit()

        current_app.logger.info(
            "[ASSIGN] delivery=%s driver=%s source=%s old_driver=%s old_status=%s new_status=%s",
            delivery.id,
            driver_user_id,
            source.value,
            old_person_id,
            old_status.value if old_status else None,
            new_status.value if new_status else None,
        )
        return AssignmentResult(delivery=delivery, history_id=history_id, changed=True)
