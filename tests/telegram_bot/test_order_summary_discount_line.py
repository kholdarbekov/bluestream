"""A total below the sum of the item lines must always say why."""

import pytest

from telegram_bot.utils import MessageBuilder


def _order(**overrides):
    order = {
        "order_number": "TG_000401_26",
        "created_at": "2026-09-03T09:00:00+00:00",
        "subtotal": 54000,
        "discount_amount": 0,
        "loyalty_discount": 0,
        "tier_discount": 0,
        "delivery_fee": 0,
        "total_amount": 54000,
        "status": "pending",
    }
    order.update(overrides)
    return order


@pytest.fixture
def mock_i18n(monkeypatch):
    """Mock i18n translations for order summary."""
    from i18n import Translation

    tr = Translation()
    tr.translations = {
        "en": {
            "telegram.order.number": "Number",
            "telegram.order.total": "Total: {0} UZS",
            "telegram.orders.estimate_discount_line": "Discount: {amount}",
            "telegram.orders.estimate_reward_line": "Loyalty rewards: {amount}",
            "telegram.orders.order_tier_discount_line": "Cash-payment discount: {amount}",
            "telegram.orders.delivery_fee": "Delivery fee: {amount}",
        }
    }

    monkeypatch.setattr("telegram_bot.utils.i18n", tr)
    return tr


def test_tier_discount_is_stated(app, mock_i18n):
    summary = MessageBuilder.build_order_summary(_order(tier_discount=810, total_amount=53190), "en")

    assert "810" in summary


def test_discount_line_comes_before_the_total(app, mock_i18n):
    """Line order is the one thing this change moves: the total must render
    AFTER the discount breakdown, not above it, so the customer sees why the
    total is lower than the item lines before they see the total itself."""
    summary = MessageBuilder.build_order_summary(_order(tier_discount=810, total_amount=53190), "en")

    lines = summary.split("\n")
    discount_index = next(i for i, line in enumerate(lines) if "Cash-payment discount" in line)
    total_index = next(i for i, line in enumerate(lines) if line.startswith("💰"))

    assert discount_index < total_index


def test_no_breakdown_when_nothing_is_discounted(app, mock_i18n):
    summary = MessageBuilder.build_order_summary(_order(), "en")

    assert "810" not in summary
    # Total amount should appear in the output (54,000 UZS)
    assert "54,000" in summary
    # None of the discount/reward/tier line labels should render either —
    # a regression that unconditionally emits a zero-valued line would pass
    # the amount-only assertion above but still be a defect.
    assert "Discount" not in summary
    assert "Loyalty rewards" not in summary
    assert "Cash-payment discount" not in summary
