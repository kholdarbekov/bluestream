"""Pin-constrained solving.

Position semantics: `pinned_positions` maps a matrix index to its 0-based slot
among the DELIVERY nodes (start excluded). `_solve_with_pins` returns a full
path whose element 0 is start_idx, so a stop pinned to position 0 appears at
path[1].
"""

import pytest

from business_app.services.route_optimization_service import RouteOptimizationService


def line_matrix(coords):
    """Symmetric matrix over 1-D positions; duration == |dx|, distance == |dx|."""
    m = {}
    for i, a in enumerate(coords):
        for j, b in enumerate(coords):
            if i == j:
                continue
            d = abs(a - b)
            m[(i, j)] = {"duration_minutes": float(d), "distance_km": float(d)}
    return m


class TestSolveWithPins:
    def test_no_pins_is_identical_to_solve_tsp(self):
        matrix = line_matrix([0, 7, 3, 12, 5])
        assert RouteOptimizationService._solve_with_pins(matrix, {}) == \
            RouteOptimizationService._solve_tsp(matrix, start_idx=0)

    def test_pinned_stop_holds_first_slot(self):
        # Start at 0; nearest is node 2 (x=3). Pin node 3 (x=12) to slot 0.
        matrix = line_matrix([0, 7, 3, 12])
        path = RouteOptimizationService._solve_with_pins(matrix, {3: 0})
        assert path[0] == 0
        assert path[1] == 3
        assert sorted(path) == [0, 1, 2, 3]

    def test_pinned_stop_holds_last_slot(self):
        matrix = line_matrix([0, 7, 3, 12])
        path = RouteOptimizationService._solve_with_pins(matrix, {2: 2})
        assert path[0] == 0
        assert path[3] == 2

    def test_two_pins_both_hold_their_slots(self):
        matrix = line_matrix([0, 7, 3, 12, 5])
        path = RouteOptimizationService._solve_with_pins(matrix, {1: 0, 4: 2})
        assert path[1] == 1
        assert path[3] == 4
        assert sorted(path) == [0, 1, 2, 3, 4]

    def test_position_beyond_end_is_clamped(self):
        matrix = line_matrix([0, 7, 3])
        path = RouteOptimizationService._solve_with_pins(matrix, {1: 99})
        assert path[-1] == 1
        assert sorted(path) == [0, 1, 2]

    def test_all_stops_pinned_returns_exactly_that_order(self):
        matrix = line_matrix([0, 7, 3, 12])
        path = RouteOptimizationService._solve_with_pins(matrix, {1: 2, 2: 0, 3: 1})
        assert path == [0, 2, 3, 1]

    def test_single_node_matrix(self):
        assert RouteOptimizationService._solve_with_pins({}, {}) == []

    def test_pinned_key_is_start_idx_is_skipped(self):
        # If a pin tries to pin the start node, it is silently ignored
        # (start is never a delivery, so it has no slot). Path should still be valid.
        matrix = line_matrix([0, 7, 3, 12])
        path = RouteOptimizationService._solve_with_pins(matrix, {0: 1, 2: 0})
        assert path[0] == 0  # start_idx is still at the beginning
        assert sorted(path) == [0, 1, 2, 3]  # valid permutation; node 0 not duplicated

    def test_pinned_key_out_of_range_is_skipped(self):
        # If a pin references an invalid matrix index (negative or >= n),
        # it is silently ignored. Path should still be valid.
        matrix = line_matrix([0, 7, 3])
        path = RouteOptimizationService._solve_with_pins(matrix, {1: 0, 99: 1, -1: 2})
        assert sorted(path) == [0, 1, 2]  # valid permutation; invalid keys ignored
        assert path[0] == 0


class TestTwoOptFrozen:
    def test_improves_an_unfrozen_path(self):
        matrix = line_matrix([0, 12, 3, 7])
        improved = RouteOptimizationService._two_opt_frozen(matrix, [0, 1, 2, 3], frozen_positions=set())
        cost = sum(matrix[(improved[i], improved[i + 1])]["duration_minutes"] for i in range(len(improved) - 1))
        assert cost == pytest.approx(12.0)  # 0→3→7→12

    def test_never_moves_a_frozen_position(self):
        matrix = line_matrix([0, 12, 3, 7])
        improved = RouteOptimizationService._two_opt_frozen(matrix, [0, 1, 2, 3], frozen_positions={1})
        assert improved[1] == 1
