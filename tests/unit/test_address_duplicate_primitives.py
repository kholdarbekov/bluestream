"""Primitives of the duplicate-address guard: apartment comparison, fallback points and the two radii."""

import pytest

from business_app.utils.address_helpers import apartments_differ
from business_app.utils.geo_validation import is_fallback_point
from shared.constants import TASHKENT_COORDINATES, TASHKENT_DISTRICTS


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("5", "7", True),
        (" 5a ", "5A", False),
        ("5", None, False),
        ("", "7", False),
        (None, None, False),
        # A request body may carry a JSON number: it is the apartment it reads as.
        (5, "5", False),
        (5, "7", True),
        ("7", 5, True),
    ],
)
def test_apartments_differ_only_when_both_are_known_and_different(a, b, expected):
    assert apartments_differ(a, b) is expected


def test_every_district_centre_and_the_city_centre_are_fallback_points():
    centres = [d["center"] for d in TASHKENT_DISTRICTS.values()] + [
        (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"])
    ]
    assert all(is_fallback_point(lat, lng) for lat, lng in centres)
    assert is_fallback_point(41.2995000004, 69.2401) is True  # equal after round(6)
    assert is_fallback_point(41.299501, 69.2401) is False
    assert is_fallback_point(None, 69.2401) is False and is_fallback_point("x", "y") is False


@pytest.fixture
def restore_business_config():
    # As tests/unit/test_place_suggestion_radius_config.py: `reload` rebinds the LIVE module and this
    # teardown runs before monkeypatch's, so reload, then re-pin the captured values.
    import importlib

    import shared.business_config as bc

    originals = (bc.ADDRESS_DUPLICATE_RADIUS_M, bc.ADDRESS_MOVE_MAX_M)
    yield
    importlib.reload(bc)
    bc.ADDRESS_DUPLICATE_RADIUS_M, bc.ADDRESS_MOVE_MAX_M = originals


def test_the_radii_default_to_50_and_150_and_read_the_environment(monkeypatch, app, restore_business_config):
    assert (app.config["ADDRESS_DUPLICATE_RADIUS_M"], app.config["ADDRESS_MOVE_MAX_M"]) == (50, 150)
    monkeypatch.setenv("ADDRESS_DUPLICATE_RADIUS_M", "35")
    import importlib

    import shared.business_config as bc

    assert importlib.reload(bc).ADDRESS_DUPLICATE_RADIUS_M == 35
