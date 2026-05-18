"""Singleton service that owns the DB-backed marking-code task configuration.

The service is the only writer to ``marking_code_task_config``. It seeds the
row from env-var defaults on first read, validates updates, bumps the
``schedule_version`` whenever any schedule field changes (the custom Celery
beat scheduler watches this version), and writes a settings-change audit log
entry per save.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from celery.schedules import crontab
from flask import current_app

from business_app import db
from business_app.models.marking_code_config import (
    MarkingCodeScheduleType,
    MarkingCodeTaskConfig,
)
from business_app.models.product import Product, ProductFiscalProfile
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ValidationError


# Mapping of MarkingCodeTaskConfig column -> ProductFiscalProfile override column.
_OVERRIDE_MAP = {
    "target_min": "override_target_min",
    "target_max": "override_target_max",
    "trend_window_days": "override_trend_window_days",
    "runway_days": "override_runway_days",
    "safety_multiplier": "override_safety_multiplier",
    "low_water_ratio": "override_low_water_ratio",
    "asl_belgisi_utilisation_api_chunk_size": "override_asl_belgisi_utilisation_api_chunk_size",
}

_SCHEDULE_FIELDS = {
    "schedule_type",
    "interval_days",
    "day_of_week",
    "execution_hour",
    "execution_minute",
}

_GLOBAL_NUMERIC_FIELDS = {
    "target_min",
    "target_max",
    "trend_window_days",
    "runway_days",
    "asl_belgisi_utilisation_api_chunk_size",
    "tc_utilisation_delay_seconds",
}


class MarkingCodeConfigService:
    """Read/write access to the singleton ``MarkingCodeTaskConfig`` row."""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_config(self) -> MarkingCodeTaskConfig:
        """Return the singleton row, seeding from env-var defaults if absent."""
        row = MarkingCodeTaskConfig.query.get(1)
        if row is not None:
            return row
        return self._seed_default_row()

    def _seed_default_row(self) -> MarkingCodeTaskConfig:
        cfg = current_app.config
        row = MarkingCodeTaskConfig(
            id=1,
            schedule_type=MarkingCodeScheduleType.DAILY,
            execution_hour=0,
            execution_minute=0,
            schedule_version=1,
            target_min=int(cfg.get("MARKING_CODE_TARGET_MIN", 5) or 5),
            target_max=int(cfg.get("MARKING_CODE_TARGET_MAX", 500) or 500),
            trend_window_days=int(cfg.get("MARKING_CODE_TREND_WINDOW_DAYS", 7) or 7),
            runway_days=int(cfg.get("MARKING_CODE_RUNWAY_DAYS", 1) or 1),
            safety_multiplier=Decimal(str(cfg.get("MARKING_CODE_SAFETY_MULTIPLIER", 1.5) or 1.5)),
            low_water_ratio=Decimal(str(cfg.get("MARKING_CODE_LOW_WATER_RATIO", 0.25) or 0.25)),
            asl_belgisi_utilisation_api_chunk_size=int(cfg.get("MARKING_CODE_UTILISATION_BATCH_SIZE", 200) or 200),
            tc_utilisation_enabled=bool(cfg.get("TAX_COMMITTEE_UTILISATION_ENABLED", True)),
            tc_utilisation_delay_seconds=int(cfg.get("TAX_COMMITTEE_UTILISATION_DELAY_SECONDS", 120) or 120),
        )
        db.session.add(row)
        db.session.commit()
        return row

    def get_effective_for_product(self, product: Product) -> Dict[str, Any]:
        """Return the effective config for ``product`` (global + overrides)."""
        cfg = self.get_config()
        effective = {
            "target_min": cfg.target_min,
            "target_max": cfg.target_max,
            "trend_window_days": cfg.trend_window_days,
            "runway_days": cfg.runway_days,
            "safety_multiplier": float(cfg.safety_multiplier),
            "low_water_ratio": float(cfg.low_water_ratio),
            "asl_belgisi_utilisation_api_chunk_size": cfg.asl_belgisi_utilisation_api_chunk_size,
            "tc_utilisation_enabled": bool(cfg.tc_utilisation_enabled),
            "tc_utilisation_delay_seconds": cfg.tc_utilisation_delay_seconds,
        }
        profile: Optional[ProductFiscalProfile] = getattr(product, "fiscal_profile", None)
        if profile is None:
            return effective
        for base_key, override_attr in _OVERRIDE_MAP.items():
            value = getattr(profile, override_attr, None)
            if value is None:
                continue
            if base_key in ("safety_multiplier", "low_water_ratio"):
                effective[base_key] = float(value)
            else:
                effective[base_key] = int(value)
        return effective

    # ------------------------------------------------------------------
    # Crontab translation
    # ------------------------------------------------------------------

    def to_crontab(self, cfg: Optional[MarkingCodeTaskConfig] = None) -> crontab:
        """Translate the schedule fields on ``cfg`` into a Celery crontab."""
        cfg = cfg or self.get_config()
        hour = int(cfg.execution_hour or 0)
        minute = int(cfg.execution_minute or 0)

        if cfg.schedule_type == MarkingCodeScheduleType.DAILY:
            return crontab(hour=hour, minute=minute)
        if cfg.schedule_type == MarkingCodeScheduleType.WEEKLY:
            dow = int(cfg.day_of_week if cfg.day_of_week is not None else 1)
            return crontab(hour=hour, minute=minute, day_of_week=dow)
        if cfg.schedule_type == MarkingCodeScheduleType.INTERVAL_DAYS:
            n = int(cfg.interval_days or 1)
            n = max(1, n)
            # Every Nth day-of-month is a reasonable interpretation of
            # "every N days" without adding extra scheduler state. For N=1
            # this is equivalent to daily.
            if n == 1:
                return crontab(hour=hour, minute=minute)
            days = ",".join(str(d) for d in range(1, 32, n))
            return crontab(hour=hour, minute=minute, day_of_month=days)
        # Defensive fallback — should be unreachable thanks to enum validation.
        return crontab(hour=hour, minute=minute)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_updates(self, current: MarkingCodeTaskConfig, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce + range-check the incoming patch. Returns the cleaned dict."""
        cleaned: Dict[str, Any] = {}

        if "schedule_type" in updates:
            raw = updates["schedule_type"]
            try:
                cleaned["schedule_type"] = MarkingCodeScheduleType(raw)
            except (ValueError, TypeError):
                raise ValidationError(f"Invalid schedule_type: {raw!r}")

        if "interval_days" in updates:
            v = updates["interval_days"]
            if v is None:
                cleaned["interval_days"] = None
            else:
                v = int(v)
                if not (1 <= v <= 30):
                    raise ValidationError("interval_days must be between 1 and 30")
                cleaned["interval_days"] = v

        if "day_of_week" in updates:
            v = updates["day_of_week"]
            if v is None:
                cleaned["day_of_week"] = None
            else:
                v = int(v)
                if not (0 <= v <= 6):
                    raise ValidationError("day_of_week must be between 0 (Mon) and 6 (Sun)")
                cleaned["day_of_week"] = v

        if "execution_hour" in updates:
            v = int(updates["execution_hour"])
            if not (0 <= v <= 23):
                raise ValidationError("execution_hour must be between 0 and 23")
            cleaned["execution_hour"] = v

        if "execution_minute" in updates:
            v = int(updates["execution_minute"])
            if not (0 <= v <= 59):
                raise ValidationError("execution_minute must be between 0 and 59")
            cleaned["execution_minute"] = v

        if "target_min" in updates:
            cleaned["target_min"] = int(updates["target_min"])
        if "target_max" in updates:
            cleaned["target_max"] = int(updates["target_max"])
        if "trend_window_days" in updates:
            v = int(updates["trend_window_days"])
            if not (1 <= v <= 90):
                raise ValidationError("trend_window_days must be between 1 and 90")
            cleaned["trend_window_days"] = v
        if "runway_days" in updates:
            v = int(updates["runway_days"])
            if not (1 <= v <= 30):
                raise ValidationError("runway_days must be between 1 and 30")
            cleaned["runway_days"] = v
        if "safety_multiplier" in updates:
            v = Decimal(str(updates["safety_multiplier"]))
            if not (Decimal("0.5") <= v <= Decimal("5.0")):
                raise ValidationError("safety_multiplier must be between 0.5 and 5.0")
            cleaned["safety_multiplier"] = v
        if "low_water_ratio" in updates:
            v = Decimal(str(updates["low_water_ratio"]))
            if not (Decimal("0") < v <= Decimal("1")):
                raise ValidationError("low_water_ratio must be in (0, 1]")
            cleaned["low_water_ratio"] = v
        if "asl_belgisi_utilisation_api_chunk_size" in updates:
            v = int(updates["asl_belgisi_utilisation_api_chunk_size"])
            if not (1 <= v <= 1000):
                raise ValidationError("asl_belgisi_utilisation_api_chunk_size must be between 1 and 1000")
            cleaned["asl_belgisi_utilisation_api_chunk_size"] = v
        if "tc_utilisation_enabled" in updates:
            cleaned["tc_utilisation_enabled"] = bool(updates["tc_utilisation_enabled"])
        if "tc_utilisation_delay_seconds" in updates:
            v = int(updates["tc_utilisation_delay_seconds"])
            if not (0 <= v <= 3600):
                raise ValidationError("tc_utilisation_delay_seconds must be between 0 and 3600")
            cleaned["tc_utilisation_delay_seconds"] = v

        # Cross-field checks after merging with current values.
        merged_min = cleaned.get("target_min", current.target_min)
        merged_max = cleaned.get("target_max", current.target_max)
        if not (1 <= int(merged_min) <= int(merged_max) <= 10000):
            raise ValidationError("Require 1 ≤ target_min ≤ target_max ≤ 10000")

        merged_type = cleaned.get("schedule_type", current.schedule_type)
        if merged_type == MarkingCodeScheduleType.INTERVAL_DAYS:
            n = cleaned.get("interval_days", current.interval_days)
            if not n or int(n) < 1:
                raise ValidationError("interval_days is required when schedule_type=interval_days")
        if merged_type == MarkingCodeScheduleType.WEEKLY:
            dow = cleaned.get("day_of_week", current.day_of_week)
            if dow is None:
                raise ValidationError("day_of_week is required when schedule_type=weekly")

        return cleaned

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def update_config(
        self,
        updates: Dict[str, Any],
        actor_user_id: Optional[int] = None,
    ) -> MarkingCodeTaskConfig:
        """Apply a partial patch, bump schedule_version, and audit."""
        row = self.get_config()
        old_snapshot = row.to_dict()

        cleaned = self._validate_updates(row, updates or {})
        if not cleaned:
            return row

        schedule_changed = any(k in cleaned for k in _SCHEDULE_FIELDS)

        for key, value in cleaned.items():
            setattr(row, key, value)

        if schedule_changed:
            row.schedule_version = int(row.schedule_version or 0) + 1

        if actor_user_id is not None:
            row.updated_by_user_id = actor_user_id

        db.session.commit()

        audit_logger.log_event(
            event_type=AuditEventType.SETTINGS_CHANGED,
            action="update_marking_code_task_config",
            severity=AuditSeverity.MEDIUM,
            resource_type="marking_code_task_config",
            resource_id="1",
            old_values=old_snapshot,
            new_values=row.to_dict(),
            description="Updated marking-code task configuration",
        )
        return row

    def update_product_overrides(
        self,
        product_id: int,
        overrides: Dict[str, Any],
        actor_user_id: Optional[int] = None,
    ) -> ProductFiscalProfile:
        """Set or clear per-product override columns on ``ProductFiscalProfile``.

        A ``None`` value clears the override and falls back to the global value.
        """
        profile = ProductFiscalProfile.query.filter_by(product_id=int(product_id)).first()
        if profile is None:
            raise ValidationError(f"No fiscal profile for product_id={product_id}")

        old_snapshot = profile.to_dict()
        applied: Dict[str, Any] = {}

        for base_key, override_attr in _OVERRIDE_MAP.items():
            if base_key not in overrides:
                continue
            raw = overrides[base_key]
            if raw is None:
                setattr(profile, override_attr, None)
                applied[override_attr] = None
                continue
            if base_key in ("safety_multiplier", "low_water_ratio"):
                value = Decimal(str(raw))
                if base_key == "safety_multiplier" and not (Decimal("0.5") <= value <= Decimal("5.0")):
                    raise ValidationError("override safety_multiplier must be between 0.5 and 5.0")
                if base_key == "low_water_ratio" and not (Decimal("0") < value <= Decimal("1")):
                    raise ValidationError("override low_water_ratio must be in (0, 1]")
            else:
                value = int(raw)
                if base_key == "trend_window_days" and not (1 <= value <= 90):
                    raise ValidationError("override trend_window_days must be between 1 and 90")
                if base_key == "runway_days" and not (1 <= value <= 30):
                    raise ValidationError("override runway_days must be between 1 and 30")
                if base_key == "asl_belgisi_utilisation_api_chunk_size" and not (1 <= value <= 1000):
                    raise ValidationError("override asl_belgisi_utilisation_api_chunk_size must be between 1 and 1000")
                if base_key in ("target_min", "target_max") and not (1 <= value <= 10000):
                    raise ValidationError(f"override {base_key} must be between 1 and 10000")
            setattr(profile, override_attr, value)
            applied[override_attr] = float(value) if isinstance(value, Decimal) else value

        # Cross-field on the resulting effective min/max.
        eff_min = (
            profile.override_target_min if profile.override_target_min is not None else self.get_config().target_min
        )
        eff_max = (
            profile.override_target_max if profile.override_target_max is not None else self.get_config().target_max
        )
        if int(eff_min) > int(eff_max):
            raise ValidationError("Effective target_min must be ≤ target_max after applying overrides")

        db.session.commit()

        audit_logger.log_event(
            event_type=AuditEventType.SETTINGS_CHANGED,
            action="update_marking_code_product_overrides",
            severity=AuditSeverity.LOW,
            resource_type="product_fiscal_profile",
            resource_id=str(profile.id),
            old_values=old_snapshot,
            new_values=profile.to_dict(),
            description=f"Updated marking-code overrides for product {product_id}",
        )
        return profile
