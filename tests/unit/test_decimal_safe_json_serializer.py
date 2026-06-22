"""Unit tests for the Decimal-aware JSON serializer used by SQLAlchemy JSON columns.

SQLAlchemy's default ``json_serializer`` is plain ``json.dumps``, which raises
``TypeError: Object of type Decimal is not JSON serializable`` on any stray
``Decimal`` (e.g. an un-cast SQL aggregate landing in an analytics report
payload). ``decimal_safe_json_serializer`` coerces Decimal -> float (the
project's money-as-float-in-JSON convention) while still raising for genuinely
unserializable types so real bugs are not silently swallowed.
"""

import json
from decimal import Decimal

import pytest

from business_app.config.base import decimal_safe_json_serializer


@pytest.mark.unit
def test_serializes_top_level_decimal_as_float():
    assert json.loads(decimal_safe_json_serializer({"revenue": Decimal("234000.00")})) == {
        "revenue": 234000.0
    }


@pytest.mark.unit
def test_serializes_nested_decimal_as_float():
    payload = {"cities": [{"city": "Tashkent", "revenue": Decimal("126000.50")}]}
    assert json.loads(decimal_safe_json_serializer(payload)) == {
        "cities": [{"city": "Tashkent", "revenue": 126000.5}]
    }


@pytest.mark.unit
def test_plain_payload_round_trips_unchanged():
    payload = {"a": 1, "b": "x", "c": [1.5, True, None]}
    assert json.loads(decimal_safe_json_serializer(payload)) == payload


@pytest.mark.unit
def test_still_raises_for_truly_unserializable_type():
    # A set is not JSON serializable — the serializer must not silently swallow it.
    with pytest.raises(TypeError):
        decimal_safe_json_serializer({"x": {1, 2, 3}})
