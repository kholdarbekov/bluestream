"""
Route optimization service.

Computes the optimal stop sequence for a driver's active deliveries using a
Yandex Distance Matrix (with traffic) + nearest-neighbor + 2-opt TSP solver.
The result is persisted to `DeliveryRoute.optimized_order` so the staff bot's
"My active deliveries" can render the list in the right order with a "Next
stop" badge on top.

Also exposes `compute_insertion_cost(driver_id, new_delivery_id)` to evaluate
whether a freshly-pooled order can be slipped into a driver's already-planned
route at low detour cost. Used by the pool-arrival webhook flow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.services.maps_service import MapsService
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

LOCATION_FRESH_DEFAULT_SECONDS = 1800  # 30 min

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
        pinned_by_delivery: Dict[str, int] = {}
        keep_override = False

        if existing_route is not None and existing_route.manual_override:
            if respect_override:
                settled = self._apply_override_policy(existing_route, deliveries)
                if settled is not None:
                    return settled
                # Falls through: the set GREW, so we re-solve the unpinned
                # stops and splice the admin's pins back into their slots.
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

        start_point, start_source = self._resolve_start_point(driver_id, deliveries)
        delivery_points = [self._delivery_point(d) for d in deliveries]
        all_points = [start_point] + delivery_points

        matrix, source = self.maps.get_distance_matrix(all_points, traffic=traffic)

        # Translate delivery-id pins into matrix indices (index 0 is the start).
        pinned_positions = {
            idx + 1: int(pinned_by_delivery[str(d.id)])
            for idx, d in enumerate(deliveries)
            if str(d.id) in pinned_by_delivery
        }
        if pinned_positions:
            order = self._solve_with_pins(matrix, pinned_positions, start_idx=0)
        else:
            order = self._solve_tsp(matrix, start_idx=0)

        # `order` includes index 0 (start). Strip it; the rest are delivery indices in [1..N].
        route_indices = [i - 1 for i in order if i != 0]
        optimized_delivery_ids = [deliveries[i].id for i in route_indices]

        total_km, total_min = self._sum_route_metrics(matrix, order)

        route = self._upsert_route(
            driver_id=driver_id,
            start_point=start_point,
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
            return route

        added = active_ids - existing_set
        if not added:
            surviving = [did for did in existing if did in active_ids]
            route.optimized_order = surviving
            route.pinned_stops = self.clamp_pins(route.pinned_stops, surviving)
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

    def compute_insertion_cost(
        self,
        driver_id: int,
        new_delivery_id: int,
        *,
        traffic: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Find the cheapest insertion of `new_delivery_id` into driver's route.

        Returns `{"position": int, "delta_km": float, "delta_minutes": float}`
        or None if the driver has no active deliveries, the new delivery has
        no usable coordinates, or the route can't be evaluated.
        """
        active = self._load_active_deliveries(driver_id)
        if not active:
            return None

        # Without a driver location we can't measure detour from anywhere
        # meaningful — skip the driver rather than pretend a city-centre
        # detour estimate is reliable.
        if self._location_status(driver_id) == "missing":
            return None

        new_delivery = Delivery.query.get(new_delivery_id)
        if not new_delivery:
            return None
        self._geocode_missing_addresses([new_delivery] + active)

        new_point = self._delivery_point(new_delivery)
        if new_point is None:
            return None
        active = [d for d in active if self._delivery_point(d) is not None]
        if not active:
            return None

        start_point, _ = self._resolve_start_point(driver_id, active)
        existing_points = [self._delivery_point(d) for d in active]

        # Use the driver's persisted optimized order if it covers exactly these
        # deliveries; otherwise compute a fresh order on the fly.
        ordered_points = self._ordered_existing_points(driver_id, active, existing_points)
        baseline_route = [start_point] + ordered_points

        matrix_baseline, _ = self.maps.get_distance_matrix(baseline_route, traffic=traffic)
        baseline_km = self._sum_path_km(matrix_baseline, list(range(len(baseline_route))))

        # Try inserting `new_point` at every position 1..len(baseline_route).
        # Position 1 = visit new stop first, len = visit it last.
        candidate_points = baseline_route + [new_point]
        new_idx = len(baseline_route)
        matrix_with, _ = self.maps.get_distance_matrix(candidate_points, traffic=traffic)

        best_position: Optional[int] = None
        best_delta_km: Optional[float] = None
        best_delta_min: Optional[float] = None

        # Loop invariants — compute once. Both baselines are taken from
        # `matrix_baseline` so the deltas are measured against a single,
        # consistent matrix snapshot (not the cell-rounded `matrix_with`).
        baseline_min = self._sum_path_minutes(matrix_baseline, list(range(len(baseline_route))))
        for pos in range(1, len(baseline_route) + 1):
            seq = list(range(len(baseline_route)))
            seq.insert(pos, new_idx)
            km = self._sum_path_km(matrix_with, seq)
            mins = self._sum_path_minutes(matrix_with, seq)
            delta_km = km - baseline_km
            delta_min = mins - baseline_min
            if best_delta_km is None or delta_km < best_delta_km:
                best_position = pos
                best_delta_km = delta_km
                best_delta_min = delta_min

        if best_position is None:
            return None
        return {
            "position": best_position,
            "delta_km": best_delta_km,
            "delta_minutes": best_delta_min,
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

        # Fall back to Tashkent center constant
        return (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"]), "tashkent_default"

    def _ordered_existing_points(
        self,
        driver_id: int,
        deliveries: List[Delivery],
        points: List[Point],
    ) -> List[Point]:
        route = (
            DeliveryRoute.query.filter_by(delivery_person_id=driver_id)
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
        if not route or not route.optimized_order:
            return points
        ids = [d.id for d in deliveries]
        if set(route.optimized_order) != set(ids):
            return points
        by_id = {d.id: pt for d, pt in zip(deliveries, points)}
        return [by_id[did] for did in route.optimized_order]

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
        return cls._sum_path_km(matrix, path), cls._sum_path_minutes(matrix, path)

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
        route = (
            DeliveryRoute.query.filter_by(delivery_person_id=int(driver_id))
            .order_by(DeliveryRoute.created_at.desc())
            .first()
        )
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
                    matrix, _ = self.maps.get_distance_matrix([start_point, (dest_lat, dest_lng)], traffic=True)
                    top["eta_minutes_from_current_location"] = round(matrix[(0, 1)]["duration_minutes"])
                    top["distance_km_to_next"] = round(matrix[(0, 1)]["distance_km"], 1)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("next-leg ETA failed driver=%s: %s", driver_id, exc)
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
