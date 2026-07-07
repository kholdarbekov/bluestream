"""Unit tests for Fix A3: analytics serializers must use the MoneyFloat SSOT
instead of a manual ``field_validator`` that converts Decimal -> float.

See business_app/serializers/types.py for the MoneyFloat convention: Decimal
in, float out via a PlainSerializer, no precision loss and no manual
Decimal-to-float field_validator anti-pattern.
"""

from decimal import Decimal

import pytest

from business_app.serializers.analytics_serializers import (
    ChartDataPointSchema,
    ChartSchema,
    ChartType,
    DashboardMetricSchema,
)


@pytest.mark.unit
class TestDashboardMetricSchemaMoneyFloat:
    def test_decimal_value_serializes_to_float_without_precision_loss(self):
        metric = DashboardMetricSchema(name="Total Revenue", value=Decimal("1234567.89"))
        dumped = metric.model_dump()

        assert dumped["value"] == 1234567.89
        assert isinstance(dumped["value"], float)

    def test_decimal_previous_value_serializes_to_float(self):
        metric = DashboardMetricSchema(
            name="Total Revenue",
            value=Decimal("100.50"),
            previous_value=Decimal("90.25"),
        )
        dumped = metric.model_dump()

        assert dumped["previous_value"] == 90.25
        assert isinstance(dumped["previous_value"], float)

    def test_previous_value_none_stays_none(self):
        metric = DashboardMetricSchema(name="Total Revenue", value=Decimal("100.50"))
        dumped = metric.model_dump()

        assert dumped["previous_value"] is None

    def test_accepts_int_and_float_inputs(self):
        int_metric = DashboardMetricSchema(name="Orders", value=5, previous_value=3)
        float_metric = DashboardMetricSchema(name="Rate", value=1.5, previous_value=1.2)

        assert int_metric.model_dump()["value"] == 5.0
        assert float_metric.model_dump()["value"] == 1.5

    def test_manual_field_validator_removed(self):
        assert "validate_numeric_values" not in DashboardMetricSchema.__dict__


@pytest.mark.unit
class TestChartDataPointSchemaMoneyFloat:
    def test_decimal_value_serializes_to_float_without_precision_loss(self):
        point = ChartDataPointSchema(label="Jan", value=Decimal("9999.99"))
        dumped = point.model_dump()

        assert dumped["value"] == 9999.99
        assert isinstance(dumped["value"], float)

    def test_manual_field_validator_removed(self):
        assert "validate_value" not in ChartDataPointSchema.__dict__


@pytest.mark.unit
class TestChartSchemaMoneyFloat:
    def test_decimal_total_serializes_to_float_without_precision_loss(self):
        chart = ChartSchema(
            title="Revenue",
            chart_type=ChartType.LINE,
            data=[],
            total=Decimal("54321.12"),
        )
        dumped = chart.model_dump()

        assert dumped["total"] == 54321.12
        assert isinstance(dumped["total"], float)

    def test_total_none_stays_none(self):
        chart = ChartSchema(title="Revenue", chart_type=ChartType.LINE, data=[])
        dumped = chart.model_dump()

        assert dumped["total"] is None

    def test_manual_field_validator_removed(self):
        assert "validate_total" not in ChartSchema.__dict__
