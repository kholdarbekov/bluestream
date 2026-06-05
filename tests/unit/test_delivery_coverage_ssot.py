"""Coverage SSOT accessors used by the public coverage surfaces."""
from shared.constants import (
    TASHKENT_POLYGON,
    TASHKENT_DISTRICTS,
    get_delivery_coverage,
    get_geoshape_polygon,
)


def test_coverage_shape_and_reuses_polygon():
    cov = get_delivery_coverage("en")
    assert cov["city"] == "Tashkent"
    assert cov["polygon"] is TASHKENT_POLYGON          # no second copy of the boundary
    assert cov["center"]["latitude"] == 41.2995
    assert {d["key"] for d in cov["districts"]} == set(TASHKENT_DISTRICTS.keys())
    assert "Tashkent" in cov["summary"]


def test_coverage_localized_and_falls_back_to_en():
    assert get_delivery_coverage("ru")["districts"][0]["name"]       # localized, non-empty
    assert get_delivery_coverage("xx")["summary"] == get_delivery_coverage("en")["summary"]


def test_geoshape_polygon_roundtrips_the_polygon():
    pairs = get_geoshape_polygon().split(" ")
    assert len(pairs) == len(TASHKENT_POLYGON)
    lat0, lng0 = pairs[0].split(",")
    assert float(lat0) == TASHKENT_POLYGON[0][0]
    assert float(lng0) == TASHKENT_POLYGON[0][1]
