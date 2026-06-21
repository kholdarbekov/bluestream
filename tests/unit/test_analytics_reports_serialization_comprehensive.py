"""Comprehensive regression tests: analytics report metrics must be JSON-serializable.

PROD INCIDENT RECAP
-------------------
The daily/weekly analytics beat tasks persist the whole business report into
``AnalyticsReport.report_data`` — a ``JSON`` column (``nullable=False``) — via
``db.session.commit()``. SQLAlchemy serialises that column with ``json.dumps``.

``AnalyticsService._get_revenue_metrics`` and
``_get_customer_lifetime_value_analysis`` returned raw ``Decimal`` money values
straight out of SQL ``SUM``/``AVG`` aggregates (and a ``Decimal`` growth_rate
derived from them). On commit this raised::

    TypeError: Object of type Decimal is not JSON serializable

so the task fell into its ``except`` block, returned ``{"error": ...}``, and
NO report row was ever written.

WHY THE OLD 4000+ TESTS MISSED IT
---------------------------------
* The bug only surfaces when the SUM/AVG aggregates actually return ``Decimal``
  objects — i.e. when there is real order data in the window. Tests that ran
  against an empty DB got ``0`` (a Python int from the ``or 0`` fallback) and so
  never exercised the Decimal path.
* Sibling unit tests mocked the sub-methods, so the real aggregate types were
  never produced or serialised.

These tests therefore SEED REAL ORDERS with ``Decimal`` totals (current +
previous period, delivered + pending, multiple customers) so the aggregates
return genuine ``Decimal`` values, then assert the report is ``json.dumps``-able
and contains NO ``Decimal`` anywhere — exactly mirroring the JSON-column write
that crashed in prod.
"""

import json
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pytest

from business_app.models.analytics import AnalyticsReport
from business_app.models.order import Order
from business_app.services.analytics_service import AnalyticsService
from business_app.tasks import analytics_tasks
from shared.enums import OrderStatus


# ---------------------------------------------------------------------------
# SQLite dialect shim (NOT the bug under test)
# ---------------------------------------------------------------------------
# The test suite runs on SQLite in-memory. ``func.date(Order.created_at)`` is
# returned by SQLite as a *string* (e.g. "2026-06-21") rather than the
# ``datetime.date`` object Postgres returns, so the production helpers that call
# ``date.isoformat()`` on that grouped column (``_get_growth_trends`` and
# ``_get_daily_sales_trend``) raise ``AttributeError`` on SQLite *whenever order
# rows exist* — an environment quirk unrelated to the Decimal-serialization bug.
#
# To exercise the REAL Decimal path (revenue/CLV/order/customer aggregates) for
# the whole-overview and full-report scenarios while seeding real orders, this
# autouse fixture replaces ONLY those two date-string helpers with Postgres-
# shaped, already-float output. Everything that produced the prod Decimal leak
# (_get_revenue_metrics, _get_customer_lifetime_value_analysis, ...) is left
# fully intact and runs against the seeded Decimal orders.
@pytest.fixture(autouse=True)
def _patch_sqlite_date_string_helpers(monkeypatch):
    def _fake_growth_trends(self, start_date, end_date):
        return {"daily_orders": [], "daily_revenue": []}

    def _fake_daily_sales_trend(self, start_date, end_date):
        return []

    monkeypatch.setattr(AnalyticsService, "_get_growth_trends", _fake_growth_trends)
    monkeypatch.setattr(AnalyticsService, "_get_daily_sales_trend", _fake_daily_sales_trend)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_order(
    db,
    user_id,
    *,
    number,
    total,
    subtotal=None,
    status=OrderStatus.DELIVERED,
    created_at=None,
):
    """Create and persist an Order with Decimal money so SUM/AVG return Decimal."""
    if created_at is None:
        created_at = datetime.now(UTC)
    if subtotal is None:
        subtotal = total
    order = Order(
        user_id=user_id,
        order_number=number,
        status=status,
        subtotal=Decimal(str(subtotal)),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(str(total)),
        created_at=created_at,
    )
    db.session.add(order)
    return order


def _find_decimals(obj, path="report"):
    """Recursively walk a dict/list and return paths of any Decimal values.

    A non-empty result means the structure would crash ``json.dumps`` (and
    therefore the AnalyticsReport JSON-column commit) in prod.
    """
    found = []
    if isinstance(obj, Decimal):
        found.append(path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(_find_decimals(v, f"{path}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(_find_decimals(v, f"{path}[{i}]"))
    return found


@pytest.fixture
def analytics_service():
    return AnalyticsService()


@pytest.fixture
def seeded_orders(db, sample_user, admin_user, delivery_driver):
    """Seed orders that force SUM/AVG aggregates to return real Decimals.

    Spans the *current* period (last day) AND the *previous* period (~2 days
    ago) so ``_get_revenue_metrics`` computes a non-trivial growth_rate from
    two Decimal sums (the exact value that was a Decimal pre-fix), and uses
    multiple distinct customers so the CLV analysis sums per-customer Decimals.
    """
    now = datetime.now(UTC)

    # --- current period (within the last day) ---
    _make_order(
        db, sample_user.id, number="ORD-CUR-1", total="18000.00",
        status=OrderStatus.DELIVERED, created_at=now - timedelta(hours=2),
    )
    _make_order(
        db, sample_user.id, number="ORD-CUR-2", total="25500.50",
        status=OrderStatus.DELIVERED, created_at=now - timedelta(hours=5),
    )
    _make_order(
        db, admin_user.id, number="ORD-CUR-3", total="9999.99",
        status=OrderStatus.PENDING, created_at=now - timedelta(hours=8),
    )
    # cancelled order must be excluded from revenue/CLV aggregates
    _make_order(
        db, admin_user.id, number="ORD-CUR-CANCEL", total="50000.00",
        status=OrderStatus.CANCELLED, created_at=now - timedelta(hours=10),
    )

    # --- previous period (about 2 days back) so growth_rate is computed ---
    _make_order(
        db, delivery_driver.id, number="ORD-PREV-1", total="12000.00",
        status=OrderStatus.DELIVERED, created_at=now - timedelta(days=2),
    )
    _make_order(
        db, sample_user.id, number="ORD-PREV-2", total="7777.77",
        status=OrderStatus.DELIVERED, created_at=now - timedelta(days=2, hours=3),
    )

    db.session.commit()
    return {
        "now": now,
        "current_start": now - timedelta(days=1),
        "current_end": now + timedelta(minutes=1),
    }


# ---------------------------------------------------------------------------
# _get_revenue_metrics
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.analytics
class TestRevenueMetricsSerialization:
    def test_all_money_fields_are_float_and_json_dumpable_with_data(self, analytics_service, seeded_orders):
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        metrics = analytics_service._get_revenue_metrics(start, end)

        # Mirrors the JSON-column write that crashed in prod.
        json.dumps(metrics)

        assert isinstance(metrics["total_revenue"], float)
        assert isinstance(metrics["average_order_value"], float)
        assert isinstance(metrics["previous_period_revenue"], float)
        assert isinstance(metrics["growth_rate"], float)
        # No raw Decimal must survive anywhere.
        assert _find_decimals(metrics) == []

    def test_total_revenue_value_excludes_cancelled_and_matches_seed(self, analytics_service, seeded_orders):
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        metrics = analytics_service._get_revenue_metrics(start, end)

        # 18000 + 25500.50 + 9999.99 = 53500.49 (cancelled 50000 excluded)
        assert metrics["total_revenue"] == pytest.approx(53500.49, abs=0.01)

    def test_growth_rate_is_nonzero_float_when_previous_period_has_revenue(self, analytics_service, seeded_orders):
        """The growth_rate was a Decimal pre-fix because it divides two Decimal sums."""
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        metrics = analytics_service._get_revenue_metrics(start, end)

        # Previous window has real revenue, so growth_rate is computed (not the
        # int-0 short-circuit) — this is the path that produced a Decimal.
        assert metrics["previous_period_revenue"] > 0
        assert isinstance(metrics["growth_rate"], float)
        assert metrics["growth_rate"] != 0
        json.dumps(metrics["growth_rate"])

    def test_empty_range_returns_serializable_zeros(self, analytics_service, db):
        """Empty window must still be float zeros and json-serializable (no crash)."""
        far_past_start = datetime(2000, 1, 1, tzinfo=UTC)
        far_past_end = datetime(2000, 1, 2, tzinfo=UTC)

        metrics = analytics_service._get_revenue_metrics(far_past_start, far_past_end)

        json.dumps(metrics)
        assert metrics["total_revenue"] == 0.0
        assert metrics["average_order_value"] == 0.0
        assert metrics["previous_period_revenue"] == 0.0
        assert metrics["growth_rate"] == 0.0
        assert all(isinstance(metrics[k], float) for k in metrics)

    def test_single_order_average_order_value_is_float(self, analytics_service, db, sample_user):
        now = datetime.now(UTC)
        _make_order(db, sample_user.id, number="ORD-SOLO", total="13333.33", created_at=now)
        db.session.commit()

        metrics = analytics_service._get_revenue_metrics(now - timedelta(hours=1), now + timedelta(hours=1))

        json.dumps(metrics)
        assert isinstance(metrics["average_order_value"], float)
        assert metrics["average_order_value"] == pytest.approx(13333.33, abs=0.01)


# ---------------------------------------------------------------------------
# _get_customer_lifetime_value_analysis
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.analytics
class TestClvAnalysisSerialization:
    def test_average_clv_is_float_and_json_dumpable_with_multiple_customers(self, analytics_service, seeded_orders):
        clv = analytics_service._get_customer_lifetime_value_analysis()

        json.dumps(clv)
        assert isinstance(clv["average_clv"], float)
        assert clv["total_customers"] >= 2
        assert _find_decimals(clv) == []

    def test_clv_sums_decimal_per_customer_values_into_float(self, analytics_service, db, sample_user, admin_user):
        """The per-customer total_value is a Decimal SUM; total_clv must float() it."""
        now = datetime.now(UTC)
        _make_order(db, sample_user.id, number="CLV-1", total="10000.00", created_at=now)
        _make_order(db, sample_user.id, number="CLV-2", total="20000.00", created_at=now)
        _make_order(db, admin_user.id, number="CLV-3", total="30000.00", created_at=now)
        db.session.commit()

        clv = analytics_service._get_customer_lifetime_value_analysis()

        json.dumps(clv)
        # two customers: (30000 + 30000) / 2 = 30000.0
        assert clv["average_clv"] == pytest.approx(30000.0, abs=0.01)
        assert isinstance(clv["average_clv"], float)

    def test_clv_no_customers_returns_serializable(self, analytics_service, db):
        clv = analytics_service._get_customer_lifetime_value_analysis()

        json.dumps(clv)
        assert clv["total_customers"] == 0
        assert _find_decimals(clv) == []


# ---------------------------------------------------------------------------
# get_dashboard_overview (whole payload incl. revenue subsection)
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.analytics
class TestDashboardOverviewSerialization:
    def test_whole_overview_is_json_dumpable_with_seeded_data(self, analytics_service, seeded_orders):
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        overview = analytics_service.get_dashboard_overview(start, end)

        json.dumps(overview)
        # The revenue subsection is the one that leaked Decimals.
        assert isinstance(overview["revenue"]["total_revenue"], float)
        assert isinstance(overview["revenue"]["growth_rate"], float)
        assert _find_decimals(overview) == []

    def test_overview_default_period_is_serializable(self, analytics_service, seeded_orders):
        """No explicit dates -> service computes a 30-day window; must still serialize."""
        overview = analytics_service.get_dashboard_overview()

        json.dumps(overview)
        assert _find_decimals(overview) == []


# ---------------------------------------------------------------------------
# generate_business_report for every report type
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.analytics
class TestGenerateBusinessReportSerialization:
    @pytest.mark.parametrize("report_type", ["daily", "weekly", "monthly", "quarterly", "annual"])
    def test_report_is_json_dumpable_and_decimal_free(self, analytics_service, seeded_orders, report_type):
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        report = analytics_service.generate_business_report(report_type, start, end)

        # This is the real-world guard: the daily AND weekly beat tasks persist
        # exactly this dict into the JSON column.
        json.dumps(report)
        leaks = _find_decimals(report)
        assert leaks == [], f"{report_type} report leaked Decimal at: {leaks}"
        # quarterly/annual reuse the monthly generator (which hardcodes the
        # "monthly" report_type label) — that is the real prod behaviour.
        expected_label = "monthly" if report_type in ("quarterly", "annual") else report_type
        assert report["report_type"] == expected_label

    @pytest.mark.parametrize("report_type", ["daily", "weekly", "monthly", "quarterly", "annual"])
    def test_report_overview_revenue_subsection_is_float(self, analytics_service, seeded_orders, report_type):
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        report = analytics_service.generate_business_report(report_type, start, end)

        revenue = report["overview"]["revenue"]
        assert isinstance(revenue["total_revenue"], float)
        assert isinstance(revenue["average_order_value"], float)
        assert isinstance(revenue["growth_rate"], float)
        assert isinstance(revenue["previous_period_revenue"], float)

    def test_weekly_report_embeds_clv_as_float(self, analytics_service, seeded_orders):
        """Weekly report embeds get_customer_analytics -> lifetime_value -> average_clv."""
        start = seeded_orders["current_start"]
        end = seeded_orders["current_end"]

        report = analytics_service.generate_business_report("weekly", start, end)

        clv = report["customers"]["lifetime_value"]
        assert isinstance(clv["average_clv"], float)
        json.dumps(report)


# ---------------------------------------------------------------------------
# Celery tasks end-to-end (the real prod crash site)
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.analytics
class TestAnalyticsTasksPersistRealReport:
    def test_daily_task_persists_report_without_decimal_typeerror(self, db, seeded_orders, admin_user):
        """End-to-end: real AnalyticsService report committed to the JSON column.

        This is the exact prod failure mode. With seeded order data the report
        contains Decimal-producing aggregates; if any leaks, the commit raises
        'Object of type Decimal is not JSON serializable' and the task returns
        {"error": ...} with no row written.
        """
        result = analytics_tasks.generate_daily_analytics_report.run()

        assert "error" not in result, f"daily task failed (Decimal leak?): {result}"
        assert result["success"] is True

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "daily"
        # report_data round-trips cleanly through JSON.
        round_tripped = json.loads(json.dumps(report.report_data))
        assert round_tripped == report.report_data
        assert _find_decimals(report.report_data) == []

    def test_daily_task_report_data_revenue_is_float(self, db, seeded_orders):
        result = analytics_tasks.generate_daily_analytics_report.run()

        assert result.get("success") is True
        report = AnalyticsReport.query.get(result["report_id"])
        revenue = report.report_data["overview"]["revenue"]
        assert isinstance(revenue["total_revenue"], float)
        assert isinstance(revenue["growth_rate"], float)

    def test_weekly_task_persists_report_without_decimal_typeerror(self, db, seeded_orders, admin_user):
        result = analytics_tasks.generate_weekly_business_report.run()

        assert "error" not in result, f"weekly task failed (Decimal leak?): {result}"
        assert result["success"] is True

        report = AnalyticsReport.query.get(result["report_id"])
        assert report is not None
        assert report.report_type == "weekly"
        round_tripped = json.loads(json.dumps(report.report_data))
        assert round_tripped == report.report_data
        assert _find_decimals(report.report_data) == []

    def test_daily_task_decimal_leak_would_be_caught_structurally(self, db, seeded_orders):
        """Strong structural guard: walk the persisted dict, fail on ANY Decimal.

        Even if a future change introduces a Decimal that happens to be
        json.dumps-tolerated by some encoder, this recursive check fails loudly.
        """
        result = analytics_tasks.generate_daily_analytics_report.run()
        report = AnalyticsReport.query.get(result["report_id"])

        leaks = _find_decimals(report.report_data, "daily_report_data")
        assert leaks == [], f"Decimal leaked into persisted daily report: {leaks}"

    def test_weekly_task_decimal_leak_would_be_caught_structurally(self, db, seeded_orders):
        result = analytics_tasks.generate_weekly_business_report.run()
        report = AnalyticsReport.query.get(result["report_id"])

        leaks = _find_decimals(report.report_data, "weekly_report_data")
        assert leaks == [], f"Decimal leaked into persisted weekly report: {leaks}"
