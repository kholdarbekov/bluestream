"""The driver's day starts at LOCAL midnight, not UTC midnight.

`route_optimization_service` scoped "today" to UTC midnight in three places
(`current_route`, `_resolve_start_point`, `_upsert_route`) while a fourth
already used local midnight. The business runs on DISPLAY_TIMEZONE
(Asia/Tashkent, UTC+5), so UTC midnight is 05:00 local and "today" actually
meant 05:00 yesterday -> 05:00 today. Every day between 00:00 and 05:00 local
that cut both ways:

  * a delivery completed at 04:45 local counted as YESTERDAY, so the start
    point fell through to the warehouse instead of the driver's real last
    position;
  * a delivery completed at 08:00 the PREVIOUS local day still counted as
    today, anchoring the route on a ~20h-old GPS fix.

It also surfaced as a nightly test failure: any test placing a record at
`now - 45min` fails for the 45 minutes after UTC midnight.

These tests are wall-clock independent — they assert the property (the
boundary is local midnight) rather than a hard-coded instant, so they hold at
every hour of the day.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app.services import route_optimization_service as ros
from shared.constants import DISPLAY_TIMEZONE


@pytest.mark.unit
class TestDriverDayStart:
    def test_day_start_is_local_midnight(self, app):
        """UTC midnight is 05:00 in Tashkent — the driver's day starts at 00:00."""
        with app.app_context():
            start = ros._driver_day_start_utc()

        local = start.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)

    def test_day_start_is_the_most_recent_local_midnight(self, app):
        """Not just any local midnight: the one that started the current day."""
        with app.app_context():
            start = ros._driver_day_start_utc()

        now = datetime.now(timezone.utc)
        assert start <= now
        assert now - start < timedelta(days=1)

    def test_day_start_is_timezone_aware(self, app):
        """Compared against tz-aware DB columns; a naive value would raise."""
        with app.app_context():
            start = ros._driver_day_start_utc()

        assert start.tzinfo is not None
        assert start.utcoffset() == timedelta(0), "must be normalized to UTC for DB comparison"


@pytest.mark.unit
class TestReaderAndWriterAgree:
    """The dispatch map READS the route rows the optimizer WRITES.

    dispatch_service._route_window_start_utc carries an explicit warning to
    keep it identical to RouteOptimizationService's boundary: if the two
    disagree, an admin can edit a route the map is not displaying, or see one
    the editor can no longer find. Moving the optimizer to local midnight
    without moving this would have opened exactly that 5-hour gap.
    """

    def test_dispatch_route_window_matches_the_optimizer(self, app):
        from business_app.services.dispatch_service import DispatchService

        with app.app_context():
            assert DispatchService._route_window_start_utc() == ros._driver_day_start_utc()


@pytest.mark.unit
class TestDayBoundaryHasOneDefinition:
    def test_no_utc_midnight_day_boundary_remains(self):
        """The whole bug was one decision written four times, three of them
        wrong. Pin it: the module must derive the day boundary in exactly one
        place, so a fourth copy cannot drift back to UTC."""
        import inspect

        source = inspect.getsource(ros)

        assert 'datetime.now(timezone.utc).replace(hour=0' not in source
        assert source.count("def _driver_day_start_utc") == 1
        # Every consumer goes through the helper.
        assert source.count("_driver_day_start_utc()") >= 4
