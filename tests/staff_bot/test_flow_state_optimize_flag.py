"""Spec §4.2: the 412 fallback arms a flag so the pin that follows optimizes
without a second tap. It must clear through the same SSOT as every other
pending flow — a menu tap in between has to leave no armed remnant behind."""

import asyncio
from unittest.mock import MagicMock

import pytest

from staff_bot.utils import flow_state


@pytest.mark.unit
def test_optimize_flag_is_a_registered_pending_flow():
    assert "pending_optimize_after_location" in flow_state.PENDING_FLOW_USER_DATA_KEYS


@pytest.mark.unit
def test_clear_pending_flows_drops_the_optimize_flag():
    ctx = MagicMock()
    ctx.user_data = {"pending_optimize_after_location": True}

    asyncio.run(flow_state.clear_pending_flows(ctx, None))

    assert "pending_optimize_after_location" not in ctx.user_data
