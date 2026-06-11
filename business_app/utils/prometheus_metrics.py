"""
Prometheus metrics exposition for INF-003.

Exposes the business-critical counters/gauges/summaries referenced by
[monitoring/alert_rules.yml](../../monitoring/alert_rules.yml):

- payment_webhook_total — every inbound webhook delivery, labelled by provider + outcome
- payment_webhook_failures_total — webhook handler exceptions + signature failures
- payment_webhook_duplicates_total — idempotency-guard hits (replay or gateway retry)
- payments_pending_age_seconds — Summary tracking age of PENDING payments (p95 used by alert)
- pg_pool_in_use / pg_pool_size — SQLAlchemy connection-pool Gauges

Multiprocess: Flask runs under Gunicorn with preload_app=True + 2 workers + 3 gthreads.
prometheus_client needs PROMETHEUS_MULTIPROC_DIR set before import so Counters and
Summaries aggregate correctly across processes; Gauges need explicit multiprocess_mode.
Gauge defaults to 'all' here — for pool stats we use 'liveall' so each worker reports
its own snapshot, aggregated via sum/max in the dashboard query.
"""

from __future__ import annotations

import os
from typing import Optional

from prometheus_client import Counter, Gauge, Summary, CollectorRegistry, multiprocess
from prometheus_flask_exporter import PrometheusMetrics


# Per-process default registry is replaced in multiproc mode at /metrics scrape time.
payment_webhook_total = Counter(
    "payment_webhook_total",
    "Total inbound payment webhooks received, by provider and outcome.",
    labelnames=("provider", "outcome"),  # outcome: success|signature_invalid|unsupported|exception|rate_limited
)

payment_webhook_failures_total = Counter(
    "payment_webhook_failures_total",
    "Payment webhooks that failed handler processing (signature, parse, or exception).",
    labelnames=("provider", "reason"),  # reason: signature_invalid|unsupported|exception|rate_limited
)

payment_webhook_duplicates_total = Counter(
    "payment_webhook_duplicates_total",
    "Payment webhooks suppressed by the idempotency guard (duplicate or gateway retry).",
    labelnames=("provider",),
)

# Summary produces quantiles natively; the alert expression in alert_rules.yml
# reads the 0.95 quantile directly.
payments_pending_age_seconds = Summary(
    "payments_pending_age_seconds",
    "Age (in seconds) of PENDING payment records at sampling time.",
)

pg_pool_size = Gauge(
    "pg_pool_size",
    "Configured SQLAlchemy connection-pool size (per worker process).",
    multiprocess_mode="liveall",
)

pg_pool_in_use = Gauge(
    "pg_pool_in_use",
    "SQLAlchemy connections currently checked out of the pool (per worker process).",
    multiprocess_mode="liveall",
)

# Data-integrity gauge sampled by the delivery monitoring Celery task. A
# "stranded" delivery is in a pool status (scheduled/pending) yet still has a
# driver assigned — invisible to both the driver active list and the pool.
stranded_deliveries = Gauge(
    "stranded_deliveries",
    "Deliveries in a pool status (scheduled/pending) that still have a driver assigned.",
    multiprocess_mode="liveall",
)


_flask_exporter: Optional[PrometheusMetrics] = None


def _update_pool_gauges() -> None:
    """Refresh pg_pool_* gauges from the live SQLAlchemy engine."""
    try:
        from business_app import db

        pool = db.engine.pool
        # QueuePool exposes `size()` (configured), `checkedout()` (in use),
        # `overflow()` (overflow connections), `checkedin()` (available).
        # NullPool / SingletonThreadPool used in tests won't have these.
        if hasattr(pool, "size") and hasattr(pool, "checkedout"):
            pg_pool_size.set(pool.size() + max(pool.overflow(), 0) if hasattr(pool, "overflow") else pool.size())
            pg_pool_in_use.set(pool.checkedout())
    except Exception:  # pragma: no cover — defensive: pool metrics are best-effort
        pass


def setup_prometheus_metrics(app) -> PrometheusMetrics:
    """Wire prometheus_flask_exporter into the Flask app.

    Registers:
    - Default request counters/histograms at /metrics (Prometheus text format)
    - A before_request hook that refreshes the pool gauges on each request so
      scrape snapshots reflect live pool state without a separate poller.

    The legacy JSON /metrics endpoint that used to live in
    business_app/utils/monitoring.py has been removed — it had no consumers
    (Prometheus rejected it as `application/json`, nginx blocks external
    /metrics, no internal caller) and Flask serves first-registered matching
    rules, so it was silently shadowing this exporter and breaking every
    `flask_*` / `payment_webhook_*` dashboard.
    """
    global _flask_exporter

    _flask_exporter = PrometheusMetrics(
        app,
        defaults_prefix="flask",  # flask_http_request_total etc — matches alert_rules.yml
        path=None,  # register the endpoint manually below so we control the route
    )

    # Expose /metrics. In multiproc mode, reads from MultiProcessCollector
    # (requires PROMETHEUS_MULTIPROC_DIR set before module import — see
    # ensure_multiproc_dir below); otherwise the default process registry.
    _flask_exporter.register_endpoint(
        "/metrics",
        app=app,
    )

    @app.before_request
    def _refresh_pool_metrics() -> None:
        _update_pool_gauges()

    return _flask_exporter


def record_webhook_received(provider: str) -> None:
    payment_webhook_total.labels(provider=provider, outcome="success").inc()


def record_webhook_failure(provider: str, reason: str) -> None:
    """Increment both the outcome-tagged total and the failures counter."""
    payment_webhook_total.labels(provider=provider, outcome=reason).inc()
    payment_webhook_failures_total.labels(provider=provider, reason=reason).inc()


def record_webhook_duplicate(provider: str) -> None:
    payment_webhook_duplicates_total.labels(provider=provider).inc()
    # Duplicate still counts as a received webhook for rate-normalised alerts.
    payment_webhook_total.labels(provider=provider, outcome="duplicate").inc()


def observe_pending_payment_age(age_seconds: float) -> None:
    """Sample a PENDING payment's age. Called by the reconciliation Celery task."""
    if age_seconds < 0:
        return
    payments_pending_age_seconds.observe(age_seconds)


def set_stranded_deliveries(count: int) -> None:
    """Set the current stranded-delivery count. Called by the delivery
    monitoring Celery task. Best-effort: metrics must never break the task."""
    try:
        stranded_deliveries.set(max(int(count), 0))
    except Exception:  # pragma: no cover — metrics are best-effort
        pass


def multiproc_collect_registry() -> CollectorRegistry:
    """Build a registry that aggregates per-worker files under PROMETHEUS_MULTIPROC_DIR.

    Callers running outside the Flask endpoint (e.g. CLI tooling, ad-hoc
    inspection) can pass this registry to generate_latest().
    """
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def ensure_multiproc_dir(app) -> None:
    """Validate PROMETHEUS_MULTIPROC_DIR at app boot.

    prometheus_client only enters multiprocess mode if this env var is set
    before Counter/Gauge definitions are imported. We can't set it here
    (too late — module already imported), but we can warn loudly if running
    under Gunicorn without it so ops notices during rollout.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    if os.environ.get("GUNICORN_WORKERS"):
        app.logger.warning(
            "PROMETHEUS_MULTIPROC_DIR not set but GUNICORN_WORKERS is — "
            "Prometheus counters will diverge across workers. "
            "Set PROMETHEUS_MULTIPROC_DIR=/tmp/prom-multiproc (or similar) "
            "before gunicorn starts."
        )
