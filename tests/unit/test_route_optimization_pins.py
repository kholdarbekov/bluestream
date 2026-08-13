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


class TestReturnColumnNeverCosted:
    """`_solve_with_pins` copies `matrix[(free_node, start_idx)]` cells (the
    stop->origin "return" edges) into its local sub-matrix — see the
    `local_matrix` construction — but no consumer ever COSTS them: the
    open-path solvers never produce a `(v, start_idx)` edge in the first
    place. The TRUE reason (final review round, residual 1 — the previous
    wording blamed "`_two_opt_frozen`'s frozen index-0 prefix", which is
    wrong: `frozen_positions` never contains index 0 in the first place,
    because it only ever holds positions >= 1, see `_solve_with_pins`'s
    `frozen = {i + 1 for ...}`): `_two_opt_frozen` iterates
    `range(1, len(best) - 1)` for its reversal start `i`, so index 0 can
    never sit inside a reversed slice and `path[0]` stays `start_idx`
    forever — no `(v, 0)` edge is ever produced to look up. Likewise
    `_solve_tsp_exact`'s Held-Karp DP and `_solve_tsp_heuristic`'s NN+2-opt
    are both explicitly open-ended (no return edge), by construction, not by
    the frozen set.

    This is the exact invariant `distance_matrix.py::_fetch_origin_row_col`
    relies on to justify mirroring `col` from `row` on the partial-fetch
    cache path instead of paying for a second HTTP round-trip (see that
    function's docstring, and `_store_split_cache` where the full-fetch path
    stores the real column).

    Poison must be a SINGLE, ASYMMETRIC cell, not a uniform value across
    every `(i, 0)`. A uniform poison (the original, vacuous version of this
    test) adds the identical constant to every candidate's return edge under
    a hypothetical closed-tour objective too — so the ranking between
    candidates never changes and the assertion would hold even after such a
    regression, making the tripwire fire on nothing. Poisoning exactly one
    cell breaks that symmetry: it changes only ONE candidate tour's
    hypothetical closing cost, so a solver that started costing the return
    leg would flip the winner and this test would genuinely fail. Verified
    empirically for this fixture: clean closed-tour costs tie at
    24.0/24.0 for the two free-node orderings; poisoning only `(3, 0)` moves
    one of them to 1,000,011.0, breaking the tie — proof this poison is
    capable of tripping the invariant it guards, unlike a uniform one.
    """

    def test_poisoning_a_single_return_to_origin_cell_does_not_change_the_solve(self):
        matrix = line_matrix([0, 7, 3, 12, 5])
        pinned_positions = {1: 0, 4: 2}
        baseline = RouteOptimizationService._solve_with_pins(matrix, dict(pinned_positions))

        poisoned = dict(matrix)
        # Asymmetric: ONLY node 3's return-to-origin cell. Node 2's stays
        # clean, so a closed-tour objective would see the two free-node
        # orderings (which visit 2 and 3 in opposite positions) diverge —
        # see the class docstring for the empirical proof.
        poisoned[(3, 0)] = {"duration_minutes": 999_999.0, "distance_km": 999_999.0}
        poisoned_result = RouteOptimizationService._solve_with_pins(poisoned, dict(pinned_positions))

        assert poisoned_result == baseline


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
