"""Regression tests for DeliveryPerson.is_working_now timezone handling (RC-A).

Bug: is_working_now compared ``datetime.now(UTC).time()`` against
``working_hours_start``/``working_hours_end`` strings that are authored in the
app's DISPLAY_TIMEZONE (Asia/Tashkent, UTC+5). During a driver's real morning
shift (local 09:00-14:00 == UTC 04:00-09:00) it returned False, silently
disabling auto-assignment and pool-insertion suggestions for every order created
before 09:00 UTC. The comparison must happen in DISPLAY_TIMEZONE.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from business_app.models.delivery import DeliveryPerson

TZ_MODULE = "business_app.utils.timezone_utils"


def _driver(**overrides):
    dp = DeliveryPerson()
    dp.is_active = True
    dp.is_available = True
    dp.working_hours_start = "09:00"
    dp.working_hours_end = "18:00"
    for key, value in overrides.items():
        setattr(dp, key, value)
    return dp


@pytest.mark.unit
class TestIsWorkingNowTimezone:
    def test_morning_utc_maps_into_local_shift(self):
        """05:10 UTC == 10:10 Asia/Tashkent, inside 09:00-18:00 local -> working."""
        driver = _driver()
        fixed_utc = datetime(2026, 7, 18, 5, 10, tzinfo=timezone.utc)
        with patch(f"{TZ_MODULE}.get_utc_now", return_value=fixed_utc):
            assert driver.is_working_now is True

    def test_evening_utc_past_local_close_not_working(self):
        """15:00 UTC == 20:00 Asia/Tashkent, past 18:00 local -> not working."""
        driver = _driver()
        fixed_utc = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
        with patch(f"{TZ_MODULE}.get_utc_now", return_value=fixed_utc):
            assert driver.is_working_now is False

    def test_early_utc_before_local_open_not_working(self):
        """03:30 UTC == 08:30 Asia/Tashkent, before 09:00 local -> not working."""
        driver = _driver()
        fixed_utc = datetime(2026, 7, 18, 3, 30, tzinfo=timezone.utc)
        with patch(f"{TZ_MODULE}.get_utc_now", return_value=fixed_utc):
            assert driver.is_working_now is False

    def test_inactive_driver_never_working(self):
        driver = _driver(is_active=False)
        fixed_utc = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
        with patch(f"{TZ_MODULE}.get_utc_now", return_value=fixed_utc):
            assert driver.is_working_now is False

    def test_unavailable_driver_never_working(self):
        driver = _driver(is_available=False)
        fixed_utc = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
        with patch(f"{TZ_MODULE}.get_utc_now", return_value=fixed_utc):
            assert driver.is_working_now is False
