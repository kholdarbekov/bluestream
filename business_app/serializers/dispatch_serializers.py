"""Request bodies for the dispatch write endpoints."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class SetStopOrderRequest(BaseModel):
    ordered_delivery_ids: List[int] = Field(..., min_length=0)
    # {"<delivery_id>": <0-based position>} — keys arrive as JSON object keys,
    # i.e. strings, and are stored that way.
    pinned: Dict[str, int] = Field(default_factory=dict)
    # What the admin's screen was showing. Absence is not "no opinion" — it is a
    # save with no staleness protection, so it is required.
    expected_delivery_ids: List[int]

    @field_validator("ordered_delivery_ids")
    @classmethod
    def _no_duplicates(cls, value: List[int]) -> List[int]:
        if len(set(value)) != len(value):
            raise ValueError("ordered_delivery_ids contains duplicates")
        return value


class AssignStopRequest(BaseModel):
    driver_id: int
    position: Optional[int] = None


class UnassignStopRequest(BaseModel):
    reason: Optional[str] = None
