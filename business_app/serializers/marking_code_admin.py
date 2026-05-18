"""Request body schemas for the marking-code admin endpoints.

Output shapes come straight from the ``to_dict()`` methods on the models.
The heavy validation lives in ``MarkingCodeConfigService`` (range/cross-field
checks); these schemas are only here to coerce inbound JSON to the right
Python types and reject obviously wrong payloads early.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MarkingCodeTaskConfigUpdate(BaseModel):
    """Partial update payload for the global config row."""

    model_config = ConfigDict(extra="forbid")

    schedule_type: Optional[Literal["daily", "weekly", "interval_days"]] = None
    interval_days: Optional[int] = Field(default=None, ge=1, le=30)
    day_of_week: Optional[int] = Field(default=None, ge=0, le=6)
    execution_hour: Optional[int] = Field(default=None, ge=0, le=23)
    execution_minute: Optional[int] = Field(default=None, ge=0, le=59)

    target_min: Optional[int] = Field(default=None, ge=1, le=10000)
    target_max: Optional[int] = Field(default=None, ge=1, le=10000)
    trend_window_days: Optional[int] = Field(default=None, ge=1, le=90)
    runway_days: Optional[int] = Field(default=None, ge=1, le=30)
    safety_multiplier: Optional[float] = Field(default=None, ge=0.5, le=5.0)

    low_water_ratio: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    asl_belgisi_utilisation_api_chunk_size: Optional[int] = Field(default=None, ge=1, le=1000)

    tc_utilisation_enabled: Optional[bool] = None
    tc_utilisation_delay_seconds: Optional[int] = Field(default=None, ge=0, le=3600)


class ProductMarkingCodeOverridesUpdate(BaseModel):
    """Per-product override payload. ``None`` clears an override."""

    model_config = ConfigDict(extra="forbid")

    target_min: Optional[int] = Field(default=None, ge=1, le=10000)
    target_max: Optional[int] = Field(default=None, ge=1, le=10000)
    trend_window_days: Optional[int] = Field(default=None, ge=1, le=90)
    runway_days: Optional[int] = Field(default=None, ge=1, le=30)
    safety_multiplier: Optional[float] = Field(default=None, ge=0.5, le=5.0)
    low_water_ratio: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    asl_belgisi_utilisation_api_chunk_size: Optional[int] = Field(default=None, ge=1, le=1000)


class MarkingCodeTaskRunTrigger(BaseModel):
    """Manual-run request body."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["all", "product"]
    product_id: Optional[int] = Field(default=None, ge=1)
