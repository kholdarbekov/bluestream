"""The gauge behind `PriceChangeSwitchLate`: how long a due price change has gone unapplied.

Read on the web process's `/metrics` scrape, so a dead worker or beat still pages.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from business_app.models.price_change import PriceChange, PriceChangeStatus
from business_app.utils import prometheus_metrics as pm
from business_app.utils.local_windows import local_date
from tests.integration.price_change_builders import NEW_19, CAP_19, bottles, make_admin, make_price_change

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _sentinel_gauge(monkeypatch):
    # -1 distinguishes "left unchanged" from "set to 0".
    pm.price_change_overdue_seconds.set(-1)
    monkeypatch.setattr(pm, "_price_change_gauge_last_refresh", 0.0)
    yield


def _gauge():
    return pm.price_change_overdue_seconds._value.get()


def _change(db, *, days_ago=1, status=PriceChangeStatus.ANNOUNCED, **columns):
    change_on = local_date() - timedelta(days=days_ago)
    effective_at = PriceChange(change_on=change_on).effective_at
    columns = {"announcement_started_at": effective_at - timedelta(days=28), **columns}
    if status == PriceChangeStatus.SCHEDULED:
        columns["announcement_started_at"] = None
    b = bottles(db)
    change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None)],
        created_by=make_admin(db),
        announce_on=change_on - timedelta(days=28),
        change_on=change_on,
        status=status,
        **columns,
    )
    db.session.commit()
    return change


def test_no_due_change_reads_zero(app, db):
    pm._refresh_price_change_gauge(force=True)

    assert _gauge() == 0


def test_an_announced_change_past_its_instant_reads_its_lateness(app, db):
    change = _change(db)

    pm._refresh_price_change_gauge(force=True)

    expected = (datetime.now(timezone.utc) - change.effective_at).total_seconds()
    assert abs(_gauge() - expected) < 60


def test_a_change_never_announced_also_counts(app, db):
    change = _change(db, status=PriceChangeStatus.SCHEDULED)

    pm._refresh_price_change_gauge(force=True)

    expected = (datetime.now(timezone.utc) - change.effective_at).total_seconds()
    assert abs(_gauge() - expected) < 60


def test_a_change_not_yet_due_reads_zero(app, db):
    _change(db, days_ago=-3)

    pm._refresh_price_change_gauge(force=True)

    assert _gauge() == 0


@pytest.mark.parametrize("status", [PriceChangeStatus.APPLIED, PriceChangeStatus.CANCELLED])
def test_applied_and_cancelled_changes_read_zero(app, db, status):
    effective_at = PriceChange(change_on=local_date() - timedelta(days=1)).effective_at
    if status == PriceChangeStatus.APPLIED:
        _change(db, status=status, applied_at=effective_at)
    else:
        # The shared builder has no `cancelled_at`; the status matrix needs it set with the status.
        change = _change(db, status=PriceChangeStatus.ANNOUNCED)
        change.status, change.cancelled_at = status, effective_at
        db.session.commit()

    pm._refresh_price_change_gauge(force=True)

    assert _gauge() == 0


def test_a_database_error_leaves_the_previous_value(app, db, monkeypatch):
    pm.price_change_overdue_seconds.set(1234.0)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pm, "_pending_price_change_instants", boom)

    pm._refresh_price_change_gauge(force=True)  # must not raise

    assert _gauge() == 1234.0


def test_the_metrics_scrape_refreshes_it(client, db):
    _change(db)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert _gauge() > 0


def test_the_alert_pages_fifteen_minutes_after_the_change_instant():
    doc = yaml.safe_load(Path("monitoring/alert_rules.yml").read_text())
    rules = {rule["alert"]: rule for group in doc["groups"] for rule in group["rules"] if "alert" in rule}
    rule = rules["PriceChangeSwitchLate"]

    assert rule["expr"] == "max(bluestream_price_change_overdue_seconds) > 600"
    assert rule["for"] == "5m"
    assert rule["labels"] == {"severity": "critical", "area": "pricing"}
    description = rule["annotations"]["description"]
    assert "price_change_apply_failed" in description
    assert "CeleryHeartbeatStale" in description
    assert "cancel or reschedule it on the admin Price changes page" in description
