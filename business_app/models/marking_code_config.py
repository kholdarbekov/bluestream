"""Singleton DB-backed configuration for the proactive marking-code utilisation task.

A single row (``id=1``) holds the schedule and tuning knobs that used to live
only in environment variables / Flask config. The custom Celery beat scheduler
in ``business_app.tasks.db_scheduler`` reads this row at startup and exits the
beat process whenever ``schedule_version`` is bumped so the container restart
policy reloads the schedule.
"""

from __future__ import annotations

import enum
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
)
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin


class MarkingCodeScheduleType(enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    INTERVAL_DAYS = "interval_days"


class MarkingCodeTaskConfig(db.Model, TimestampMixin):
    __tablename__ = "marking_code_task_config"

    id = Column(Integer, primary_key=True)

    # Schedule -----------------------------------------------------------------
    schedule_type = Column(
        Enum(
            MarkingCodeScheduleType,
            name="marking_code_schedule_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=MarkingCodeScheduleType.DAILY,
    )
    # Used only when schedule_type == INTERVAL_DAYS.
    interval_days = Column(Integer, nullable=True)
    # Used only when schedule_type == WEEKLY. 0=Mon … 6=Sun (matches Celery crontab).
    day_of_week = Column(SmallInteger, nullable=True)
    execution_hour = Column(SmallInteger, nullable=False, default=0)
    execution_minute = Column(SmallInteger, nullable=False, default=0)
    # Bumped on every save; the DB-backed scheduler watches this column to know
    # when to exit and pick up new settings.
    schedule_version = Column(Integer, nullable=False, default=1)

    # Target sizing ------------------------------------------------------------
    target_min = Column(Integer, nullable=False, default=5)
    target_max = Column(Integer, nullable=False, default=500)
    trend_window_days = Column(Integer, nullable=False, default=7)
    runway_days = Column(Integer, nullable=False, default=1)
    safety_multiplier = Column(Numeric(precision=5, scale=2), nullable=False, default=Decimal("1.50"))

    # Thresholds ---------------------------------------------------------------
    low_water_ratio = Column(Numeric(precision=4, scale=3), nullable=False, default=Decimal("0.250"))
    # Max codes sent per ``/utilisation`` request to the Asl Belgisi (Tax
    # Committee) API. Pure transport-layer chunking — the *quantity* utilised
    # per run is computed from the sales trend (target_min/max, runway,
    # safety, trend_window). With deficit=450 and chunk_size=200 we make
    # 3 API calls (200+200+50); partial failure of one chunk keeps the
    # earlier successful chunks committed.
    asl_belgisi_utilisation_api_chunk_size = Column(Integer, nullable=False, default=200)

    # Tax Committee behaviour --------------------------------------------------
    tc_utilisation_enabled = Column(Boolean, nullable=False, default=True)
    tc_utilisation_delay_seconds = Column(Integer, nullable=False, default=120)

    # Audit --------------------------------------------------------------------
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    updated_by_user = relationship("User", foreign_keys=[updated_by_user_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "schedule_type": (self.schedule_type.value if hasattr(self.schedule_type, "value") else self.schedule_type),
            "interval_days": self.interval_days,
            "day_of_week": self.day_of_week,
            "execution_hour": self.execution_hour,
            "execution_minute": self.execution_minute,
            "schedule_version": self.schedule_version,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "trend_window_days": self.trend_window_days,
            "runway_days": self.runway_days,
            "safety_multiplier": float(self.safety_multiplier) if self.safety_multiplier is not None else None,
            "low_water_ratio": float(self.low_water_ratio) if self.low_water_ratio is not None else None,
            "asl_belgisi_utilisation_api_chunk_size": self.asl_belgisi_utilisation_api_chunk_size,
            "tc_utilisation_enabled": bool(self.tc_utilisation_enabled),
            "tc_utilisation_delay_seconds": self.tc_utilisation_delay_seconds,
            "updated_by_user_id": self.updated_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
