"""Unit tests for the Google Encoded Polyline Algorithm decoder.

The encoded string and coordinates in `test_decodes_googles_official_example`
are Google's own published test vector for this exact algorithm — see
https://developers.google.com/maps/documentation/utilities/polylinealgorithm
("Example" section). Any decoder implementation must reproduce it exactly.
"""

import pytest

from business_app.utils.polyline import decode_polyline


@pytest.mark.unit
class TestDecodePolyline:
    def test_decodes_googles_official_example(self):
        """Google's documented worked example: 3 points, precision 5."""
        encoded = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

        result = decode_polyline(encoded)

        assert result == [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]

    def test_none_input_returns_none(self):
        assert decode_polyline(None) is None

    def test_empty_string_returns_none(self):
        assert decode_polyline("") is None

    def test_never_returns_an_empty_list(self):
        """`[]` is truthy in both Python and JS — OperationsMap.jsx's own
        `hasRealGeometry` check exists specifically because an empty-but-truthy
        geometry value was once treated as "real" and drawn solid. The decoder
        must not resurrect that bug server-side: no usable points means `None`,
        never `[]`.
        """
        assert decode_polyline("") != []
        assert decode_polyline(None) != []

    def test_malformed_input_does_not_raise(self):
        """A provider quirk or truncated string must degrade to the honest
        dashed fallback, not crash the geometry endpoint."""
        assert decode_polyline("not-a-real-polyline-!!!") is None

    def test_truncated_string_does_not_raise(self):
        """A string that ends mid-varint (no continuation-terminating byte)
        must not raise an IndexError walking off the end — it must degrade to
        `None` instead. "_" alone is one continuation byte (0x20, the
        "more bytes follow" bit set) with nothing after it.
        """
        assert decode_polyline("_") is None

    def test_round_trips_a_single_point_at_the_origin(self):
        """(0, 0) encodes to "??" under this algorithm — the simplest
        non-trivial case, useful as a sanity check independent of the
        official multi-point example."""
        assert decode_polyline("??") == [[0.0, 0.0]]
