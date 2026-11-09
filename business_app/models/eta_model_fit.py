"""ETA model fits: what the weekly job learned about stop times and promise windows, one insert-only row per fit.

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.10. Plan: honest-ETA Phase 2, R5 (the
stop curve), R11 (the window buckets) and R13 (fits are insert-only). Every constraint and index is named and mirrors
migration 533fdcecfeff one for one.

- Insert-only, like `DeliveryPromise`: the weekly job (`EtaCalibration`) is the one writer and the engine's
  `current_params(now)` the reader. A row is never updated, so it has no `updated_at` and does not use TimestampMixin;
  the next fit is a new row. The engine reads the latest whose `fitted_at` has passed.
- A fit is the window of plan days it read, `[data_from, data_to)`: 05:00 local on its first plan day to 05:00 local on
  the Monday it ran (spec §6), as UTC instants. `data_to` is unique, so a re-run for the same window inserts nothing.
- `params` is the COMPLETE parameter set, so the engine reads one row: a component the fit kept is copied from the
  previous fit, or from the seeds in `shared.business_config`.
  `{"cycle_points": [[km, minutes], ...], "depot_line": [a, b],
    "window_buckets": {"<band>|started" or "<band>|not_started": {"lo": int | null, "hi": int | null, "n": int}},
    "monitoring": {"by_hour_band": {...}, "by_bottles_band": {...}}}`.
  A bucket always carries its fresh n; its lo and hi are null while n is under ETA_WINDOW_MIN_PROMISES (a thin bucket
  borrows when the window is read, R11). `monitoring` is reported, never read by the engine.
- `rows_used` holds the count of rows each component was fitted from, and `kept_previous` maps a component the fit kept
  to the reason (`{}` when it kept none).
"""

from sqlalchemy import JSON, Column, DateTime, Index, Integer, UniqueConstraint

from business_app import db


class EtaModelFit(db.Model):
    """One fit of the engine's learned parameters. Insert-only."""

    __tablename__ = "eta_model_fits"
    __table_args__ = (
        UniqueConstraint("data_to", name="uq_eta_model_fits_data_to"),
        # The engine reads the latest fit with `fitted_at <= now`.
        Index("ix_eta_model_fits_fitted_at", "fitted_at"),
    )

    id = Column(Integer, primary_key=True)
    fitted_at = Column(DateTime(timezone=True), nullable=False)
    data_from = Column(DateTime(timezone=True), nullable=False)
    data_to = Column(DateTime(timezone=True), nullable=False)
    params = Column(JSON, nullable=False)
    rows_used = Column(JSON, nullable=False)
    kept_previous = Column(JSON, nullable=False)

    def __repr__(self):
        return f"<EtaModelFit {self.id} fitted_at={self.fitted_at} data_to={self.data_to}>"
