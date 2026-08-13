"""
Route optimization service.

Computes the optimal stop sequence for a driver's active deliveries using a
Yandex Distance Matrix (with traffic) + nearest-neighbor + 2-opt TSP solver.
The result is persisted to `DeliveryRoute.optimized_order` so the staff bot's
"My active deliveries" can render the list in the right order with a "Next
stop" badge on top.

Also exposes `compute_diversion_gain(driver_id, new_delivery_id)` to evaluate
whether a freshly-pooled order is enough of a win to offer the driver a "go
here first" diversion ahead of their already-committed stop (spec §7). Used
by the pool-arrival webhook flow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import func

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.services.maps_service import MapsService
from business_app.utils import google_routes
from business_app.utils.exceptions import ExternalServiceError
from shared.constants import TASHKENT_COORDINATES
from shared.enums import DeliveryStatus

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

ACTIVE_DELIVERY_STATUSES = (
    DeliveryStatus.ASSIGNED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT,
    DeliveryStatus.ARRIVED,
)

# Statuses that mean "the driver has started driving to / is at this stop".
COMMITTED_STATUSES = (DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED)

# A stop the driver has not begun: these are the candidates for "the next
# stop" the driver cares about. IN_TRANSIT/ARRIVED stops are already started.
UNSTARTED_STATUSES = (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP)

# Triggers that mean "the driver themselves caused this re-optimization".
# Spec 2026-08-11 §5.1 set plus the two Task-9 status-trigger labels.
# "arrival" is kept defensively even though Task 2 removed its emitters.
DRIVER_INITIATED_TRIGGERS = frozenset(
    {"arrival", "delivery", "location_update", "manual", "accept", "picked_up", "in_transit"}
)

# Triggers that mean a human explicitly asked for a fresh optimal sequence
# right now — "Optimize routes" (driver) / "Reset to optimal" (dispatch).
# Spec §4.4 (amended): these bypass the hysteresis gate entirely, even for an
# unchanged set. Deliberately narrower than DRIVER_INITIATED_TRIGGERS: the two
# constants answer different questions (should we notify? vs. did a human ask
# for THIS re-solve?) and must not be merged or derived from one another.
# "location_update"/"accept"/"delivery" are driver-CAUSED but are not requests
# to re-optimize — they stay gated.
EXPLICIT_REQUEST_TRIGGERS = ("manual", "admin_dispatch_reset")

LOCATION_FRESH_DEFAULT_SECONDS = 1800  # 30 min

# Uncertainty radius, in metres, beyond which a reported fix is refused outright
# rather than stored. Re-sorting a route around a point that could be half a
# kilometre away yields a worse sequence than keeping the older-but-precise fix
# we already hold, so the write is rejected and the previous position survives.
# Override per-environment with DRIVER_LOCATION_MAX_ACCURACY_METERS.
LOCATION_MAX_ACCURACY_DEFAULT_METERS = 500

# At/below this many *deliveries* (matrix size N+1 including start), `_solve_tsp`
# uses Held-Karp DP for a provably optimal sequence under the supplied matrix.
# 12 deliveries -> n=13, 2^12*12 ~= 49K states, sub-second on commodity hardware.
# Above this we fall back to the nearest-neighbor + 2-opt heuristic.
HELDKARP_MAX_DELIVERIES = 12


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat naive timestamps as UTC. PostgreSQL preserves tz on DateTime(timezone=True),
    SQLite (used in tests) does not — without this normalization a stored
    `datetime.now(UTC)` round-trips as naive and breaks the >= comparisons below."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class RouteOptimizationService:
    """Computes and persists optimal driver routes."""

    def __init__(self):
        self.maps = MapsService()
        # Set only on the debounced-skip path of `optimize_for_driver`, and
        # reset at the top of every call. Lets a caller (the Celery task)
        # report an accurate reason for a `None` result without `optimize_
        # for_driver` widening its `Optional[DeliveryRoute]` return contract
        # — every existing caller/test that pattern-matches on `is None` /
        # `is not None` stays untouched.
        self.last_skip_reason: Optional[str] = None

    # ----- public API -------------------------------------------------------

    def optimize_for_driver(
        self,
        driver_id: int,
        *,
        traffic: bool = True,
        trigger: str = "auto",
        respect_override: bool = True,
    ) -> Optional[DeliveryRoute]:
        """Compute and persist the optimal sequence for `driver_id`.

        Returns the upserted DeliveryRoute, or None when no optimization can
        be performed:
          - driver has no active deliveries; or
          - none of the delivery addresses can be geocoded; or
          - we have no driver location to optimize from (location_status =
            "missing"). Driver-current-location is a hard precondition for
            route optimization — without it any sequence we produce is just
            a guess. Caller is expected to prompt the driver to share
            location and try again.

        When the route carries a dispatch override (`manual_override`) and
        `respect_override` is True, the admin's sequence wins: see
        `_apply_override_policy`. Pass `respect_override=False` for the
        explicit "Reset to optimal" action, which clears the override.
        """
        self.last_skip_reason = None
        deliveries = self._load_active_deliveries(driver_id)
        if not deliveries:
            logger.info("route_optimize: driver=%s has no active deliveries", driver_id)
            # A spent override — every stop it locked has completed — must not
            # outlive its delivery set and silently bind a future, unrelated one.
            existing_route = self.current_route(driver_id)
            if existing_route is not None and existing_route.manual_override:
                existing_route.optimized_order = []
                existing_route.manual_override = False
                existing_route.pinned_stops = {}
                existing_route.overridden_by = None
                existing_route.overridden_at = None
                db.session.commit()
            return None

        # Hard precondition: we need a real driver location. We accept both
        # "fresh" and "stale" — once the driver has shared location at all,
        # it's a sensible start point even if older than the freshness
        # threshold. Only "missing" (never shared) blocks optimization.
        loc_status = self._location_status(driver_id)
        if loc_status == "missing":
            logger.info(
                "route_optimize: driver=%s has no shared location (status=missing) — skipping",
                driver_id,
            )
            return None

        existing_route = self.current_route(driver_id)
        prev_order_raw: List[int] = [
            int(x) for x in ((existing_route.optimized_order or []) if existing_route is not None else [])
        ]

        pinned_by_delivery: Dict[str, int] = {}
        keep_override = False

        if existing_route is not None and existing_route.manual_override:
            if respect_override:
                settled = self._apply_override_policy(existing_route, deliveries)
                if settled is not None:
                    self._stamp_materiality(
                        settled,
                        prev_order=prev_order_raw,
                        deliveries=deliveries,
                        trigger=trigger,
                    )
                    return settled
                # Falls through: the set GREW, so we re-solve the unpinned
                # stops and splice the admin's pins back into their slots.
                #
                # Pin safety invariant (route-UX plan 2026-08-11 §4.4, verified
                # 2026-08-11): `keep_override=True` is reachable ONLY here, and
                # only here, and only because the active set strictly grew
                # (`_apply_override_policy` returns None precisely when `added`
                # is non-empty). The hysteresis gate below activates ONLY when
                # the set is UNCHANGED (`set(prev_order_raw) ==
                # set(optimized_delivery_ids)`). Those two conditions are
                # mutually exclusive by construction, so the gate can never
                # fire while `keep_override` is True: an admin's pinned/
                # overridden route can never be silently re-sequenced OR have
                # its pin state clamped/dropped by the hysteresis suppression
                # path. Do not weaken either guard without re-deriving this.
                pinned_by_delivery = dict(existing_route.pinned_stops or {})
                keep_override = True
            else:
                existing_route.manual_override = False
                existing_route.pinned_stops = {}
                existing_route.overridden_by = None
                existing_route.overridden_at = None
                db.session.commit()

        self._geocode_missing_addresses(deliveries)
        deliveries = [d for d in deliveries if self._delivery_point(d) is not None]
        if not deliveries:
            logger.warning("route_optimize: driver=%s no deliveries with usable coords", driver_id)
            return None

        # Debounce (spec §4.5) applies ONLY to a stationary-ping re-solve: the
        # active delivery set must be UNCHANGED from what's already published,
        # matching Task 6's own hysteresis rule ("a set change always
        # publishes") applied one gate earlier. Without this equality check, a
        # driver who accepts a new stop and then shares location from the same
        # spot the very next moment (a live-in-place `delivery` solve already
        # having stamped `last_optimized_at`/`last_driver_location` seconds
        # earlier) would have the new stop silently withheld from
        # `optimized_order` until the debounce window/move-threshold clears —
        # `location_update` is the ONLY trigger the pool-accept flow relies on
        # to re-run optimization (staff_bot/handlers/delivery/orders_pool.py),
        # so it must never sit on a set it hasn't actually solved yet.
        #
        # MUST run here, after the geocode filter above (not right after
        # `prev_order_raw` is computed): `prev_order_raw` is the persisted
        # `optimized_order`, which only ever contains deliveries that
        # survived that filter. A delivery whose address can never be
        # geocoded (missing `delivery_address`, or `_geocode_missing_
        # addresses` perpetually failing on it) stays in the RAW active set
        # forever while never landing in `optimized_order` — comparing
        # against the raw set would make the two sets never match, silently
        # defeating the debounce (full re-solve, plus the anchored driver->
        # committed leg's second matrix call below) for that driver's entire
        # shift. Reaching `_apply_override_policy` above first is harmless:
        # that path makes no matrix call, so nothing the debounce guards
        # against has happened yet.
        if (
            trigger == "location_update"
            and {d.id for d in deliveries} == set(prev_order_raw)
            and self._should_debounce_location_trigger(driver_id)
        ):
            logger.info("route_optimize_debounced driver=%s trigger=location_update", driver_id)
            self.last_skip_reason = "location_debounced"
            return None

        # --- Anchor rule (route-UX plan 2026-08-11, spec §4.2, amended) -----
        # With a committed stop, the TAIL is solved FROM the committed stop's
        # fixed coordinates and the committed stop is pinned at position 0 —
        # this is what stops a newly-arrived stop from jumping ahead of the
        # one the driver is already driving to, and what makes the tail
        # matrix repeat (and hit the Redis cache) across solves instead of
        # missing every time because point 0 used to be the moving driver.
        #
        # The PERSISTED route still describes what's left to drive from where
        # the driver actually is: `start_location_*` stays the driver's own
        # position (never the committed stop's), and the persisted totals are
        # the tail metrics PLUS the driver -> committed-stop leg, priced with
        # its own small 2-point matrix call below (mirrors
        # `annotate_active_items`'s next-leg ETA) rather than folded into the
        # tail matrix — folding it in would put the moving driver point back
        # into the tail's cache key and defeat the anchor's whole point.
        committed, committed_point, is_anchored = self._resolve_anchor(driver_id, deliveries)
        driver_point, driver_source = self._resolve_start_point(driver_id, deliveries)
        if is_anchored:
            start_point, start_source = committed_point, "committed_stop"
        else:
            committed = None
            start_point, start_source = driver_point, driver_source

        delivery_points = [self._delivery_point(d) for d in deliveries]
        all_points = [start_point] + delivery_points

        matrix, source = self.maps.get_distance_matrix(all_points, traffic=traffic)

        # Solve-local pins: the admin's override pins (when the set grew)
        # plus the committed stop at slot 0. The committed pin is DERIVED
        # each solve and is never persisted into route.pinned_stops.
        solving_pins: Dict[str, int] = dict(pinned_by_delivery)
        if committed is not None:
            cid = str(committed.id)
            admin_slots = {int(v) for v in solving_pins.values()}
            if cid not in solving_pins and 0 not in admin_slots:
                solving_pins[cid] = 0

        # Translate delivery-id pins into matrix indices (index 0 is the start).
        pinned_positions = {
            idx + 1: int(solving_pins[str(d.id)]) for idx, d in enumerate(deliveries) if str(d.id) in solving_pins
        }
        if pinned_positions:
            order = self._solve_with_pins(matrix, pinned_positions, start_idx=0)
        else:
            order = self._solve_tsp(matrix, start_idx=0)

        # `order` includes index 0 (start). Strip it; the rest are delivery indices in [1..N].
        route_indices = [i - 1 for i in order if i != 0]
        optimized_delivery_ids = [deliveries[i].id for i in route_indices]

        total_km, total_min = self._sum_route_metrics(matrix, order)

        # --- Hysteresis (spec §4.4): same set -> publish only material gains.
        # Both orders are costed on the SAME `matrix` snapshot (fetched once,
        # above); costing the previous order on an older, persisted snapshot
        # would let provider jitter leak into the comparison (the §10
        # two-snapshot bug, fixed in the insertion path by this plan's
        # Task 12). This runs BEFORE the driver->committed leg is priced and
        # added below, so that leg (a fixed cost independent of tail order)
        # never appears on one side of the comparison and not the other.
        #
        # A human who explicitly asked for a fresh optimum right now — "Reset
        # to optimal" (admin_dispatch_reset) / "Optimize routes" (manual) —
        # bypasses the gate outright. Without this, RouteEditService.reoptimize
        # clears the manual-override lock but can silently re-publish the very
        # sequence the admin just asked to discard, because it satisfies the
        # gate's own math: the lock clears, the button appears to do nothing.
        if (
            trigger not in EXPLICIT_REQUEST_TRIGGERS
            and prev_order_raw
            and set(prev_order_raw) == set(optimized_delivery_ids)
            and prev_order_raw != optimized_delivery_ids
        ):
            idx_of = {d.id: i + 1 for i, d in enumerate(deliveries)}
            prev_respects_pins = all(
                int(slot) < len(prev_order_raw) and prev_order_raw[int(slot)] == int(did)
                for did, slot in solving_pins.items()
            )
            if prev_respects_pins:
                prev_path = [0] + [idx_of[did] for did in prev_order_raw]
                prev_min = self._sum_path_minutes(matrix, prev_path)
                # Both sides of the comparison must stay travel-only (see
                # `_sum_route_metrics`'s docstring): `total_min` here is
                # service-inclusive, so re-derive the travel-only figure for
                # `order` rather than reusing it, or the flat per-stop
                # service constant silently retunes this threshold.
                gain = prev_min - self._sum_path_minutes(matrix, order)
                min_gain = float(current_app.config.get("ROUTE_RESEQUENCE_MIN_GAIN_MINUTES", 4.0))
                min_ratio = float(current_app.config.get("ROUTE_RESEQUENCE_MIN_GAIN_RATIO", 0.08))
                if gain < min_gain or (prev_min > 0 and (gain / prev_min) < min_ratio):
                    logger.info(
                        "route_resequence_suppressed driver=%s gain_min=%.2f prev_min=%.1f",
                        driver_id,
                        gain,
                        prev_min,
                    )
                    optimized_delivery_ids = list(prev_order_raw)
                    total_km, total_min = self._sum_route_metrics(matrix, prev_path)

        if committed is not None:
            # The tail matrix above never contains the driver's live point
            # (that's the anchor's whole point — see the comment above), so
            # the persisted total is missing the leg the driver is actually
            # driving right now. Price it with one small, separate 2-point
            # call — same pattern `annotate_active_items` uses for the
            # next-leg ETA — and degrade to tail-only metrics if it fails
            # rather than raise: a missing leg cost is better than a broken
            # route.
            try:
                leg_matrix, _leg_source = self.maps.get_distance_matrix(
                    [driver_point, committed_point], traffic=traffic
                )
                leg = leg_matrix[(0, 1)]
                total_km += leg.get("distance_km") or 0.0
                total_min += leg.get("duration_minutes") or 0.0
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "route_optimize: driver->committed leg distance failed driver=%s: %s",
                    driver_id,
                    exc,
                )

        materiality = self.compute_materiality(
            prev_order=prev_order_raw,
            new_order=optimized_delivery_ids,
            deliveries=deliveries,
            trigger=trigger,
        )

        person_row = DeliveryPerson.query.filter_by(user_id=driver_id).first()
        last_driver_location = (
            [person_row.current_location_lat, person_row.current_location_lng]
            if person_row
            and person_row.current_location_lat is not None
            and person_row.current_location_lng is not None
            else None
        )

        route = self._upsert_route(
            driver_id=driver_id,
            start_point=driver_point,
            optimized_delivery_ids=optimized_delivery_ids,
            total_km=total_km,
            total_min=total_min,
            keep_override=keep_override,
            extra={
                "matrix_source": source,
                "start_source": start_source,
                "trigger": trigger,
                "traffic": traffic,
                "fallback": source == "haversine",
                "committed_delivery_id": committed.id if committed is not None else None,
                "materiality": materiality,
                "last_driver_location": last_driver_location,
            },
        )

        logger.info(
            "route_optimized driver=%s n=%d total_km=%.2f total_min=%.1f matrix=%s start=%s trigger=%s pins=%d",
            driver_id,
            len(optimized_delivery_ids),
            total_km,
            total_min,
            source,
            start_source,
            trigger,
            len(pinned_positions),
        )
        return route

    def _apply_override_policy(
        self,
        route: DeliveryRoute,
        deliveries: List[Delivery],
    ) -> Optional[DeliveryRoute]:
        """Decide what an overridden route does about the current delivery set.

        Returns the settled route when nothing further is needed, or None when
        the caller must continue into a real re-solve (the set GREW).

        - Set unchanged  -> skip; the admin's sequence stands.
        - Set only shrank -> drop the departed stops, keep the remaining order
          verbatim, clamp the pins. No solver and no distance-matrix call: the
          admin ordered these stops deliberately and re-solving would discard
          that for a stop that simply completed.

        Neither branch re-solves, so neither branch can recompute
        `extra_data["start_source"]` — but `committed_delivery_id` IS refreshed
        on both (`_sync_committed_delivery_id`) even though nothing else about
        the route changes. Without that, a route anchored on a stop that then
        completes (falls into the "shrank" branch, since it leaves the active
        set) would keep naming that now-delivered stop as "committed" — stale
        and, on "shrank" specifically, actively wrong for whatever Task
        5/Plan 3's card and Task 6's hysteresis do with the field.
        """
        active_ids = {d.id for d in deliveries}
        existing = [int(did) for did in (route.optimized_order or [])]
        existing_set = set(existing)

        if existing_set == active_ids:
            logger.info(
                "route_optimize_skipped_manual_override driver=%s route=%s n=%d",
                route.delivery_person_id,
                route.id,
                len(existing),
            )
            self._sync_committed_delivery_id(route)
            db.session.commit()
            return route

        added = active_ids - existing_set
        if not added:
            surviving = [did for did in existing if did in active_ids]
            route.optimized_order = surviving
            route.pinned_stops = self.clamp_pins(route.pinned_stops, surviving)
            self._sync_committed_delivery_id(route)
            db.session.commit()
            logger.info(
                "route_override_shrunk driver=%s route=%s removed=%s remaining=%d",
                route.delivery_person_id,
                route.id,
                sorted(existing_set - active_ids),
                len(surviving),
            )
            return route

        return None

    def _sync_committed_delivery_id(self, route: DeliveryRoute) -> None:
        """Refresh `extra_data["committed_delivery_id"]` to the driver's
        CURRENT committed stop. Callers own the commit boundary — this only
        mutates `route.extra_data` in place (no-op, no write, if the value is
        already correct)."""
        committed = self.get_committed_stop(route.delivery_person_id)
        current = committed.id if committed is not None else None
        extra = dict(route.extra_data or {})
        if extra.get("committed_delivery_id") != current:
            extra["committed_delivery_id"] = current
            route.extra_data = extra

    def compute_diversion_gain(
        self,
        driver_id: int,
        new_delivery_id: int,
        *,
        traffic: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Minutes saved by visiting `new_delivery_id` BEFORE the committed
        stop instead of after it (spec §7). None when the driver has no
        committed stop (no offer is ever made then — the optimizer just does
        the right thing silently), no usable coordinates exist, or the
        driver has never shared a location.

        Both options are costed on ONE matrix snapshot with the SAME solver
        (`_solve_with_pins`, minutes objective) — the §10 two-snapshot and
        two-objective bugs must not be reintroduced here. "Is this driver
        anchored, and on what" comes from `_resolve_anchor`, the ONE
        definition shared with `optimize_for_driver` — never a second inline
        copy of the rule.
        """
        active = self._load_active_deliveries(driver_id)
        if not active:
            return None
        if self._location_status(driver_id) == "missing":
            return None

        new_delivery = Delivery.query.get(new_delivery_id)
        if new_delivery is None:
            return None
        self._geocode_missing_addresses([new_delivery] + active)

        new_point = self._delivery_point(new_delivery)
        if new_point is None:
            return None
        active = [d for d in active if self._delivery_point(d) is not None]

        committed, committed_point, is_anchored = self._resolve_anchor(driver_id, active)
        if not is_anchored:
            return None

        person = DeliveryPerson.query.filter_by(user_id=driver_id).first()
        if person is None or person.current_location_lat is None or person.current_location_lng is None:
            return None
        origin = (person.current_location_lat, person.current_location_lng)

        tail = [d for d in active if d.id != committed.id]
        points = [origin, committed_point] + [self._delivery_point(d) for d in tail] + [new_point]
        matrix, _source = self.maps.get_distance_matrix(points, traffic=traffic)

        committed_idx = 1
        new_idx = len(points) - 1
        order_committed_first = self._solve_with_pins(matrix, {committed_idx: 0}, start_idx=0)
        order_new_first = self._solve_with_pins(matrix, {new_idx: 0}, start_idx=0)
        gain = self._sum_path_minutes(matrix, order_committed_first) - self._sum_path_minutes(matrix, order_new_first)
        return {
            "gain_minutes": gain,
            "committed_delivery_id": committed.id,
            "committed_order_number": (committed.order.order_number if committed.order else str(committed.order_id)),
        }

    # ----- internal helpers -------------------------------------------------

    def _load_active_deliveries(self, driver_id: int) -> List[Delivery]:
        return (
            Delivery.query.filter(
                Delivery.delivery_person_id == driver_id,
                Delivery.status.in_(ACTIVE_DELIVERY_STATUSES),
            )
            .order_by(Delivery.id.asc())
            .all()
        )

    def current_route(self, driver_id: int) -> Optional[DeliveryRoute]:
        """Today's route row for this driver, newest first. Public: the dispatch
        read/write services need the same row this service upserts into."""
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            DeliveryRoute.query.filter(
                DeliveryRoute.delivery_person_id == driver_id,
                DeliveryRoute.route_date >= today_start,
            )
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )

    def active_deliveries(self, driver_id: int) -> List[Delivery]:
        """The driver's active delivery set — public.

        RouteEditService's staleness guard MUST use the same definition of
        "active" this service optimises over. A second, divergent copy of that
        filter is a correctness bug waiting to happen, so it is exposed rather
        than reimplemented.
        """
        return self._load_active_deliveries(driver_id)

    def get_committed_stop(self, driver_id: int) -> Optional[Delivery]:
        """The driver's committed stop (spec 2026-08-11 §4.1), or None.

        Among active deliveries whose status is IN_TRANSIT or ARRIVED, the one
        whose most recent transition INTO its current status is latest by
        DeliveryStatusHistory.changed_at. Defined by recency of STARTING, not
        by optimized_order position — that is what lets a driver start any
        stop (free start order, §6.4) without an IN_TRANSIT→ASSIGNED edge.

        Bounded by COMMITTED_STOP_MAX_AGE_HOURS: a delivery whose most recent
        transition into its current status is older than that no longer
        counts as committed — it is still active and still gets routed, it
        just stops anchoring the route (a delivery that never completes must
        not pin itself to position 0 forever). Applied identically in the
        primary path and the defensive fallback below so the two never
        disagree.
        """
        max_age_hours = current_app.config.get("COMMITTED_STOP_MAX_AGE_HOURS", 12)
        threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        row = (
            db.session.query(
                DeliveryStatusHistory.delivery_id,
                func.max(DeliveryStatusHistory.changed_at).label("committed_at"),
            )
            .join(Delivery, Delivery.id == DeliveryStatusHistory.delivery_id)
            .filter(
                Delivery.delivery_person_id == driver_id,
                Delivery.status.in_(COMMITTED_STATUSES),
                DeliveryStatusHistory.new_status == Delivery.status,
                DeliveryStatusHistory.changed_at >= threshold,
            )
            .group_by(DeliveryStatusHistory.delivery_id)
            .order_by(
                func.max(DeliveryStatusHistory.changed_at).desc(),
                # Deterministic tiebreak on an exact-timestamp collision. The
                # grouped column itself, so it stays legal under Postgres's
                # strict GROUP BY (no non-aggregated, non-grouped column).
                DeliveryStatusHistory.delivery_id.desc(),
            )
            .first()
        )
        if row is not None:
            return Delivery.query.get(row.delivery_id)
        # Defensive fallback: an IN_TRANSIT/ARRIVED delivery with NO matching
        # history row at all (created/transitioned outside the status
        # services). `updated_at` is only a trustworthy proxy for "when did
        # this transition happen" in that no-history case; a delivery that
        # DOES have a matching history row must be judged by that row alone
        # (already done above) — never re-admitted here, since `updated_at`
        # bumps on unrelated field writes (e.g. GPS pings) and would make a
        # month-stale delivery look fresh again. Hence the NOT EXISTS guard.
        history_exists_for_current_status = DeliveryStatusHistory.query.filter(
            DeliveryStatusHistory.delivery_id == Delivery.id,
            DeliveryStatusHistory.new_status == Delivery.status,
        ).exists()
        return (
            Delivery.query.filter(
                Delivery.delivery_person_id == driver_id,
                Delivery.status.in_(COMMITTED_STATUSES),
                Delivery.updated_at >= threshold,
                ~history_exists_for_current_status,
            )
            .order_by(Delivery.updated_at.desc(), Delivery.id.desc())
            .first()
        )

    def _resolve_anchor(
        self, driver_id: int, deliveries: List[Delivery]
    ) -> Tuple[Optional[Delivery], Optional[Point], bool]:
        """The anchor rule's ONE definition (spec §4.2; extracted 2026-08-12
        review fix). `optimize_for_driver` and `compute_diversion_gain` both
        derive "is this route anchored, and on what" from this single call —
        never from an inline copy of the expression. A second copy was
        exactly the drift this plan exists to prevent: if §4.2 ever grows a
        condition applied to only one copy, a diversion offer could end up
        measured from GPS while the solve anchors on the committed stop,
        silently reintroducing the U-turn suggestion this whole plan exists
        to prevent.

        Returns `(committed, committed_point, is_anchored)`. `committed`/
        `committed_point` may be non-None even when `is_anchored` is False
        (a committed stop exists but isn't a member of `deliveries`) —
        callers must gate on `is_anchored`, never on `committed is not None`
        alone.
        """
        committed = self.get_committed_stop(driver_id)
        committed_point = self._delivery_point(committed) if committed is not None else None
        is_anchored = (
            committed is not None and committed_point is not None and any(d.id == committed.id for d in deliveries)
        )
        return committed, committed_point, is_anchored

    def _should_debounce_location_trigger(self, driver_id: int) -> bool:
        """Skip a location_update re-solve when the last solve is fresh OR the
        driver barely moved since it (spec §4.5). Applies ONLY to
        trigger='location_update' — status transitions move the anchor and
        must always solve. State lives in the route row; no Redis needed."""
        route = self.current_route(driver_id)
        if route is None:
            return False
        extra = route.extra_data or {}
        last_at_raw = extra.get("last_optimized_at")
        if last_at_raw:
            try:
                last_at = _ensure_aware(datetime.fromisoformat(last_at_raw))
                window = int(current_app.config.get("ROUTE_OPTIMIZE_DEBOUNCE_SECONDS", 60))
                if (datetime.now(timezone.utc) - last_at).total_seconds() < window:
                    return True
            except (TypeError, ValueError):
                pass
        last_loc = extra.get("last_driver_location")
        person = DeliveryPerson.query.filter_by(user_id=driver_id).first()
        if last_loc and person and person.current_location_lat is not None and person.current_location_lng is not None:
            from business_app.utils.helpers import calculate_distance

            moved_m = (
                calculate_distance(
                    float(last_loc[0]),
                    float(last_loc[1]),
                    person.current_location_lat,
                    person.current_location_lng,
                )
                * 1000.0
            )
            min_move = float(current_app.config.get("ROUTE_OPTIMIZE_MIN_MOVE_METERS", 150))
            if moved_m < min_move:
                return True
        return False

    @staticmethod
    def _first_unstarted(order_ids: List[int], status_by_id: Dict[int, Any]) -> Optional[int]:
        """First id in `order_ids` whose CURRENT status is unstarted.
        Ids absent from `status_by_id` (completed / cancelled / unassigned)
        are skipped — they can never be 'the next stop'."""
        for did in order_ids:
            if status_by_id.get(int(did)) in UNSTARTED_STATUSES:
                return int(did)
        return None

    def compute_materiality(
        self,
        *,
        prev_order: List[int],
        new_order: List[int],
        deliveries: List[Delivery],
        trigger: str,
    ) -> Dict[str, Any]:
        """The published materiality verdict (spec §5.1). Persisted to
        DeliveryRoute.extra_data['materiality'] — the ONLY representation;
        the Celery gate, the webhook and the bot read it, never re-derive it."""
        status_by_id = {d.id: d.status for d in deliveries}
        prev_ids = [int(x) for x in (prev_order or [])]
        new_ids = [int(x) for x in (new_order or [])]
        return {
            "head_changed": self._first_unstarted(prev_ids, status_by_id)
            != self._first_unstarted(new_ids, status_by_id),
            "set_changed": set(prev_ids) != set(new_ids),
            "sequence_changed": prev_ids != new_ids,
            "driver_initiated": trigger in DRIVER_INITIATED_TRIGGERS,
            "trigger": trigger,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _stamp_materiality(
        self,
        route: DeliveryRoute,
        *,
        prev_order: List[int],
        deliveries: List[Delivery],
        trigger: str,
    ) -> None:
        """Merge a fresh materiality verdict into an already-settled route
        (the manual-override early-return paths, which bypass _upsert_route)."""
        merged = dict(route.extra_data or {})
        merged["materiality"] = self.compute_materiality(
            prev_order=prev_order,
            new_order=[int(x) for x in (route.optimized_order or [])],
            deliveries=deliveries,
            trigger=trigger,
        )
        route.extra_data = merged
        db.session.commit()

    def delivery_point(self, delivery: Delivery) -> Optional[Point]:
        """A delivery's destination coordinates, or None — public for the same
        reason as `active_deliveries`."""
        return self._delivery_point(delivery)

    @staticmethod
    def clamp_pins(pinned: Optional[Dict[str, Any]], ordered_ids: List[int]) -> Dict[str, int]:
        """Drop pins whose delivery left the route, then re-anchor each
        surviving pin to its actual 0-based index within `ordered_ids`.

        Pin values are a literal position within `optimized_order` (Task 1's
        contract), so after a shrink the correct new value is wherever that
        delivery actually landed in the shrunk sequence — NOT a rank among
        the other surviving pins. Without this, a pin recorded at slot 2 on
        a 3-stop route would point past the end of a 2-stop one, and
        `_solve_with_pins` would clamp it to "last" — silently promoting a
        mid-route pin to the final stop.
        """
        if not pinned:
            return {}
        alive = {str(did) for did in ordered_ids}
        index_of = {str(did): idx for idx, did in enumerate(ordered_ids)}
        return {k: index_of[str(k)] for k in pinned if str(k) in alive}

    def _delivery_point(self, delivery: Delivery) -> Optional[Point]:
        order = delivery.order
        if order is None:
            return None
        addr = order.delivery_address
        if addr is None or addr.latitude is None or addr.longitude is None:
            return None
        return (addr.latitude, addr.longitude)

    def _geocode_missing_addresses(self, deliveries: List[Delivery]) -> None:
        """Fill in lat/lng on UserAddress rows that lack them. Best-effort."""
        for d in deliveries:
            order = d.order
            if order is None or order.delivery_address is None:
                continue
            addr = order.delivery_address
            if addr.latitude is not None and addr.longitude is not None:
                continue
            try:
                geocoded = self.maps.geocode_address(
                    address=addr.full_address or addr.street_address or "",
                    city=addr.city or "Tashkent",
                )
            except (ExternalServiceError, Exception) as exc:  # noqa: BLE001
                logger.warning("geocode failed for address=%s: %s", addr.id, exc)
                continue
            addr.latitude = geocoded.get("latitude")
            addr.longitude = geocoded.get("longitude")
        try:
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            logger.warning("commit after geocoding failed: %s", exc)

    def _resolve_start_point(self, driver_id: int, deliveries: List[Delivery]) -> Tuple[Point, str]:
        """Pick the best available start coordinate for the driver."""
        fresh_seconds = current_app.config.get("DRIVER_LOCATION_FRESH_SECONDS", LOCATION_FRESH_DEFAULT_SECONDS)
        threshold = datetime.now(timezone.utc) - timedelta(seconds=fresh_seconds)

        person = DeliveryPerson.query.filter_by(user_id=driver_id).first()
        person_last_update = _ensure_aware(person.last_location_update) if person else None
        if (
            person
            and person.current_location_lat is not None
            and person.current_location_lng is not None
            and person_last_update is not None
            and person_last_update >= threshold
        ):
            return (person.current_location_lat, person.current_location_lng), "driver_live"

        # Last completed delivery from earlier today (location snapshot in history)
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        last_completed = (
            DeliveryStatusHistory.query.join(Delivery, Delivery.id == DeliveryStatusHistory.delivery_id)
            .filter(
                Delivery.delivery_person_id == driver_id,
                DeliveryStatusHistory.new_status == DeliveryStatus.DELIVERED,
                DeliveryStatusHistory.changed_at >= today_start,
                DeliveryStatusHistory.location_lat.isnot(None),
                DeliveryStatusHistory.location_lng.isnot(None),
            )
            .order_by(DeliveryStatusHistory.changed_at.desc())
            .first()
        )
        if last_completed:
            return (last_completed.location_lat, last_completed.location_lng), "last_completed"

        # Depot from a recent route, if any
        recent_route = (
            DeliveryRoute.query.filter_by(delivery_person_id=driver_id)
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
        if recent_route and recent_route.start_location_lat is not None:
            return (recent_route.start_location_lat, recent_route.start_location_lng), "depot"

        # Fall back to the configured warehouse (spec 8.5). NEVER a route
        # stop — only the start anchor for a driver who has never shared
        # location today.
        return (
            float(current_app.config.get("WAREHOUSE_LATITUDE", TASHKENT_COORDINATES["latitude"])),
            float(current_app.config.get("WAREHOUSE_LONGITUDE", TASHKENT_COORDINATES["longitude"])),
        ), "warehouse"

    # ----- TSP --------------------------------------------------------------

    @classmethod
    def _solve_tsp(
        cls,
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        *,
        start_idx: int = 0,
        weight: str = "duration_minutes",
    ) -> List[int]:
        """Optimal stop sequence for an open-ended path TSP (no return to start).

        Dispatches to:
          - `_solve_tsp_exact` (Held-Karp DP) when n-1 <= HELDKARP_MAX_DELIVERIES;
          - `_solve_tsp_heuristic` (nearest-neighbor seed + 2-opt) otherwise.
        Both branches return a list of indices starting with `start_idx` and
        visiting every node in the matrix exactly once.
        """
        n = max(i for i, _ in matrix.keys()) + 1 if matrix else 0
        if n <= 1:
            return [start_idx] if n == 1 else []
        if n - 1 <= HELDKARP_MAX_DELIVERIES:
            return cls._solve_tsp_exact(matrix, start_idx=start_idx, weight=weight)
        return cls._solve_tsp_heuristic(matrix, start_idx=start_idx, weight=weight)

    @staticmethod
    def _solve_tsp_exact(
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        *,
        start_idx: int = 0,
        weight: str = "duration_minutes",
    ) -> List[int]:
        """Held-Karp DP for the open-ended path TSP.

        State: (visited_mask, last_node) -> minimum cost of a path starting at
        `start_idx`, visiting every node in `visited_mask` exactly once, and
        ending at `last_node`. Open-ended: no return edge to `start_idx`.

        Mask convention: bit `v` is set when node `v` has been visited.
        `start_idx` is always considered visited and is NOT included in the
        bits we iterate over (it's the implicit prefix of every path).
        """
        n = max(i for i, _ in matrix.keys()) + 1 if matrix else 0
        if n <= 1:
            return [start_idx] if n == 1 else []

        others = [v for v in range(n) if v != start_idx]
        if not others:
            return [start_idx]

        # Map node id -> bit position among `others` so masks stay compact
        # even when start_idx != 0.
        bit_of: Dict[int, int] = {v: i for i, v in enumerate(others)}

        INF = float("inf")
        # dp[mask][v] = best cost ending at v having visited exactly the
        # nodes encoded in `mask` (plus the implicit start_idx prefix).
        # parent[mask][v] = predecessor of v on the best such path.
        size = 1 << len(others)
        dp: List[List[float]] = [[INF] * n for _ in range(size)]
        parent: List[List[int]] = [[-1] * n for _ in range(size)]

        # Base: paths of length 1 (start_idx -> v).
        for v in others:
            mask = 1 << bit_of[v]
            dp[mask][v] = matrix[(start_idx, v)][weight]
            parent[mask][v] = start_idx

        # Iterate masks in increasing popcount so subproblems are ready.
        for mask in range(1, size):
            for v in others:
                vb = 1 << bit_of[v]
                if not (mask & vb):
                    continue
                prev_mask = mask ^ vb
                if prev_mask == 0:
                    continue  # base case already filled above
                best_cost = dp[mask][v]
                best_prev = parent[mask][v]
                for u in others:
                    if u == v:
                        continue
                    ub = 1 << bit_of[u]
                    if not (prev_mask & ub):
                        continue
                    cand = dp[prev_mask][u] + matrix[(u, v)][weight]
                    if cand < best_cost:
                        best_cost = cand
                        best_prev = u
                dp[mask][v] = best_cost
                parent[mask][v] = best_prev

        full_mask = size - 1
        end_node = min(others, key=lambda v: dp[full_mask][v])

        # Reconstruct path by walking parents back to start_idx.
        path_rev: List[int] = []
        mask = full_mask
        cur = end_node
        while cur != start_idx:
            path_rev.append(cur)
            prev = parent[mask][cur]
            mask ^= 1 << bit_of[cur]
            cur = prev
        path_rev.append(start_idx)
        return list(reversed(path_rev))

    @staticmethod
    def _solve_tsp_heuristic(
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        *,
        start_idx: int = 0,
        weight: str = "duration_minutes",
    ) -> List[int]:
        """Nearest-neighbor seed + 2-opt improvement, open-ended (no return to start)."""
        n = max(i for i, _ in matrix.keys()) + 1 if matrix else 0
        if n <= 1:
            return [start_idx] if n == 1 else []

        # Nearest-neighbor seed.
        unvisited = set(range(n))
        unvisited.discard(start_idx)
        path = [start_idx]
        current = start_idx
        while unvisited:
            nxt = min(unvisited, key=lambda j: matrix[(current, j)][weight])
            path.append(nxt)
            unvisited.discard(nxt)
            current = nxt

        # 2-opt improvement (open path: never reverse the start at index 0,
        # but every other position — including the last — is fair game).
        # Uses *best-improvement*: each pass evaluates all candidate swaps
        # against a frozen `best` and applies the single biggest improvement.
        # First-improvement (apply the first improving swap found) can lock
        # the search into a local optimum because an early small win blocks
        # a later larger one from the same seed.
        def path_cost(p: List[int]) -> float:
            return sum(matrix[(p[i], p[i + 1])][weight] for i in range(len(p) - 1))

        best = path
        best_cost = path_cost(best)
        max_passes = 20
        for _ in range(max_passes):
            candidate: Optional[List[int]] = None
            candidate_cost = best_cost
            for i in range(1, len(best) - 1):
                for k in range(i + 1, len(best)):
                    new_path = best[:i] + best[i : k + 1][::-1] + best[k + 1 :]
                    new_cost = path_cost(new_path)
                    if new_cost + 1e-9 < candidate_cost:
                        candidate = new_path
                        candidate_cost = new_cost
            if candidate is None:
                break
            best = candidate
            best_cost = candidate_cost
        return best

    @classmethod
    def _solve_with_pins(
        cls,
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        pinned_positions: Dict[int, int],
        *,
        start_idx: int = 0,
        weight: str = "duration_minutes",
    ) -> List[int]:
        """Open-path TSP that honours fixed positions for pinned nodes.

        `pinned_positions` maps a matrix index to its 0-based slot among the
        DELIVERY nodes (start excluded). Returns a path beginning with
        `start_idx`, so a node pinned to slot 0 lands at path[1].

        With no pins this delegates straight to `_solve_tsp` and is therefore
        byte-identical to the unconstrained behaviour. With pins it is an
        explicitly approximate three-step construction — solve the free nodes
        optimally, splice the pinned ones into their slots, then improve with a
        2-opt that may not move a pinned element. It is NOT a claimed optimum
        under the constraint; honouring the admin's pin is the point.
        """
        n = max(i for i, _ in matrix.keys()) + 1 if matrix else 0
        if n <= 1:
            return [start_idx] if n == 1 else []
        if not pinned_positions:
            return cls._solve_tsp(matrix, start_idx=start_idx, weight=weight)

        pinned_nodes = {int(node) for node in pinned_positions}
        free_nodes = [v for v in range(n) if v != start_idx and v not in pinned_nodes]

        # 1. Optimal sequence over start + free nodes. `_solve_tsp` infers its
        #    node count from max(matrix key), so the sub-problem is re-indexed
        #    into a compact 0..k space rather than passed sparsely.
        if free_nodes:
            local_nodes = [start_idx] + free_nodes
            local_of = {g: i for i, g in enumerate(local_nodes)}
            local_matrix = {
                (local_of[a], local_of[b]): matrix[(a, b)] for a in local_nodes for b in local_nodes if a != b
            }
            local_order = cls._solve_tsp(local_matrix, start_idx=0, weight=weight)
            sequence = [local_nodes[i] for i in local_order if i != 0]
        else:
            sequence = []

        # 2. Splice pinned nodes into their slots, lowest slot first so each
        #    insertion index means what the admin saw. Out-of-range slots clamp.
        #    Skip pins whose keys are start_idx or out of matrix range; a
        #    duplicate start_idx in sequence would violate the path invariant.
        for node, pos in sorted(pinned_positions.items(), key=lambda kv: (kv[1], kv[0])):
            node = int(node)
            if node == start_idx or node < 0 or node >= n:
                continue
            idx = max(0, min(int(pos), len(sequence)))
            sequence.insert(idx, node)

        # 3. Constrained 2-opt over the whole path with pinned slots frozen.
        path = [start_idx] + sequence
        frozen = {i + 1 for i, node in enumerate(sequence) if node in pinned_nodes}
        return cls._two_opt_frozen(matrix, path, frozen_positions=frozen, weight=weight)

    @staticmethod
    def _two_opt_frozen(
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        path: List[int],
        frozen_positions: set,
        weight: str = "duration_minutes",
    ) -> List[int]:
        """Best-improvement 2-opt that never relocates a frozen position.

        `frozen_positions` are indices INTO `path`. A 2-opt move reverses the
        slice [i..k]; any element inside that slice moves, so a candidate whose
        slice covers a frozen index is rejected outright. Elements outside the
        slice keep their index, which is exactly the invariant pins need.
        """

        def path_cost(p: List[int]) -> float:
            return sum(matrix[(p[i], p[i + 1])][weight] for i in range(len(p) - 1))

        best = list(path)
        best_cost = path_cost(best)
        max_passes = 20
        for _ in range(max_passes):
            candidate: Optional[List[int]] = None
            candidate_cost = best_cost
            for i in range(1, len(best) - 1):
                for k in range(i + 1, len(best)):
                    if any(pos in frozen_positions for pos in range(i, k + 1)):
                        continue
                    new_path = best[:i] + best[i : k + 1][::-1] + best[k + 1 :]
                    new_cost = path_cost(new_path)
                    if new_cost + 1e-9 < candidate_cost:
                        candidate = new_path
                        candidate_cost = new_cost
            if candidate is None:
                break
            best = candidate
            best_cost = candidate_cost
        return best

    @staticmethod
    def _sum_path_km(matrix: Dict[Tuple[int, int], Dict[str, float]], path: List[int]) -> float:
        return sum(matrix[(path[i], path[i + 1])]["distance_km"] for i in range(len(path) - 1))

    @staticmethod
    def _sum_path_minutes(matrix: Dict[Tuple[int, int], Dict[str, float]], path: List[int]) -> float:
        return sum(matrix[(path[i], path[i + 1])]["duration_minutes"] for i in range(len(path) - 1))

    @classmethod
    def _sum_route_metrics(
        cls,
        matrix: Dict[Tuple[int, int], Dict[str, float]],
        path: List[int],
    ) -> Tuple[float, float]:
        """Route totals = travel + flat per-stop service time (spec 8.4).

        `path` includes the start node at index 0, so stops served =
        len(path) - 1. A constant per stop is added to every candidate
        sequence equally, so it can never change which sequence the solver
        prefers — it belongs in TOTALS, never in the matrix. This helper is
        the ONLY place route totals are computed (`optimize_for_driver` and
        `RouteEditService._refresh_metrics` both call it), so the fold
        applies to both writers — SSOT. Insertion/diversion deltas
        (`_sum_path_km`/`_sum_path_minutes`, `compute_diversion_gain`) stay
        travel-only on purpose: they measure a detour/gain THRESHOLD, not a
        published total, and a constant per stop would either retune the
        threshold silently or cancel out unnoticed.
        """
        km = cls._sum_path_km(matrix, path)
        minutes = cls._sum_path_minutes(matrix, path)
        service_minutes = float(current_app.config.get("ROUTE_SERVICE_TIME_MINUTES", 4.0))
        stops = max(len(path) - 1, 0)
        return km, minutes + service_minutes * stops

    # ----- persistence ------------------------------------------------------

    def _upsert_route(
        self,
        *,
        driver_id: int,
        start_point: Point,
        optimized_delivery_ids: List[int],
        total_km: float,
        total_min: float,
        extra: Dict[str, Any],
        keep_override: bool = False,
    ) -> DeliveryRoute:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        route = (
            DeliveryRoute.query.filter(
                DeliveryRoute.delivery_person_id == driver_id,
                DeliveryRoute.route_date >= today_start,
            )
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
        if route is None:
            route = DeliveryRoute(
                name=f"Auto route {today_start.strftime('%Y-%m-%d')} driver={driver_id}",
                delivery_person_id=driver_id,
                start_location_lat=start_point[0],
                start_location_lng=start_point[1],
                route_date=today_start,
                status="planned",
            )
            db.session.add(route)

        route.start_location_lat = start_point[0]
        route.start_location_lng = start_point[1]
        route.optimized_order = optimized_delivery_ids
        route.total_distance_km = total_km
        route.estimated_duration_minutes = int(round(total_min))
        if keep_override:
            # The admin's pins were honoured in the sequence above; keep the
            # lock (and re-clamp, since the set changed) so the next trigger
            # doesn't quietly re-sequence what dispatch decided.
            route.manual_override = True
            route.pinned_stops = self.clamp_pins(route.pinned_stops, optimized_delivery_ids)
        else:
            route.manual_override = False
            route.pinned_stops = {}
            route.overridden_by = None
            route.overridden_at = None
        merged_extra = dict(route.extra_data or {})
        merged_extra.update(extra)
        merged_extra["last_optimized_at"] = datetime.now(timezone.utc).isoformat()
        route.extra_data = merged_extra

        db.session.commit()
        return route

    # ----- read API for the bot --------------------------------------------

    def annotate_active_items(
        self,
        driver_id: int,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Sort `items` by the driver's optimized order and add per-item flags.

        Mutates and returns the same list (item shape from the existing
        /delivery/active builder — must contain `delivery_id`,
        `destination_latitude`, `destination_longitude`).
        """
        # TODAY's route only (§10 fix): the unfiltered newest-row read could
        # render yesterday's sequence as today's plan. current_route is the
        # single definition of "the driver's route row" — reuse it.
        route = self.current_route(int(driver_id))
        order_map: Dict[int, int] = {}
        if route and route.optimized_order:
            for pos, did in enumerate(route.optimized_order):
                order_map[did] = pos

        items.sort(key=lambda it: (order_map.get(it.get("delivery_id"), 1_000_000), it.get("delivery_id") or 0))

        for idx, it in enumerate(items):
            it["route_position"] = order_map.get(it.get("delivery_id"))
            it["is_next"] = idx == 0
            it["eta_minutes_from_current_location"] = None
            it["distance_km_to_next"] = None
            it["eta_source"] = None
            it["eta_suppressed"] = False

        if items:
            top = items[0]
            dest_lat = top.get("destination_latitude")
            dest_lng = top.get("destination_longitude")
            # Only compute next-leg ETA when we have a real, fresh driver
            # location. Otherwise the resolved start point is the depot or
            # city-centre fallback and the ETA would be misleading — the bot
            # already knows to suppress those fields when location_status
            # isn't "fresh".
            if dest_lat is not None and dest_lng is not None and self._location_status(int(driver_id)) == "fresh":
                try:
                    start_point, _ = self._resolve_start_point(int(driver_id), [])
                    # Tier 1: Google Routes TRAFFIC_AWARE — the single
                    # traffic-aware number in the product (spec 8.2).
                    # Silent None on any failure or when unconfigured.
                    leg = google_routes.get_traffic_aware_leg(start_point, (dest_lat, dest_lng))
                    if leg is not None:
                        top["eta_minutes_from_current_location"] = round(leg["duration_minutes"])
                        top["distance_km_to_next"] = round(leg["distance_km"], 1)
                        top["eta_source"] = "google_traffic"
                    else:
                        matrix, source = self.maps.get_distance_matrix(
                            [start_point, (dest_lat, dest_lng)], traffic=True
                        )
                        if source == "haversine":
                            # Honest-ETA rule (spec 8.4): a straight-line
                            # estimate must never render as a measured ETA.
                            # The decision is DECIDED HERE and published as a
                            # field; values stay None so no client can show
                            # them. The bot reads `eta_suppressed`, it never
                            # re-derives the rule (SSOT).
                            top["eta_suppressed"] = True
                        else:
                            top["eta_minutes_from_current_location"] = round(matrix[(0, 1)]["duration_minutes"])
                            top["distance_km_to_next"] = round(matrix[(0, 1)]["distance_km"], 1)
                            # `eta_source` is a PROVENANCE field (final
                            # review round, I3) — "cache" is a tier label,
                            # not a provider, so it must never leak through
                            # here. Recover the provider that actually
                            # produced the cached data; if that isn't
                            # possible (entry predates this fix, or expired
                            # between the two lookups), say so explicitly
                            # rather than publish the tier name.
                            if source == "cache":
                                top["eta_source"] = (
                                    self.maps.get_cached_matrix_source(
                                        [start_point, (dest_lat, dest_lng)], traffic=True
                                    )
                                    or "cache_unknown_provider"
                                )
                            else:
                                top["eta_source"] = source
                except Exception as exc:  # noqa: BLE001
                    logger.warning("next-leg ETA failed driver=%s: %s", driver_id, exc)
                    # Distinguish "computed then errored" from "nothing
                    # computed" (final review round, I4): both previously
                    # left `(eta_suppressed=False, eta_source=None)`,
                    # indistinguishable from "driver has no fresh GPS" to a
                    # Plan 3 consumer. `eta_suppressed` stays False — this is
                    # NOT the honest-ETA-suppression case, it's a failure.
                    #
                    # Null the value fields too (residuals round, item 4): the
                    # `try` block can raise AFTER partially populating them —
                    # e.g. `distance_km_to_next` raising right after
                    # `eta_minutes_from_current_location` was already
                    # assigned, or (since I3) `get_cached_matrix_source`
                    # raising after BOTH values were already set. Without
                    # this, `eta_source="error"` could coexist with a real
                    # (partial) ETA — a fourth, undocumented state. The
                    # published contract stays exactly three states.
                    top["eta_minutes_from_current_location"] = None
                    top["distance_km_to_next"] = None
                    top["eta_source"] = "error"
        return items

    def location_status(self, driver_id: int) -> str:
        """Public wrapper for current location freshness."""
        return self._location_status(driver_id)

    def _location_status(self, driver_id: int) -> str:
        person = DeliveryPerson.query.filter_by(user_id=driver_id).first()
        return self.location_status_for_person(person)

    @staticmethod
    def location_status_for_person(person: Optional[DeliveryPerson]) -> str:
        """Freshness status for an ALREADY-LOADED `DeliveryPerson` row.

        Same rule as `_location_status`/`location_status` — factored out so a
        caller that already has the row in hand (e.g. DispatchService._drivers,
        which loads every active driver in one query) doesn't pay a second
        per-driver query just to reach the same three values. `_location_status`
        delegates here too, so there is exactly one implementation of this rule;
        if it ever drifted, the dispatch map would silently disagree with what
        the driver's own bot shows.
        """
        if not person or person.current_location_lat is None or person.last_location_update is None:
            return "missing"
        fresh_seconds = current_app.config.get("DRIVER_LOCATION_FRESH_SECONDS", LOCATION_FRESH_DEFAULT_SECONDS)
        threshold = datetime.now(timezone.utc) - timedelta(seconds=fresh_seconds)
        return "fresh" if _ensure_aware(person.last_location_update) >= threshold else "stale"

    def build_route_summary(self, driver_id: int, remaining_count: int) -> Dict[str, Any]:
        """Card-header numbers for the staff bot's route card (Phase 3 Task 1).

        Published as a FIELD so the bot never re-derives progress, the
        committed stop, or a finish estimate (CLAUDE.md SSOT). `finish_eta`
        is honest-or-absent: None when there is no route today, the matrix
        fell back to haversine, or no duration was computed — the bot omits
        the fragment instead of showing a straight-line guess (spec §8.4).

        TIMEZONE TRAP for consumers: both values leave this method in UTC,
        but `TimezoneMiddleware._convert_response_datetimes` rewrites any
        response key literally named `updated_at`, at any nesting depth, into
        DISPLAY_TIMEZONE. `finish_eta` is not on that field list. So over the
        wire `updated_at` arrives at +05:00 and `finish_eta` arrives as UTC —
        parse both as tz-aware and never assume a `Z` suffix.
        """
        from zoneinfo import ZoneInfo

        from shared.constants import DISPLAY_TIMEZONE

        local_tz = ZoneInfo(DISPLAY_TIMEZONE)
        local_midnight = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = local_midnight.astimezone(timezone.utc)

        completed_today = Delivery.query.filter(
            Delivery.delivery_person_id == driver_id,
            Delivery.status == DeliveryStatus.DELIVERED,
            Delivery.delivered_at >= day_start_utc,
        ).count()

        route = self.current_route(driver_id)
        extra = dict(route.extra_data or {}) if route is not None else {}
        committed_id = extra.get("committed_delivery_id")
        updated_at = extra.get("last_optimized_at")

        finish_eta = None
        if (
            route is not None
            and remaining_count > 0
            and not extra.get("fallback")
            and route.estimated_duration_minutes is not None
            and updated_at
        ):
            try:
                solved_at = datetime.fromisoformat(updated_at)
                finish_eta = (solved_at + timedelta(minutes=int(route.estimated_duration_minutes))).isoformat()
            except (TypeError, ValueError):
                finish_eta = None

        return {
            "remaining": int(remaining_count),
            "stops_completed_today": int(completed_today),
            "stops_total_today": int(completed_today) + int(remaining_count),
            "committed_delivery_id": committed_id,
            "finish_eta": finish_eta,
            "updated_at": updated_at,
        }
