"""Regression tests for COD settlement clarity text in staff bot order cards."""

from staff_bot.handlers.delivery.status_update import StatusUpdateHandler
from staff_bot.utils.formatters import format_order_card, get_cod_cash_projection


def test_format_order_card_marks_cash_as_already_collected_when_settled():
    card = format_order_card(
        {
            "order_number": "ORD-COD-001",
            "payment_method": "cash",
            "payment_status": "completed",
            "total_amount": 90000,
            "outstanding_amount": 0,
            "expected_cash_to_collect": 0,
            "item_count": 1,
        },
        "en",
    )

    assert "Cash already collected" in card
    assert "Cash to collect now: 0" in card


def test_format_order_card_marks_cash_as_partially_collected():
    card = format_order_card(
        {
            "order_number": "ORD-COD-002",
            "payment_method": "cash",
            "payment_status": "partially_paid",
            "total_amount": 18000,
            "outstanding_amount": 13000,
            "expected_cash_to_collect": 13000,
            "item_count": 1,
        },
        "en",
    )

    assert "Cash partially collected" in card
    assert "Cash to collect now: 13,000" in card


def test_cod_projection_keeps_explicit_zero_without_falling_back_to_total_amount():
    projection = get_cod_cash_projection(
        {
            "expected_cash_to_collect": 0,
            "outstanding_amount": 0,
            "total_amount": 90000,
        }
    )

    assert projection["expected_cash_to_collect"] == 0


def test_status_update_expected_cash_uses_projection_zero():
    cash_due = StatusUpdateHandler._get_expected_cash_to_collect(
        {
            "expected_cash_to_collect": 0,
            "outstanding_amount": 0,
            "total_amount": 90000,
        }
    )

    assert cash_due == 0
