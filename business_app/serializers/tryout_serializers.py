"""Try-out domain serializers and payload validators."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TrialContactPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    phone: str = Field(..., min_length=5, max_length=20)
    company_name: Optional[str] = Field(default=None, max_length=200)
    preferred_language: str = Field(default="uz", min_length=2, max_length=5)
    notes: Optional[str] = None


class TrialContactAddressPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: Optional[str] = Field(default=None, max_length=100)
    full_address: str = Field(..., min_length=3)
    district: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default="Tashkent", max_length=100)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    delivery_notes: Optional[str] = None
    is_default: bool = True


class TryoutItemPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: int
    quantity: int = Field(..., ge=1)


class UpdateTrialContactPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, min_length=5, max_length=20)
    company_name: Optional[str] = Field(default=None, max_length=200)
    preferred_language: Optional[str] = Field(default=None, min_length=2, max_length=5)
    notes: Optional[str] = None


class CreateTryoutPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trial_contact: TrialContactPayload
    address: TrialContactAddressPayload
    items: List[TryoutItemPayload] = Field(..., min_length=1)
    notes: Optional[str] = None
    internal_notes: Optional[str] = None
    assigned_driver_user_id: Optional[int] = None
    complete_handoff: bool = False
    return_due_at: Optional[datetime] = None


class UpdateTryoutPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trial_contact: Optional[UpdateTrialContactPayload] = None
    items: Optional[List[TryoutItemPayload]] = Field(default=None, min_length=1)
    notes: Optional[str] = None
    internal_notes: Optional[str] = None
    return_due_at: Optional[datetime] = None
    outcome: Optional[str] = None
    status: Optional[str] = None
    address: Optional[TrialContactAddressPayload] = None
    assigned_driver_user_id: Optional[int] = None
    complete_handoff: Optional[bool] = None


class CreateTryoutTaskPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_type: str
    assigned_driver_user_id: Optional[int] = None
    due_at: Optional[datetime] = None
    notes: Optional[str] = None


class PickupLinePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: int
    units: Decimal = Field(..., gt=0)


class RecordPickupPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pickups: List[PickupLinePayload] = Field(..., min_length=1)
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


class BottleAdjustmentPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: int
    units: Decimal
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


def serialize_trial_contact(contact) -> Dict[str, Any]:
    return contact.to_dict() if contact else {}


def serialize_trial_contact_address(address) -> Dict[str, Any]:
    return address.to_dict() if address else {}
