"""
Unit tests for order number generation in current Order model.
"""

import re
from decimal import Decimal
from unittest.mock import Mock

import pytest

from business_app.models.order import Order
from business_app.utils.constants import ORDER_SOURCE_PREFIXES


@pytest.mark.unit
@pytest.mark.order
class TestOrderNumberGeneration:
    def test_source_prefixes_include_supported_channels(self):
        assert ORDER_SOURCE_PREFIXES["telegram"] == "TG"
        assert ORDER_SOURCE_PREFIXES["web"] == "WB"
        assert ORDER_SOURCE_PREFIXES["phone"] == "CC"
        assert ORDER_SOURCE_PREFIXES["admin"] == "AD"

    def test_generates_expected_pattern_for_known_source(self, monkeypatch, app):
        fake_result = Mock()
        fake_result.scalar.return_value = 42

        with app.app_context():
            monkeypatch.setattr("business_app.models.order.db.session.execute", lambda *args, **kwargs: fake_result)
            order = Order(
                user_id=1,
                order_source="telegram",
                subtotal=Decimal("10000.00"),
                total_amount=Decimal("10000.00"),
            )

        assert re.match(r"^TG_000042_\d{2}$", order.order_number)

    def test_defaults_to_web_prefix_for_unknown_source(self, monkeypatch, app):
        fake_result = Mock()
        fake_result.scalar.return_value = 1

        with app.app_context():
            monkeypatch.setattr("business_app.models.order.db.session.execute", lambda *args, **kwargs: fake_result)
            order = Order(
                user_id=1,
                order_source="legacy_unknown",
                subtotal=Decimal("10000.00"),
                total_amount=Decimal("10000.00"),
            )

        assert order.order_number.startswith("WB_")

    def test_falls_back_when_sequence_query_fails(self, monkeypatch, app):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        with app.app_context():
            monkeypatch.setattr("business_app.models.order.db.session.execute", _boom)
            order = Order(
                user_id=1,
                order_source="web",
                subtotal=Decimal("10000.00"),
                total_amount=Decimal("10000.00"),
            )

        assert order.order_number.startswith("WB")
        assert len(order.order_number) >= 10
