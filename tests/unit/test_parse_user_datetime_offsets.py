"""parse_user_datetime: an explicit offset / "Z" is the instant itself; a naive value is local wall time."""

from datetime import UTC, datetime

import pytest

from business_app.utils.timezone_utils import parse_user_datetime

TASHKENT = "Asia/Tashkent"
NINE_Z = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-11-10T09:00:00.000Z", NINE_Z),
        ("2026-11-10T09:00:00.347Z", datetime(2026, 11, 10, 9, 0, 0, 347000, tzinfo=UTC)),
        ("2026-11-10T09:00:00Z", NINE_Z),
        ("2026-11-10T09:00:00+00:00", NINE_Z),
        ("2026-11-10T14:00:00+05:00", NINE_Z),
        ("2026-11-10T14:00:00+0500", NINE_Z),
        ("2026-11-10T14:00:00.000+05:00", NINE_Z),
        ("2026-11-10T09:00:00.123456+00:00", datetime(2026, 11, 10, 9, 0, 0, 123456, tzinfo=UTC)),
        ("2026-11-10T14:00:00", NINE_Z),
        ("2026-11-10T14:00:00.000", NINE_Z),
        ("2026-11-10 14:00:00", NINE_Z),
        ("2026-11-10 14:00", NINE_Z),
        ("2026-11-10", datetime(2026, 11, 9, 19, 0, tzinfo=UTC)),
    ],
)
def test_shape_resolves_to_the_true_instant_in_utc(raw, expected):
    result = parse_user_datetime(raw, source_tz=TASHKENT)

    assert result.utcoffset().total_seconds() == 0
    assert result == expected


def test_time_of_day_alone_is_refused():
    with pytest.raises(ValueError):
        parse_user_datetime("09:00", source_tz=TASHKENT)


def test_own_output_is_idempotent():
    first = parse_user_datetime("2026-11-10T09:00:00.000Z", source_tz=TASHKENT)

    assert parse_user_datetime(first.isoformat(), source_tz=TASHKENT) == first
