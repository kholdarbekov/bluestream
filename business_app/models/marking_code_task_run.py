"""Execution ledger for the proactive marking-code utilisation tasks.

One row per task run:

* The daily fan-out (`pre_register_marking_codes_daily`) creates a *parent* row
  with ``product_id IS NULL``.
* Each per-product replenish (`replenish_marking_codes_for_product`) creates a
  *child* row with ``parent_run_id`` pointing at the daily parent (or NULL when
  triggered intra-day / manually).

The Admin UI reads this table to surface execution history, success rate, and
per-product run details — Celery's own result backend expires entries after a
short TTL and is not suitable for operational reporting.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin


class MarkingCodeRunStatus(enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class MarkingCodeTaskRun(db.Model, TimestampMixin):
    __tablename__ = "marking_code_task_runs"
    __table_args__ = (
        Index("idx_mc_task_runs_task_started", "task_name", "started_at"),
        Index("idx_mc_task_runs_product_started", "product_id", "started_at"),
        Index("idx_mc_task_runs_parent", "parent_run_id"),
        Index("idx_mc_task_runs_status_started", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True)
    task_name = Column(String(120), nullable=False, index=True)
    run_kind = Column(String(32), nullable=False, default="daily")

    parent_run_id = Column(
        Integer,
        ForeignKey("marking_code_task_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )

    status = Column(
        Enum(
            MarkingCodeRunStatus,
            name="marking_code_run_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=MarkingCodeRunStatus.RUNNING,
        index=True,
    )

    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Counters populated by the service summary.
    requested = Column(Integer, nullable=False, default=0)
    utilised = Column(Integer, nullable=False, default=0)
    skipped_invalid = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)

    pre_utilised_before = Column(Integer, nullable=True)
    pre_utilised_after = Column(Integer, nullable=True)
    target_value = Column(Integer, nullable=True)

    result_summary = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    triggered_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    parent_run = relationship(
        "MarkingCodeTaskRun",
        remote_side="MarkingCodeTaskRun.id",
        backref="child_runs",
    )
    product = relationship("Product", foreign_keys=[product_id])
    triggered_by_user = relationship("User", foreign_keys=[triggered_by_user_id])

    def to_dict(self, include_children: bool = False) -> dict:
        data = {
            "id": self.id,
            "task_name": self.task_name,
            "run_kind": self.run_kind,
            "parent_run_id": self.parent_run_id,
            "product_id": self.product_id,
            "product_name": getattr(self.product, "name", None) if self.product else None,
            "status": (self.status.value if hasattr(self.status, "value") else self.status),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "requested": self.requested,
            "utilised": self.utilised,
            "skipped_invalid": self.skipped_invalid,
            "errors": self.errors,
            "pre_utilised_before": self.pre_utilised_before,
            "pre_utilised_after": self.pre_utilised_after,
            "target_value": self.target_value,
            "result_summary": self.result_summary,
            "error_message": self.error_message,
            "triggered_by_user_id": self.triggered_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_children:
            data["children"] = [c.to_dict() for c in (self.child_runs or [])]
        return data
