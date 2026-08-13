"""Write model for admin route editing.

Owns sequencing and override bookkeeping ONLY. Every side effect with business
rules attached — giving a driver ownership of a delivery, taking it away —
delegates to the existing single sources of truth
(`DeliveryAssignmentService.assign_driver`, `StaffService.return_delivery_to_pool`)
so their invariants are enforced once, here as everywhere else.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from business_app import db
from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.services.delivery_assignment_service import DeliveryAssignmentService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.services.staff_service import StaffService
from business_app.tasks.staff_tasks import notify_staff_order_reassigned, notify_staff_order_unassigned
from business_app.utils.bot_webhook import notify_route_updated
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.staff_order_info import build_staff_order_info
from shared.enums import AssignmentSource

logger = logging.getLogger(__name__)


class RouteStaleError(ValidationError):
    """The route moved under the admin while they were editing it.

    Carries the live delivery-id set so the caller can hand the UI something
    to reconcile against instead of a bare error.
    """

    def __init__(self, message: str, current_delivery_ids: List[int]):
        super().__init__(message, error_code="DISPATCH_ROUTE_STALE")
        self.current_delivery_ids = current_delivery_ids


class RouteEditService:
    """Admin-driven edits to a driver's planned stop sequence."""

    # ----- sequence ---------------------------------------------------------

    @classmethod
    def set_stop_order(
        cls,
        *,
        driver_id: int,
        ordered_delivery_ids: List[int],
        pinned: Optional[Dict[str, Any]],
        actor_id: int,
        expected_delivery_ids: List[int],
    ) -> DeliveryRoute:
        """Persist an admin-authored stop sequence and lock the route.

        `expected_delivery_ids` is what the admin's screen was showing. If the
        driver's live active set has moved on since, this raises rather than
        writing a sequence that could resurrect a completed stop.
        """
        service = RouteOptimizationService()
        live_ids = [d.id for d in service.active_deliveries(driver_id)]
        live_set = set(live_ids)

        if set(expected_delivery_ids or []) != live_set:
            raise RouteStaleError("This route changed while you were editing it", current_delivery_ids=live_ids)
        if set(ordered_delivery_ids) != live_set:
            raise RouteStaleError(
                "The submitted sequence does not cover the driver's active deliveries",
                current_delivery_ids=live_ids,
            )
        if len(set(ordered_delivery_ids)) != len(ordered_delivery_ids):
            raise ValidationError(
                "The submitted sequence contains duplicate stops",
                error_code="DISPATCH_DUPLICATE_STOP",
            )

        route = service.current_route(driver_id)
        if route is None:
            raise NotFoundError("This driver has no route for today", error_code="DISPATCH_ROUTE_NOT_FOUND")

        changed = list(route.optimized_order or []) != list(ordered_delivery_ids)

        route.optimized_order = list(ordered_delivery_ids)
        route.pinned_stops = service.clamp_pins(pinned, ordered_delivery_ids)
        route.manual_override = True
        route.overridden_by = actor_id
        route.overridden_at = datetime.now(timezone.utc)
        cls._refresh_metrics(route, ordered_delivery_ids)
        db.session.commit()

        logger.info(
            "dispatch_route_reordered driver=%s route=%s actor=%s n=%d pins=%d changed=%s",
            driver_id,
            route.id,
            actor_id,
            len(ordered_delivery_ids),
            len(route.pinned_stops or {}),
            changed,
        )

        # A save that changed nothing must not ping the driver. Dispatch saves
        # while planning; a no-op ping trains drivers to ignore the real ones.
        if changed:
            cls._notify_route_updated(driver_id)
        return route

    @classmethod
    def reoptimize(cls, *, driver_id: int, actor_id: int) -> Optional[DeliveryRoute]:
        """Drop the manual lock and re-solve from scratch ("Reset to optimal")."""
        route = RouteOptimizationService().optimize_for_driver(
            driver_id, trigger="admin_dispatch_reset", respect_override=False
        )
        logger.info(
            "dispatch_route_reset driver=%s actor=%s route=%s",
            driver_id,
            actor_id,
            route.id if route else None,
        )
        # `optimize_for_driver` computed and persisted the real materiality
        # verdict on `route.extra_data['materiality']` — pass it through
        # instead of letting `_notify_route_updated` fabricate one. `route`
        # is None only when the re-solve found nothing to optimize (no
        # active deliveries / no driver location); there is genuinely no
        # verdict to report in that case.
        materiality = (route.extra_data or {}).get("materiality") if route is not None else None
        cls._notify_route_updated(driver_id, materiality=materiality)
        return route

    # ----- internals --------------------------------------------------------

    @classmethod
    def _refresh_metrics(cls, route: DeliveryRoute, ordered_delivery_ids: List[int]) -> None:
        """Re-measure the hand-made sequence.

        Without this the panel shows the OLD optimal route's km/min next to a
        different order — a number that is not merely stale but describes a
        route nobody is driving.

        Best-effort by design: if the matrix provider is down we keep the
        previous figures and flag them, rather than failing a save the admin
        has already committed to. The UI reads `extra_data.metrics_stale`.
        """
        service = RouteOptimizationService()
        try:
            by_id = {d.id: d for d in service.active_deliveries(route.delivery_person_id)}
            points = []
            for delivery_id in ordered_delivery_ids:
                point = service.delivery_point(by_id[delivery_id]) if delivery_id in by_id else None
                if point is None:
                    raise ValueError(f"delivery {delivery_id} has no usable coordinates")
                points.append(point)

            start = (route.start_location_lat, route.start_location_lng)
            matrix, _source = service.maps.get_distance_matrix([start] + points, traffic=True)
            km, minutes = service._sum_route_metrics(matrix, list(range(len(points) + 1)))
            route.total_distance_km = km
            route.estimated_duration_minutes = int(round(minutes))
            route.extra_data = {**(route.extra_data or {}), "metrics_stale": False}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dispatch metrics refresh failed driver=%s route=%s: %s",
                route.delivery_person_id,
                route.id,
                exc,
            )
            route.extra_data = {**(route.extra_data or {}), "metrics_stale": True}

    @staticmethod
    def _notify_route_updated(driver_id: int, materiality: Optional[Dict[str, Any]] = None) -> None:
        """Best-effort: a webhook failure must not roll back a persisted edit.

        `materiality` defaults to None (no verdict): callers that never
        re-solved the route (a hand-authored sequence save, a stop move, a
        pool return) have no materiality to report, and `notify_route_updated`
        omits those keys entirely rather than fabricating False for them.
        """
        try:
            if materiality is not None:
                notify_route_updated(driver_id, materiality=materiality)
            else:
                notify_route_updated(driver_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("route-updated push failed driver=%s: %s", driver_id, exc)

    # ----- assignment -------------------------------------------------------

    @classmethod
    def move_stop(
        cls,
        *,
        delivery_id: int,
        to_driver_id: int,
        actor_id: int,
        position: Optional[int] = None,
    ) -> Delivery:
        """Hand a stop to another driver.

        Delegates ownership to `DeliveryAssignmentService.assign_driver`, which
        may legitimately REFUSE (COD-blocked driver, non-claimable status). That
        exception propagates untouched: the caller shows the real reason and the
        route is left exactly as it was.
        """
        delivery = Delivery.query.get(delivery_id)
        if delivery is None:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        from_driver_id = delivery.delivery_person_id
        if from_driver_id == to_driver_id:
            raise ValidationError("This stop is already on that driver's route", error_code="DISPATCH_SAME_DRIVER")

        old_telegram_id = cls._telegram_id_for(from_driver_id)

        DeliveryAssignmentService.assign_driver(
            delivery_id,
            driver_user_id=to_driver_id,
            actor_id=actor_id,
            source=AssignmentSource.ADMIN_DISPATCH,
            note=f"Moved via dispatch map by admin {actor_id}",
            allow_in_progress=True,
        )

        if from_driver_id is not None:
            cls._remove_from_route(from_driver_id, delivery_id)
        cls._insert_into_route(to_driver_id, delivery_id, position)

        logger.info(
            "dispatch_stop_moved delivery=%s from=%s to=%s actor=%s position=%s",
            delivery_id,
            from_driver_id,
            to_driver_id,
            actor_id,
            position,
        )

        if from_driver_id is not None:
            cls._notify_route_updated(from_driver_id)
        cls._notify_route_updated(to_driver_id)

        new_telegram_id = cls._telegram_id_for(to_driver_id)
        cls._enqueue(
            notify_staff_order_reassigned,
            old_telegram_id,
            new_telegram_id,
            build_staff_order_info(delivery),
        )
        return delivery

    @classmethod
    def return_stop_to_pool(
        cls,
        *,
        delivery_id: int,
        actor_id: int,
        reason: Optional[str] = None,
    ) -> Delivery:
        """Take a stop off a driver and put it back in the unassigned pool."""
        delivery = Delivery.query.get(delivery_id)
        if delivery is None:
            raise NotFoundError("Delivery not found", error_code="STAFF_DELIVERY_NOT_FOUND")

        from_driver_id = delivery.delivery_person_id
        old_telegram_id = cls._telegram_id_for(from_driver_id)
        order_info = build_staff_order_info(delivery)

        delivery = StaffService.return_delivery_to_pool(
            delivery_id,
            actor_id,
            reason=reason,
            notes="Returned to pool from the dispatch map",
        )

        if from_driver_id is not None:
            cls._remove_from_route(from_driver_id, delivery_id)
            cls._notify_route_updated(from_driver_id)
            cls._enqueue(notify_staff_order_unassigned, old_telegram_id, order_info)

        logger.info(
            "dispatch_stop_pooled delivery=%s from=%s actor=%s reason=%s",
            delivery_id,
            from_driver_id,
            actor_id,
            reason,
        )
        return delivery

    # ----- route bookkeeping ------------------------------------------------

    @classmethod
    def _remove_from_route(cls, driver_id: int, delivery_id: int) -> None:
        route = RouteOptimizationService().current_route(driver_id)
        if route is None:
            return
        surviving = [did for did in (route.optimized_order or []) if did != delivery_id]
        route.optimized_order = surviving
        route.pinned_stops = RouteOptimizationService.clamp_pins(route.pinned_stops, surviving)
        cls._mark_metrics_stale(route)
        db.session.commit()

    @classmethod
    def _insert_into_route(cls, driver_id: int, delivery_id: int, position: Optional[int]) -> None:
        """Splice a stop into the target route.

        No route row yet means the driver has never been optimised today; the
        next optimisation run will build one from their active set, so there is
        nothing to splice into and nothing to fix.
        """
        route = RouteOptimizationService().current_route(driver_id)
        if route is None:
            return
        sequence = [did for did in (route.optimized_order or []) if did != delivery_id]
        idx = len(sequence) if position is None else max(0, min(int(position), len(sequence)))
        sequence.insert(idx, delivery_id)
        route.optimized_order = sequence
        route.pinned_stops = RouteOptimizationService.clamp_pins(route.pinned_stops, sequence)
        cls._mark_metrics_stale(route)
        db.session.commit()

    @staticmethod
    def _mark_metrics_stale(route: DeliveryRoute) -> None:
        """A stop moving on or off a route invalidates its distance/duration
        figures without a full re-solve. Flag them the same way
        `_refresh_metrics` does for a hand-authored sequence — but without an
        external matrix call: a move/return-to-pool must not depend on the
        matrix provider being up. The UI reads `extra_data.metrics_stale`.
        """
        route.extra_data = {**(route.extra_data or {}), "metrics_stale": True}

    @staticmethod
    def _telegram_id_for(driver_id: Optional[int]) -> Optional[str]:
        if driver_id is None:
            return None
        from business_app.models.user import User

        user = User.query.get(driver_id)
        return getattr(user, "telegram_id", None)

    @staticmethod
    def _enqueue(task, *args) -> None:
        """Best-effort notification dispatch.

        A broker hiccup must not roll back an assignment that already committed
        through the SSOT — the delivery has genuinely moved, and raising here
        would leave the admin thinking it had not.
        """
        try:
            task.delay(*args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("staff notification enqueue failed task=%s: %s", getattr(task, "name", task), exc)
