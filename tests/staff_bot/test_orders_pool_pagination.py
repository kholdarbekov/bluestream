"""Pagination-state tests for the driver "New Orders" pool (RC-E / RC-G).

Bug: ``pool_page`` in ``context.user_data`` was set on a pagination tap and
never reset, so a driver who once paged to page 2 stayed pinned there — every
later open of "New Orders" refetched page 2. When the pool held <= per_page
items (all on page 1) page 2 was empty, so that driver saw an empty list while
another driver on page 1 saw the order. A stale page beyond the current range
also had no recovery path back to page 1.

Fix: fresh menu entry resets the page to 1; an out-of-range page is clamped
into the valid range.
"""

import pytest

from staff_bot.handlers.delivery.orders_pool import OrdersPoolHandler


@pytest.mark.unit
class TestEffectivePage:
    def test_reset_forces_page_1_over_stale_value(self):
        """Opening the menu fresh must start at page 1 regardless of a
        left-over pool_page from an earlier pagination tap."""
        user_data = {"pool_page": 2}
        assert OrdersPoolHandler._effective_page(user_data, reset=True) == 1
        assert user_data["pool_page"] == 1

    def test_no_reset_keeps_chosen_page(self):
        """A pagination tap (reset=False) must honour the page the driver chose."""
        user_data = {"pool_page": 3}
        assert OrdersPoolHandler._effective_page(user_data, reset=False) == 3

    def test_default_is_page_1(self):
        user_data = {}
        assert OrdersPoolHandler._effective_page(user_data, reset=False) == 1

    def test_invalid_stored_page_falls_back_to_1(self):
        user_data = {"pool_page": 0}
        assert OrdersPoolHandler._effective_page(user_data, reset=False) == 1


@pytest.mark.unit
class TestClampPage:
    def test_page_beyond_range_clamps_down(self):
        """A stale 'page 2' button after the pool shrank to one page must
        clamp back to page 1 instead of showing an empty, dead-end list."""
        user_data = {"pool_page": 2}
        assert OrdersPoolHandler._clamp_page(user_data, total_pages=1) == 1
        assert user_data["pool_page"] == 1

    def test_page_in_range_unchanged(self):
        user_data = {"pool_page": 2}
        assert OrdersPoolHandler._clamp_page(user_data, total_pages=3) == 2
        assert user_data["pool_page"] == 2

    def test_empty_pool_clamps_to_page_1(self):
        user_data = {"pool_page": 5}
        assert OrdersPoolHandler._clamp_page(user_data, total_pages=0) == 1
        assert user_data["pool_page"] == 1
