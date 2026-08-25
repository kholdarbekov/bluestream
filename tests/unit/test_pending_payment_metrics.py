"""PENDING-payment observability.

Three stacked breakages made `StalePaymentsPending` unable to fire, ever:

1. `payments_pending_age_seconds` was a `prometheus_client.Summary`. The Python
   client's Summary emits only `_count`/`_sum` — it has NO `quantile` label
   series (unlike the Go client) — while `monitoring/alert_rules.yml` selected
   `{quantile="0.95"}`. Confirmed against prod Prometheus 2026-08-23: the only
   series present are `payments_pending_age_seconds_count` and `..._sum`.
2. The sampler ran inside the Celery worker.
3. `monitoring/prometheus.yml` scrapes `business_app` only — there is no
   `celery_worker` scrape target, so the worker's samples were never collected.

Raw age is also the wrong signal now: under the no-auto-cancel policy a large
PENDING age is expected and benign. The genuinely-wrong state — order 1100's
shape — is an unpaid electronic payment still PENDING on an order that has
already left the door.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _metric_names():
    from business_app.utils import prometheus_metrics

    return {
        name
        for name in dir(prometheus_metrics)
        if name.startswith("payments_pending")
    }


def test_summary_with_unreachable_quantile_is_gone():
    """A Summary can never satisfy a {quantile=...} selector in this client."""
    from prometheus_client import Summary

    from business_app.utils import prometheus_metrics

    stale = getattr(prometheus_metrics, "payments_pending_age_seconds", None)
    assert not isinstance(stale, Summary), (
        "payments_pending_age_seconds must not be a Summary — the Python client "
        "emits no quantile series, so the alert selecting {quantile=\"0.95\"} "
        "matched nothing and never fired"
    )


def test_gauges_exist_for_the_states_we_actually_alert_on():
    from prometheus_client import Gauge

    from business_app.utils import prometheus_metrics

    for name in (
        "payments_pending_total",
        "payments_pending_oldest_age_seconds",
        "payments_pending_on_closed_order_total",
    ):
        metric = getattr(prometheus_metrics, name, None)
        assert isinstance(metric, Gauge), f"{name} must exist and be a Gauge"


class TestPendingPaymentGaugeRefresh:
    def _seed(self, db, sample_order, *, order_status, payment_status, age_hours, method=PaymentMethod.CLICK):
        sample_order.status = order_status
        payment = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=method,
            amount=Decimal("18000.00"),
            currency="UZS",
            status=payment_status,
            payment_id=f"pm-{order_status.value}-{payment_status.value}-{age_hours}",
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        )
        db.session.add(payment)
        db.session.commit()
        return payment

    def test_counts_pending_and_reports_oldest_age(self, app, db, sample_order):
        from business_app.utils.prometheus_metrics import (
            _refresh_pending_payment_gauges,
            payments_pending_oldest_age_seconds,
            payments_pending_total,
        )

        self._seed(db, sample_order, order_status=OrderStatus.CONFIRMED,
                   payment_status=PaymentStatus.PENDING, age_hours=5)

        _refresh_pending_payment_gauges(force=True)

        assert payments_pending_total._value.get() == 1
        assert payments_pending_oldest_age_seconds._value.get() >= 5 * 3600 - 60

    def test_pending_electronic_payment_on_a_closed_order_is_counted(self, app, db, sample_order):
        """Order 1100's exact shape — the invisible-money state."""
        from business_app.utils.prometheus_metrics import (
            _refresh_pending_payment_gauges,
            payments_pending_on_closed_order_total,
        )

        self._seed(db, sample_order, order_status=OrderStatus.DELIVERED,
                   payment_status=PaymentStatus.PENDING, age_hours=30)

        _refresh_pending_payment_gauges(force=True)

        assert payments_pending_on_closed_order_total._value.get() == 1

    def test_pending_payment_on_a_live_order_is_not_flagged_as_closed(self, app, db, sample_order):
        """Benign under the no-auto-cancel policy — must not alert."""
        from business_app.utils.prometheus_metrics import (
            _refresh_pending_payment_gauges,
            payments_pending_on_closed_order_total,
        )

        self._seed(db, sample_order, order_status=OrderStatus.OUT_FOR_DELIVERY,
                   payment_status=PaymentStatus.PENDING, age_hours=48)

        _refresh_pending_payment_gauges(force=True)

        assert payments_pending_on_closed_order_total._value.get() == 0

    def test_a_db_error_never_breaks_the_scrape(self, app, db, monkeypatch):
        """Gauges are best-effort: /metrics must not 500 because a query failed."""
        from business_app.utils import prometheus_metrics

        def boom(*_a, **_kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(prometheus_metrics, "_pending_payment_rows", boom)
        prometheus_metrics._refresh_pending_payment_gauges(force=True)  # must not raise


def test_alert_rule_targets_the_closed_order_state_not_raw_age():
    """The alert must key on the wrong STATE, not on an age that is now expected."""
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(Path("monitoring/alert_rules.yml").read_text())
    exprs = [
        rule["expr"]
        for group in doc.get("groups", [])
        for rule in group.get("rules", [])
        if "expr" in rule
    ]
    joined = " ".join(exprs)

    # Parse the expressions, not the file text — the explanatory comment
    # deliberately quotes the old selector.
    assert "quantile=" not in joined or "payments_pending" not in joined, (
        "no alert may select a quantile off payments_pending_* — the Python "
        "prometheus_client Summary emits no quantile series"
    )
    assert any("payments_pending_on_closed_order_total" in e for e in exprs), (
        "the closed-order state must be alerted on"
    )
