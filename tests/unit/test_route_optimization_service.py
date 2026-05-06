"""Unit tests for RouteOptimizationService.

Covers the TSP solver, insertion-cost calculator, persistence to
DeliveryRoute, start-point fallback hierarchy, and the Haversine fallback
when external matrix calls fail.

The autouse `block_external_side_effects` fixture from conftest.py prevents
real outbound HTTP, so all tests substitute the matrix via monkeypatch on
`MapsService.get_distance_matrix`.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import (
    Delivery,
    DeliveryPerson,
    DeliveryRoute,
    DeliveryStatusHistory,
)
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_user(db):
    user = User(
        email="route-driver@example.com",
        phone="+998901112233",
        password_hash="x",
        first_name="Route",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer_user(db):
    user = User(
        email="route-customer@example.com",
        phone="+998907654321",
        password_hash="x",
        first_name="Cust",
        last_name="Omer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver_with_live_location(db, driver_user):
    person = DeliveryPerson(
        user_id=driver_user.id,
        full_name="Route Driver",
        phone="+998901112233",
        current_location_lat=41.3000,
        current_location_lng=69.2500,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return person


def _make_address(db, user_id, lat, lng, label="Stop"):
    addr = UserAddress(
        user_id=user_id,
        title=label,
        full_address=f"{label} address",
        street_address=label,
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    return addr


def _make_delivery(db, customer_id, driver_id, address, status=DeliveryStatus.ASSIGNED, order_no=None):
    order = Order(
        user_id=customer_id,
        order_number=order_no or f"ORD-{address.id}",
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        delivery_fee=Decimal("0"),
        total_amount=Decimal("10000"),
        delivery_address_id=address.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
        delivery_time_slot="09:00-12:00",
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _matrix_from_points(points):
    """Build a Haversine-style matrix dict for the given (lat, lng) points."""
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
            else:
                km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix


# ---------------------------------------------------------------------------
# TSP solver
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestTSPSolver:
    """Pure-function tests for `_solve_tsp` — no DB, no network."""

    def test_solver_visits_every_node_exactly_once(self):
        # Build a clean 4-node matrix and assert the result is a valid permutation.
        points = [(0, 0), (0, 1), (1, 0), (1, 1)]
        matrix = _matrix_from_points(points)
        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)

        assert order[0] == 0, "must start from start_idx"
        assert sorted(order) == [0, 1, 2, 3], "every node visited exactly once"

    def test_solver_returns_singleton_for_single_node(self):
        matrix = {(0, 0): {"distance_km": 0, "duration_minutes": 0}}
        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)
        assert order == [0]

    def test_solver_returns_empty_for_empty_matrix(self):
        assert RouteOptimizationService._solve_tsp({}, start_idx=0) == []

    def test_solver_picks_close_neighbor_first(self):
        # 0 sits at origin; node 1 is right next door, node 2 is far away.
        # Open-path TSP must visit 1 before 2 to avoid a long detour.
        matrix = {
            (0, 0): {"distance_km": 0, "duration_minutes": 0},
            (0, 1): {"distance_km": 1, "duration_minutes": 1},
            (0, 2): {"distance_km": 10, "duration_minutes": 10},
            (1, 0): {"distance_km": 1, "duration_minutes": 1},
            (1, 1): {"distance_km": 0, "duration_minutes": 0},
            (1, 2): {"distance_km": 9, "duration_minutes": 9},
            (2, 0): {"distance_km": 10, "duration_minutes": 10},
            (2, 1): {"distance_km": 9, "duration_minutes": 9},
            (2, 2): {"distance_km": 0, "duration_minutes": 0},
        }
        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)
        assert order == [0, 1, 2]

    def test_solver_breaks_clear_zigzag_with_2opt(self):
        # NN seed produces 0 → 1 → 2 → 3 (zigzag), 2-opt should reorder to
        # 0 → 1 → 3 → 2 which has lower cost.
        matrix = {
            (0, 0): {"distance_km": 0, "duration_minutes": 0},
            (0, 1): {"distance_km": 1, "duration_minutes": 1},
            (0, 2): {"distance_km": 2, "duration_minutes": 2},
            (0, 3): {"distance_km": 3, "duration_minutes": 3},
            (1, 0): {"distance_km": 1, "duration_minutes": 1},
            (1, 1): {"distance_km": 0, "duration_minutes": 0},
            (1, 2): {"distance_km": 10, "duration_minutes": 10},
            (1, 3): {"distance_km": 1.5, "duration_minutes": 1.5},
            (2, 0): {"distance_km": 2, "duration_minutes": 2},
            (2, 1): {"distance_km": 10, "duration_minutes": 10},
            (2, 2): {"distance_km": 0, "duration_minutes": 0},
            (2, 3): {"distance_km": 1, "duration_minutes": 1},
            (3, 0): {"distance_km": 3, "duration_minutes": 3},
            (3, 1): {"distance_km": 1.5, "duration_minutes": 1.5},
            (3, 2): {"distance_km": 1, "duration_minutes": 1},
            (3, 3): {"distance_km": 0, "duration_minutes": 0},
        }
        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)
        # Both 0,1,3,2 (cost 3.5) and 0,2,3,1 (cost 4.5) are local optima for NN+2opt;
        # we just verify the solver beats the naïve NN seed (0,1,2,3 cost 12.5).
        path_cost = sum(matrix[(order[i], order[i + 1])]["duration_minutes"] for i in range(len(order) - 1))
        assert path_cost < 12.5, f"2-opt should improve over NN baseline; got {path_cost}"
        assert order[0] == 0

    def test_2opt_unlocks_last_position(self):
        # Asymmetric counterexample where greedy NN puts the wrong delivery
        # in the last slot and the *closed-tour* 2-opt bound (the old bug)
        # cannot reach the optimum.
        #
        # Real road networks are asymmetric (one-way streets, turn
        # restrictions, traffic direction) so this is a realistic shape.
        # Nodes 0=O, 1=A, 2=B, 3=C. Costs in matrix below.
        # Greedy NN: O->A (1) -> B (1) -> C (100)            total 102
        # Old 2-opt (k < len-1) reaches:  O->B->A->C          total 61
        #   (only swap available is i=1, k=2 — never includes index 3)
        # True optimum: O->C->B->A  cost 50 + 1 + 1 =          52
        #   (or equivalently O->A->C->B->... — but len=4 so this is the
        #   complete optimum). Reaching it requires reversing the segment
        #   [1:4] (k=3=len-1), which the corrected bound now allows.
        m = {}
        # diagonals
        for i in range(4):
            m[(i, i)] = {"distance_km": 0.0, "duration_minutes": 0.0}
        # Costs (asymmetric where the trick lives — note (2,3) vs (3,2)).
        m[(0, 1)] = m[(1, 0)] = {"distance_km": 1.0, "duration_minutes": 1.0}
        m[(0, 2)] = m[(2, 0)] = {"distance_km": 10.0, "duration_minutes": 10.0}
        m[(0, 3)] = m[(3, 0)] = {"distance_km": 50.0, "duration_minutes": 50.0}
        m[(1, 2)] = m[(2, 1)] = {"distance_km": 1.0, "duration_minutes": 1.0}
        m[(1, 3)] = m[(3, 1)] = {"distance_km": 50.0, "duration_minutes": 50.0}
        m[(2, 3)] = {"distance_km": 100.0, "duration_minutes": 100.0}  # B -> C: long
        m[(3, 2)] = {"distance_km": 1.0, "duration_minutes": 1.0}  # C -> B: short

        order = RouteOptimizationService._solve_tsp_heuristic(matrix=m, start_idx=0)
        path_cost = sum(
            m[(order[i], order[i + 1])]["duration_minutes"]
            for i in range(len(order) - 1)
        )
        # Old bound would terminate at cost 61. New bound reaches 52.
        assert path_cost <= 52.0 + 1e-9, (
            f"corrected 2-opt should reach optimum 52; got order={order} "
            f"cost={path_cost}"
        )

    def test_heldkarp_matches_brute_force_for_small_n(self):
        # For N=5 deliveries the brute-force optimum is trivial to compute
        # via permutations. Held-Karp dispatch must produce the same cost.
        import itertools
        import random

        rng = random.Random(20260506)
        # Spread points around Tashkent so distances are non-degenerate.
        points = [(41.30, 69.25)] + [
            (41.30 + rng.uniform(-0.05, 0.05), 69.25 + rng.uniform(-0.05, 0.05))
            for _ in range(5)
        ]
        matrix = _matrix_from_points(points)

        def path_cost(p):
            return sum(matrix[(p[i], p[i + 1])]["duration_minutes"] for i in range(len(p) - 1))

        brute_best = min(
            (list((0,) + perm) for perm in itertools.permutations(range(1, len(points)))),
            key=path_cost,
        )
        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)
        assert order[0] == 0
        assert sorted(order) == list(range(len(points)))
        assert path_cost(order) == pytest.approx(path_cost(brute_best), rel=1e-9), (
            f"Held-Karp should match brute force; got {order} cost={path_cost(order)} "
            f"vs brute {brute_best} cost={path_cost(brute_best)}"
        )

    def test_heuristic_path_above_threshold_runs(self):
        # n = HELDKARP_MAX_DELIVERIES + 2 (i.e. start + 13 deliveries) routes
        # through the NN+2-opt branch. We just assert the result is a valid
        # permutation that visits every node exactly once.
        import random

        from business_app.services.route_optimization_service import HELDKARP_MAX_DELIVERIES

        rng = random.Random(20260506)
        n_deliveries = HELDKARP_MAX_DELIVERIES + 1  # one above the threshold
        points = [(41.30, 69.25)] + [
            (41.30 + rng.uniform(-0.05, 0.05), 69.25 + rng.uniform(-0.05, 0.05))
            for _ in range(n_deliveries)
        ]
        matrix = _matrix_from_points(points)

        order = RouteOptimizationService._solve_tsp(matrix, start_idx=0)
        assert order[0] == 0
        assert len(order) == len(points)
        assert sorted(order) == list(range(len(points))), "all indices visited exactly once"


# ---------------------------------------------------------------------------
# Start-point fallback hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestStartPointResolution:
    def test_uses_driver_live_location_when_fresh(
        self, app, db, driver_user, driver_with_live_location, customer_user
    ):
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.32, 69.28)
            delivery = _make_delivery(db, customer_user.id, driver_user.id, addr)

            svc = RouteOptimizationService()
            point, source = svc._resolve_start_point(driver_user.id, [delivery])

            assert source == "driver_live"
            assert point == (
                driver_with_live_location.current_location_lat,
                driver_with_live_location.current_location_lng,
            )

    def test_falls_back_to_last_completed_when_location_stale(
        self, app, db, driver_user, customer_user
    ):
        with app.app_context():
            # Stale driver person (location updated 2h ago, threshold is 30 min).
            stale_person = DeliveryPerson(
                user_id=driver_user.id,
                full_name="x",
                phone="x",
                current_location_lat=41.30,
                current_location_lng=69.25,
                last_location_update=datetime.now(UTC) - timedelta(hours=2),
            )
            db.session.add(stale_person)

            addr = _make_address(db, customer_user.id, 41.32, 69.28)
            delivery = _make_delivery(db, customer_user.id, driver_user.id, addr)

            # A completed delivery earlier today with a recorded GPS fix.
            history = DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=DeliveryStatus.IN_TRANSIT,
                new_status=DeliveryStatus.DELIVERED,
                changed_by=driver_user.id,
                changed_at=datetime.now(UTC) - timedelta(minutes=45),
                location_lat=41.4001,
                location_lng=69.5001,
            )
            db.session.add(history)
            db.session.commit()

            svc = RouteOptimizationService()
            point, source = svc._resolve_start_point(driver_user.id, [delivery])

            assert source == "last_completed"
            assert point == (41.4001, 69.5001)

    def test_falls_back_to_tashkent_default_when_nothing_else(
        self, app, db, driver_user, customer_user
    ):
        with app.app_context():
            from shared.constants import TASHKENT_COORDINATES

            addr = _make_address(db, customer_user.id, 41.32, 69.28)
            delivery = _make_delivery(db, customer_user.id, driver_user.id, addr)

            svc = RouteOptimizationService()
            point, source = svc._resolve_start_point(driver_user.id, [delivery])

            assert source == "tashkent_default"
            assert point == (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"])


# ---------------------------------------------------------------------------
# optimize_for_driver — orchestration + persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestOptimizeForDriver:
    def test_returns_none_when_driver_has_no_active_deliveries(self, app, db, driver_user):
        with app.app_context():
            svc = RouteOptimizationService()
            assert svc.optimize_for_driver(driver_user.id) is None

    def test_returns_none_when_driver_has_no_shared_location(
        self, app, db, driver_user, customer_user, monkeypatch
    ):
        """Driver-current-location is a hard precondition. Even with active
        deliveries, optimization must NOT silently fall back to a
        depot/city-centre origin and produce a misleading sequence — it
        must skip with `None` so the caller can surface the prompt."""
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.310, 69.270)
            _make_delivery(db, customer_user.id, driver_user.id, addr)
            # Note: no DeliveryPerson row for this driver -> location_status
            # is "missing".

            matrix_calls = []
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: matrix_calls.append(points)
                or (_matrix_from_points(points), "haversine"),
            )

            svc = RouteOptimizationService()
            assert svc.optimize_for_driver(driver_user.id) is None
            assert matrix_calls == [], (
                "Optimizer must not call the matrix API when location is missing"
            )

    def test_optimizes_when_location_is_stale_but_present(
        self, app, db, driver_user, customer_user, monkeypatch
    ):
        """Stale location is still a valid start point — the user said
        one-time share is sufficient and we don't want to nag drivers who
        haven't refreshed in a while. Only `missing` blocks optimization."""
        with app.app_context():
            from datetime import timedelta
            from business_app.models.delivery import DeliveryPerson

            person = DeliveryPerson(
                user_id=driver_user.id,
                full_name="x",
                phone="x",
                current_location_lat=41.300,
                current_location_lng=69.250,
                last_location_update=datetime.now(UTC) - timedelta(hours=2),
            )
            db.session.add(person)
            addr = _make_address(db, customer_user.id, 41.310, 69.270)
            _make_delivery(db, customer_user.id, driver_user.id, addr)

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: (
                    _matrix_from_points(points),
                    "haversine",
                ),
            )

            svc = RouteOptimizationService()
            route = svc.optimize_for_driver(driver_user.id)
            assert route is not None
            assert len(route.optimized_order) == 1

    def test_persists_route_with_optimized_order(
        self, app, db, driver_user, driver_with_live_location, customer_user, monkeypatch
    ):
        with app.app_context():
            # Three deliveries; addresses on a roughly east-west line so the
            # Haversine ordering is unambiguous when starting from the driver.
            addr_close = _make_address(db, customer_user.id, 41.300, 69.260, "close")  # ~0.8 km
            addr_mid = _make_address(db, customer_user.id, 41.300, 69.290, "mid")      # ~3.3 km
            addr_far = _make_address(db, customer_user.id, 41.300, 69.330, "far")      # ~6.7 km
            d1 = _make_delivery(db, customer_user.id, driver_user.id, addr_far, order_no="ORD-far")
            d2 = _make_delivery(db, customer_user.id, driver_user.id, addr_close, order_no="ORD-close")
            d3 = _make_delivery(db, customer_user.id, driver_user.id, addr_mid, order_no="ORD-mid")

            captured = {}

            def fake_matrix(self, points, traffic=True, use_cache=True):
                captured["points"] = list(points)
                return _matrix_from_points(points), "haversine"

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                fake_matrix,
            )

            svc = RouteOptimizationService()
            route = svc.optimize_for_driver(driver_user.id, trigger="accept")

            assert route is not None
            assert route.delivery_person_id == driver_user.id
            # Optimal closest-first order: close, mid, far.
            assert route.optimized_order == [d2.id, d3.id, d1.id]
            assert route.total_distance_km > 0
            assert route.estimated_duration_minutes is not None
            assert route.extra_data.get("trigger") == "accept"
            assert route.extra_data.get("matrix_source") == "haversine"
            assert "last_optimized_at" in route.extra_data

            # Service called the matrix with N+1 points (start + 3 stops).
            assert len(captured["points"]) == 4

    def test_upserts_today_route_on_repeat_call(
        self, app, db, driver_user, driver_with_live_location, customer_user, monkeypatch
    ):
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.310, 69.270)
            _make_delivery(db, customer_user.id, driver_user.id, addr)

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: (
                    _matrix_from_points(points),
                    "haversine",
                ),
            )

            svc = RouteOptimizationService()
            r1 = svc.optimize_for_driver(driver_user.id, trigger="accept")
            r2 = svc.optimize_for_driver(driver_user.id, trigger="manual")

            # Same row reused; trigger updated.
            assert r1.id == r2.id
            assert r2.extra_data.get("trigger") == "manual"

    def test_falls_back_to_haversine_when_matrix_provider_returns_haversine(
        self, app, db, driver_user, driver_with_live_location, customer_user, monkeypatch
    ):
        """When the matrix provider couldn't reach Yandex it returns 'haversine';
        the persisted route's extra_data flags `fallback=True` so ops can spot it."""
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.310, 69.270)
            _make_delivery(db, customer_user.id, driver_user.id, addr)

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: (
                    _matrix_from_points(points),
                    "haversine",
                ),
            )

            svc = RouteOptimizationService()
            route = svc.optimize_for_driver(driver_user.id)

            assert route.extra_data.get("fallback") is True

    def test_skips_deliveries_without_geocoded_address(
        self, app, db, driver_user, driver_with_live_location, customer_user, monkeypatch
    ):
        """A delivery whose address has no lat/lng is excluded from optimization
        rather than crashing the run. Geocoding fallback path: we deliberately
        block geocoding (raises) so the address stays without coords and the
        delivery is filtered out."""
        with app.app_context():
            good_addr = _make_address(db, customer_user.id, 41.310, 69.270, "good")
            bad_addr = UserAddress(
                user_id=customer_user.id,
                title="bad",
                full_address="no-coords",
                street_address="no-coords",
                latitude=None,
                longitude=None,
            )
            db.session.add(bad_addr)
            db.session.flush()

            d_good = _make_delivery(db, customer_user.id, driver_user.id, good_addr, order_no="GOOD")
            d_bad = _make_delivery(db, customer_user.id, driver_user.id, bad_addr, order_no="BAD")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: (
                    _matrix_from_points(points),
                    "haversine",
                ),
            )
            # Force geocoding to fail so the bad address stays uncoordinated.
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.geocode_address",
                lambda self, address, city="Tashkent": (_ for _ in ()).throw(
                    RuntimeError("geocode disabled in test")
                ),
            )

            svc = RouteOptimizationService()
            route = svc.optimize_for_driver(driver_user.id)

            assert route is not None
            assert route.optimized_order == [d_good.id]
            assert d_bad.id not in route.optimized_order


# ---------------------------------------------------------------------------
# compute_insertion_cost
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestInsertionCost:
    def test_returns_none_when_driver_has_no_shared_location(
        self, app, db, driver_user, customer_user
    ):
        """Without a real driver location we can't measure detour from
        anywhere meaningful — the candidate driver must be skipped rather
        than evaluated against a city-centre fallback."""
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.31, 69.27)
            _make_delivery(db, customer_user.id, driver_user.id, addr)
            # No DeliveryPerson row -> location is "missing".

            pool_addr = _make_address(db, customer_user.id, 41.32, 69.28)
            pool_order = Order(
                user_id=customer_user.id,
                order_number="POOL-NO-LOC",
                status=OrderStatus.CONFIRMED,
                subtotal=Decimal("0"),
                total_amount=Decimal("0"),
                delivery_address_id=pool_addr.id,
            )
            db.session.add(pool_order)
            db.session.flush()
            pool_delivery = Delivery(
                order_id=pool_order.id,
                status=DeliveryStatus.SCHEDULED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(pool_delivery)
            db.session.commit()

            svc = RouteOptimizationService()
            assert svc.compute_insertion_cost(driver_user.id, pool_delivery.id) is None

    def test_returns_none_when_driver_has_no_active_deliveries(self, app, db, driver_user, customer_user):
        with app.app_context():
            addr = _make_address(db, customer_user.id, 41.30, 69.27)
            order = Order(
                user_id=customer_user.id,
                order_number="POOL-001",
                status=OrderStatus.CONFIRMED,
                subtotal=Decimal("0"),
                total_amount=Decimal("0"),
                delivery_address_id=addr.id,
            )
            db.session.add(order)
            db.session.flush()
            pool_delivery = Delivery(
                order_id=order.id,
                status=DeliveryStatus.SCHEDULED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(pool_delivery)
            db.session.commit()

            svc = RouteOptimizationService()
            assert svc.compute_insertion_cost(driver_user.id, pool_delivery.id) is None

    def test_picks_cheapest_insertion_position(
        self, app, db, driver_user, driver_with_live_location, customer_user, monkeypatch
    ):
        """Driver's existing route runs east. A pool stop close to the driver's
        current location should slot in at position 1 (visit it first)."""
        with app.app_context():
            far_addr = _make_address(db, customer_user.id, 41.300, 69.330, "far")  # existing stop
            close_addr = _make_address(db, customer_user.id, 41.301, 69.255, "close-to-driver")
            existing = _make_delivery(db, customer_user.id, driver_user.id, far_addr)

            # New pool delivery, NOT yet assigned.
            pool_order = Order(
                user_id=customer_user.id,
                order_number="POOL-INSERT",
                status=OrderStatus.CONFIRMED,
                subtotal=Decimal("0"),
                total_amount=Decimal("0"),
                delivery_address_id=close_addr.id,
            )
            db.session.add(pool_order)
            db.session.flush()
            pool_delivery = Delivery(
                order_id=pool_order.id,
                status=DeliveryStatus.SCHEDULED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(pool_delivery)
            db.session.commit()

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                lambda self, points, traffic=True, use_cache=True: (
                    _matrix_from_points(points),
                    "haversine",
                ),
            )

            svc = RouteOptimizationService()
            cost = svc.compute_insertion_cost(driver_user.id, pool_delivery.id)

            assert cost is not None
            # The new stop is closer to the driver than the existing far stop, so
            # the optimal insert is position 1 (visit it first).
            assert cost["position"] == 1
            assert cost["delta_km"] >= 0
            assert cost["delta_minutes"] >= 0


# ---------------------------------------------------------------------------
# annotate_active_items
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestAnnotateActiveItems:
    def test_sorts_items_by_persisted_optimized_order_with_fresh_location(
        self, app, db, driver_user, driver_with_live_location
    ):
        with app.app_context():
            route = DeliveryRoute(
                name="t",
                delivery_person_id=driver_user.id,
                start_location_lat=41.3,
                start_location_lng=69.25,
                route_date=datetime.now(UTC),
                optimized_order=[33, 11, 22],
                status="planned",
            )
            db.session.add(route)
            db.session.commit()

            items = [
                {"delivery_id": 11, "destination_latitude": 41.31, "destination_longitude": 69.26},
                {"delivery_id": 22, "destination_latitude": 41.32, "destination_longitude": 69.27},
                {"delivery_id": 33, "destination_latitude": 41.33, "destination_longitude": 69.28},
            ]

            svc = RouteOptimizationService()
            # Skip the next-leg ETA call by stubbing the matrix to a no-op.
            with patch.object(
                svc.maps,
                "get_distance_matrix",
                return_value=({(0, 1): {"distance_km": 1.5, "duration_minutes": 4}}, "haversine"),
            ):
                out = svc.annotate_active_items(driver_user.id, items)

            assert [it["delivery_id"] for it in out] == [33, 11, 22]
            assert out[0]["is_next"] is True
            assert out[0]["route_position"] == 0
            assert out[1]["is_next"] is False
            assert out[1]["route_position"] == 1
            # ETA only populated when location is fresh (driver_with_live_location).
            assert out[0]["distance_km_to_next"] == 1.5
            assert out[0]["eta_minutes_from_current_location"] == 4
            assert out[1]["distance_km_to_next"] is None

    def test_suppresses_eta_when_driver_location_is_missing(self, app, db, driver_user):
        """When the driver has no fresh GPS fix, the resolved start point is
        the depot/city-centre fallback and the resulting ETA would be
        misleading. The annotation skips the matrix call entirely and leaves
        the ETA fields as None so the bot can render the right UX."""
        with app.app_context():
            route = DeliveryRoute(
                name="t",
                delivery_person_id=driver_user.id,
                start_location_lat=41.3,
                start_location_lng=69.25,
                route_date=datetime.now(UTC),
                optimized_order=[1],
                status="planned",
            )
            db.session.add(route)
            db.session.commit()

            items = [
                {"delivery_id": 1, "destination_latitude": 41.31, "destination_longitude": 69.26},
            ]
            svc = RouteOptimizationService()
            matrix_calls = []
            with patch.object(
                svc.maps,
                "get_distance_matrix",
                side_effect=lambda *a, **kw: matrix_calls.append((a, kw))
                or ({(0, 1): {"distance_km": 9, "duration_minutes": 9}}, "haversine"),
            ):
                out = svc.annotate_active_items(driver_user.id, items)

            assert out[0]["is_next"] is True
            assert out[0]["distance_km_to_next"] is None
            assert out[0]["eta_minutes_from_current_location"] is None
            assert matrix_calls == [], "should skip the matrix call when location is missing"

    def test_handles_missing_route_gracefully(self, app, db, driver_user, driver_with_live_location):
        with app.app_context():
            items = [
                {"delivery_id": 1, "destination_latitude": 41.3, "destination_longitude": 69.25},
                {"delivery_id": 2, "destination_latitude": 41.31, "destination_longitude": 69.26},
            ]
            svc = RouteOptimizationService()
            with patch.object(
                svc.maps,
                "get_distance_matrix",
                return_value=({(0, 1): {"distance_km": 1.0, "duration_minutes": 3}}, "haversine"),
            ):
                out = svc.annotate_active_items(driver_user.id, items)

            # No persisted route → stable id-asc order, route_position is None.
            assert [it["delivery_id"] for it in out] == [1, 2]
            assert out[0]["route_position"] is None
            assert out[0]["is_next"] is True


# ---------------------------------------------------------------------------
# location_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestLocationStatus:
    def test_missing_when_no_delivery_person(self, app, db, driver_user):
        with app.app_context():
            assert RouteOptimizationService().location_status(driver_user.id) == "missing"

    def test_fresh_when_recent(self, app, db, driver_user, driver_with_live_location):
        with app.app_context():
            assert RouteOptimizationService().location_status(driver_user.id) == "fresh"

    def test_stale_when_old(self, app, db, driver_user):
        with app.app_context():
            person = DeliveryPerson(
                user_id=driver_user.id,
                full_name="x",
                phone="x",
                current_location_lat=41.3,
                current_location_lng=69.25,
                last_location_update=datetime.now(UTC) - timedelta(hours=2),
            )
            db.session.add(person)
            db.session.commit()
            assert RouteOptimizationService().location_status(driver_user.id) == "stale"
