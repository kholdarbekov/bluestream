"""Regression: persisting a JSON column that contains a ``Decimal`` must not crash.

Prod bug (celery_worker, weekly ``generate_weekly_business_report``): the INSERT
into ``analytics_reports.report_data`` (a JSON column) failed with
``TypeError: Object of type Decimal is not JSON serializable`` because a report
metric was a raw SQLAlchemy ``Decimal`` aggregate. Source-level ``float()``
casts fix the known fields, but the robust SSOT backstop is a Decimal-aware
``json_serializer`` on the engine so EVERY JSON-column write (daily/weekly/
monthly/churn/forecast reports, metadata blobs, ...) tolerates stray Decimals.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db
from business_app.models.analytics import AnalyticsReport


@pytest.mark.integration
def test_json_column_commit_tolerates_decimal(app, db):
    now = datetime.now(timezone.utc)
    report = AnalyticsReport(
        report_type="weekly",
        title="Decimal JSON regression",
        start_date=now - timedelta(days=7),
        end_date=now,
        # A stray Decimal nested in the report payload (as the prod aggregates were).
        report_data={"overview": {"revenue": {"total_revenue": Decimal("234000.00")}}},
    )
    db.session.add(report)
    db.session.commit()  # pre-fix: raises TypeError (Decimal not JSON serializable)

    db.session.refresh(report)
    assert report.report_data["overview"]["revenue"]["total_revenue"] == 234000.0
