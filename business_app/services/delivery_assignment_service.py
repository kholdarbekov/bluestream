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
from business_app.models.bottle import DriverBottleSessionOrder
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
    def is_assignable(status: DeliveryStatus, *, allow_in_progress: bool) -> bool:
        """Whether a delivery in ``status`` may be given a driver.

        The ONE statement of `assign_driver`'s status guard. `assign_driver`
        refuses with it, and any surface that offers an Assign button asks it
        instead of restating the rule:

        * a held (`rescheduled`) row: never, not even through an explicit admin
          reassign (R22). Rescheduling it to today is the only early release;
        * a failed row: never either (F2). It leaves `failed` only through a
          reschedule to a new date, or by cancelling its order. A reassign kept
          it FAILED under a new driver who had nothing to deliver;
        * a pool row (SCHEDULED/PENDING): always;
        * anything else: only through an explicit admin reassign
          (``allow_in_progress``).
        """
        from business_app.services.staff_service import StaffService

        if status in (DeliveryStatus.RESCHEDULED, DeliveryStatus.FAILED):
            return False
        return allow_in_progress or status in StaffService.CLAIMABLE_DELIVERY_STATUSES

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

        # JWT identities arrive as str (`sub`); the columns compared below are
        # int. Only a digit string is converted — None and anything else keep
        # their old path (no DeliveryPerson → "Driver not found").
        if isinstance(driver_user_id, str) and driver_user_id.isdigit():
            driver_user_id = int(driver_user_id)

        # 1. Lock the delivery row -- and REFRESH it. `with_for_update()` alone
        #    re-selects the row but hands back the instance already in the
        #    session with its pre-lock attributes (measured; see
        #    `OrderScheduleService.ensure_delivery_if_due`). The hand-assign
        #    callers (`RouteEditService.move_stop`, the admin reassign route and
        #    `AdminDeliveryService.reassign_delivery`) have all loaded this
        #    delivery unlocked a moment earlier, and the guards below must judge
        #    the row as it is now. A reschedule committed in between is exactly
        #    the case step 3 exists for.
        delivery = db.session.get(Delivery, delivery_id, with_for_update=True, populate_existing=True)
        if not delivery:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        old_person_id = delivery.delivery_person_id
        old_status = delivery.status

        # 2. Idempotent re-assign to the same driver — no-op.
        if old_person_id == driver_user_id and old_status in StaffService.ACTIVE_DELIVERY_STATUSES:
            return AssignmentResult(delivery=delivery, history_id=None, changed=False)

        # 3. Status guard, stated once in `is_assignable`: pool rows are
        #    claimable; in-progress/terminal ones only through an explicit admin
        #    reassign (allow_in_progress); a held RESCHEDULED row never, not even
        #    then (spec R22). It waits for its new day and is released by its
        #    date, never by hand; an admin who wants it out today reschedules it
        #    to today. A FAILED row never either (F2): it is re-dated, not handed on.
        if not DeliveryAssignmentService.is_assignable(delivery.status, allow_in_progress=allow_in_progress):
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

        # 5. Resolve + lock the driver by user_id (NOT DeliveryPerson.id), together with the driver losing the
        #    delivery: step 11 writes both rows (their workload counters). One statement, in user-id order, the
        #    order the progress guard's crew lock takes them in. Locking the new driver's row here and writing the
        #    old one's at step 11 deadlocked with a co-driver's On the way tap, which holds the lower id and waits
        #    for the higher.
        locked = {
            row.user_id: row
            for row in DeliveryPerson.query.filter(
                DeliveryPerson.user_id.in_([uid for uid in {driver_user_id, old_person_id} if uid is not None])
            )
            .order_by(DeliveryPerson.user_id)
            .with_for_update()
            .all()
        }
        dp = locked.get(driver_user_id)
        if not dp or not dp.is_active:
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

        # 7. Bottle binding: the order follows the driver's session only when that session's FREE FULL bottles
        #    (full on board minus the stops already under way, this delivery excluded) cover the load.
        #
        #    require_session=True  (bot accept, auto-assign -- never an admin path): session missing →
        #    BOTTLE_SESSION_REQUIRED; free full short of the need → BOTTLE_SESSION_CAPACITY_EXCEEDED.
        #
        #    require_session=False (admin assign / reassign / dispatch move / bulk): never refuses on bottles.
        #    It binds when a session exists AND free full covers the need. Otherwise a binding the order
        #    holds to some other session than the new driver's is dropped: the load it was counted against
        #    is no longer the one on this order's truck. The progress-time guard late-binds later.
        #    A finished order (delivered/cancelled/returned) keeps its binding untouched: it is the closed trip's
        #    historical tally, and reassigning the delivery record must not rewrite it.
        if order and not BottleTrackingService.order_binding_is_historical(order):
            bottle_svc = BottleTrackingService()
            bottles_needed = bottle_svc.calculate_bottles_for_order(order)
            if bottles_needed > 0:
                needed = int(bottles_needed)
                session = bottle_svc.get_effective_session(driver_user_id)
                if require_session:
                    if session is None:
                        raise ValidationError(
                            "A bottle session is required to accept this order. "
                            "Please start your own session or join a colleague's.",
                            error_code="BOTTLE_SESSION_REQUIRED",
                        )
                    bottle_svc.assert_delivery_within_session_capacity(
                        session, needed, exclude_delivery_id=delivery.id
                    )  # raises CAPACITY_EXCEEDED
                    covered = True
                else:
                    covered = session is not None and needed <= bottle_svc.free_full_bottles_for_session(
                        session, exclude_delivery_id=delivery.id
                    )
                if covered:
                    bottle_svc.rebind_order_to_session(order.id, session.id, accepted_by_driver_id=driver_user_id)
                else:
                    held = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
                    if held is not None and (session is None or held.session_id != session.id):
                        bottle_svc.unbind_order(order.id)

        # 8. Compute the new status: pool→ASSIGNED; in-progress reassign keeps status.
        new_status = DeliveryStatus.ASSIGNED if old_status in StaffService.CLAIMABLE_DELIVERY_STATUSES else old_status

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
