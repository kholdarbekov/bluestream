"""TimezoneMiddleware request rewrite: Z / offset values are instants, naive values are Tashkent wall time."""

from flask import g

from business_app.middleware.timezone_middleware import TimezoneMiddleware


def test_listed_fields_become_utc_instants_and_the_rest_is_untouched(app):
    body = {
        "scheduled_at": "2026-11-10T09:00:00.000Z",
        "valid_until": "2026-11-10T09:00:00.000Z",
        "valid_from": "2026-11-10T09:00:00.000Z",
        "date_of_birth": "2003-05-22",
        "start_time": "09:00",
        "nested": {"expires_at": "2026-11-10T14:00:00+05:00"},
        "created_at": "2026-11-10T14:00:00",
    }

    with app.test_request_context("/"):
        g.user_timezone = "Asia/Tashkent"
        TimezoneMiddleware()._convert_request_datetimes(body)

    assert body == {
        "scheduled_at": "2026-11-10T09:00:00+00:00",
        "valid_until": "2026-11-10T09:00:00+00:00",
        "valid_from": "2026-11-10T09:00:00.000Z",
        "date_of_birth": "2003-05-22",
        "start_time": "09:00",
        "nested": {"expires_at": "2026-11-10T09:00:00+00:00"},
        "created_at": "2026-11-10T09:00:00+00:00",
    }
