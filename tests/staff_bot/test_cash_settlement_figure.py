"""The driver collects the server's cash figure for an unpaid Click order (spec 2026-10-09 §6.3)."""

import pytest

from staff_bot.utils.formatters import get_cod_cash_projection


@pytest.mark.unit
def test_the_cash_settlement_figure_is_what_the_driver_collects():
    payload = {"expected_cash_to_collect": 33000.0, "cash_settlement_amount": 30000.0}
    assert get_cod_cash_projection(payload)["expected_cash_to_collect"] == 30000.0


@pytest.mark.unit
def test_without_a_cash_settlement_figure_the_receivable_is_collected():
    payload = {"expected_cash_to_collect": 33000.0, "cash_settlement_amount": None}
    assert get_cod_cash_projection(payload)["expected_cash_to_collect"] == 33000.0
