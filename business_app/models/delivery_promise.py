"""Delivery promises: every arrival time the system worked out for a delivery, shown to a customer or not.

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.10 (recording and
calibration) and §4.12a (Google usage and its limits). Every CHECK, foreign key and index is named and
mirrors migration 9e7056f6124a one for one; the `id` primary key keeps Postgres's own name, as on the
sibling `reorder_reminder_events`. The vocabulary CHECKs are built with `_quoted` over the tuples below;
the migration carries frozen copies, pinned equal to these by
tests/integration/test_delivery_promise_pg_constraints.py.

- Append-only, like `ReorderReminderEvent`: a row is written once and never updated, so it has no
  `updated_at` and does not use TimestampMixin. `computed_at` is its one clock.
- One "in transit" tap writes two rows. The SHOWN row (`google_tap_v1`) is what the customer was told:
  from Google's drive time, or from our own engine's when the driver's position was too old for Google.
  The HIDDEN shadow row (`osrm_shadow_v1`) is our own engine's time for the same tap, kept to measure the
  shown promise against (§4.12a, "Accuracy data"). `shown` tells them apart.
- `(delivery_status_history_id, model_version)` is unique wherever the tap is known, so the notification
  worker re-running a tap cannot record it twice. A row that belongs to no tap is outside the index.
- `source` says where the minutes came from, or why there are none (the SOURCE_* constants of
  business_app/services/delivery_eta_service.py). A SHOWN row: `google_traffic` (minutes from Google's
  live traffic), `stale_estimate` (position too old for Google: our own engine's minutes plus an allowance),
  `google_unavailable` (Google was asked and gave nothing: no key, the day's cap spent, or Google failed),
  `stale_position` (position too old for Google, and our own engine had no real answer), `not_driver_tap`
  (someone other than the delivery's driver made the tap) or `no_destination` (the address has no
  coordinates). `google_traffic` and `stale_estimate` come with minutes (`window_end`), and only
  `google_traffic` has `expected_at`, which is Google's time. A HIDDEN shadow row: the distance matrix's own tier label
  (`osrm_selfhosted`, `cache`, ...), the straight-line label (`distance_matrix.STRAIGHT_LINE_SOURCE`,
  recorded with no time), or `error` (the matrix lookup failed). Free text, not a CHECKed vocabulary: the
  matrix owns its labels.
- Both foreign keys are plain. A promise leaves only with its order, when OrderDeletionService walks the
  foreign keys and deletes children first; nothing else deletes a delivery or a status-history row.
- The rows hold Google-derived times, which Google's terms may not allow us to store. That is an open
  question the deploy runbook sends Google (§4.12a, "Storage").
"""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, text

from business_app import db
from business_app.models.sales import _quoted
from business_app.utils.timezone_utils import get_utc_now

PROMISE_STAGES = ("waiting", "on_the_way")
PROMISE_DAY_KINDS = ("today", "tomorrow", "date", "unconfirmed")
# The two models a tap writes: the one the customer is shown, and the shadow kept to judge it by.
MODEL_GOOGLE_TAP = "google_tap_v1"
MODEL_OSRM_SHADOW = "osrm_shadow_v1"

_TAP_KNOWN = "delivery_status_history_id IS NOT NULL"


class DeliveryPromise(db.Model):
    """One arrival promise for one delivery, as the engine produced it. Append-only."""

    __tablename__ = "delivery_promises"
    __table_args__ = (
        CheckConstraint(f"stage IN ({_quoted(PROMISE_STAGES)})", name="ck_delivery_promises_stage"),
        CheckConstraint(
            f"day_kind IS NULL OR day_kind IN ({_quoted(PROMISE_DAY_KINDS)})", name="ck_delivery_promises_day_kind"
        ),
        CheckConstraint(
            "window_start IS NULL OR window_end IS NULL OR window_start <= window_end",
            name="ck_delivery_promises_window_order",
        ),
        Index("ix_delivery_promises_delivery_id_computed_at", "delivery_id", "computed_at"),
        Index(
            "uq_delivery_promises_history_model",
            "delivery_status_history_id",
            "model_version",
            unique=True,
            postgresql_where=text(_TAP_KNOWN),
            sqlite_where=text(_TAP_KNOWN),
        ),
    )

    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id", name="fk_delivery_promises_delivery_id"), nullable=False)
    # The `in_transit` tap that produced this promise; NULL for a promise no tap produced.
    delivery_status_history_id = Column(
        Integer,
        ForeignKey("delivery_status_history.id", name="fk_delivery_promises_delivery_status_history_id"),
        nullable=True,
    )
    computed_at = Column(DateTime(timezone=True), nullable=False, default=get_utc_now)
    stage = Column(String(20), nullable=False)
    expected_at = Column(DateTime(timezone=True), nullable=True)
    window_start = Column(DateTime(timezone=True), nullable=True)
    window_end = Column(DateTime(timezone=True), nullable=True)
    day_kind = Column(String(12), nullable=True)
    stops_ahead = Column(Integer, nullable=True)
    day_started = Column(Boolean, nullable=True)
    drivers_working = Column(Integer, nullable=True)
    model_version = Column(String(32), nullable=False)
    source = Column(String(40), nullable=True)
    # Whether a customer was shown this promise; the hidden shadow row of a tap stays False.
    shown = Column(Boolean, nullable=False, default=False)

    def __repr__(self):
        return f"<DeliveryPromise {self.id} delivery={self.delivery_id} {self.model_version} shown={self.shown}>"
