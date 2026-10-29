"""
Subscription Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for subscription-related data serialization
"""

from datetime import date, datetime
from typing import Any, ClassVar, Dict, FrozenSet, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, model_validator
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from business_app.models.subscription import SubscriptionItem
from business_app.serializers.types import MoneyFloat
from business_app.services.subscription_display import SKIP_REASON_CODES
from shared.enums import SubscriptionFrequency


# The fields an explicit `null` clears instead of being ignored (spec 2026-10-27 §6.1): the slot, where null is
# "Any time". Read by `pydantic_helpers.dump_changes`.
CLEARABLE_SUBSCRIPTION_FIELDS = frozenset({"delivery_time_slot_id"})


class _NoExplicitNull(BaseModel):
    """An update body that refuses an explicit `null` on the fields in `NEVER_NULL`: the service would otherwise get
    the null (or drop it silently), so `{"name": null}` is a 400 here and never a 500 later. The error is a
    `PydanticCustomError`, as `SkipDeliveryRequest`'s: its `ctx` stays JSON-serialisable."""

    NEVER_NULL: ClassVar[FrozenSet[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _refuse_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in sorted(cls.NEVER_NULL):
                if field in data and data[field] is None:
                    raise PydanticCustomError("explicit_null", "{field} may not be null", {"field": field})
        return data


class _OneFrequency(BaseModel):
    """ONE `frequency` drives a subscription (spec 2026-10-23 §2.1)."""

    frequency: SubscriptionFrequency


class CreateSubscriptionRequest(_OneFrequency):
    """Create subscription request schema"""

    name: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: int = Field(..., gt=0)
    payment_method: str
    auto_renew: bool = Field(default=True)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    items: List[Dict[str, Any]] = Field(..., min_length=1)


class UpdateSubscriptionRequest(_NoExplicitNull):
    """Update subscription request schema. An explicit `delivery_time_slot_id: null` is "Any time" (clears the slot);
    a missing key keeps it (`CLEARABLE_SUBSCRIPTION_FIELDS`)."""

    NEVER_NULL: ClassVar[FrozenSet[str]] = frozenset(
        {"name", "frequency", "delivery_address_id", "auto_renew", "payment_method"}
    )

    name: Optional[str] = Field(None, min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    # What the bot's "change frequency" sends (§4.6); the next delivery is recomputed by the rule.
    frequency: Optional[SubscriptionFrequency] = None
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: Optional[int] = Field(None, gt=0)
    # Only with `delivery_address_id`: the method chosen from the new address's menu when it does not offer the
    # current one (spec 2026-10-27 §7, B8 re-pick). Alone it is refused; `/change-payment-method` is its writer.
    payment_method: Optional[str] = None
    auto_renew: Optional[bool] = None
    # What the customer's screen showed (spec 2026-10-27 §16 M2): `allowed_actions.expected_state`, or the schedule
    # or address preview's. Required only with a schedule field or `delivery_address_id`; a stale or missing one
    # answers 409 STATE_CHANGED.
    expected_state: Optional[str] = Field(None, max_length=64)
    # Unsupported fields must answer 400, not be dropped and reported as a saved success.
    model_config = ConfigDict(extra="forbid")


class PauseSubscriptionRequest(BaseModel):
    """Pause subscription request schema. Resuming is always the customer's (or an admin's) own action:
    no client ever set an automatic resume date, so none is accepted (spec 2026-10-23 §4.7)."""

    reason: Optional[str] = Field(None, max_length=500)
    # What the customer's screen showed (spec 2026-10-27 §16 M2): `allowed_actions.expected_state`. A
    # stale or missing one answers 409 STATE_CHANGED.
    expected_state: Optional[str] = Field(None, max_length=64)


class CancelSubscriptionRequest(BaseModel):
    """Cancel subscription request schema. A cancel is always immediate (spec 2026-10-23 §4.6); the
    `immediate` flag the bot still sends is ignored."""

    reason: Optional[str] = Field(None, max_length=500)
    # What the customer's screen showed (spec 2026-10-27 §16 M2): `allowed_actions.expected_state`. A
    # stale or missing one answers 409 STATE_CHANGED.
    expected_state: Optional[str] = Field(None, max_length=64)


class AddSubscriptionItemRequest(BaseModel):
    """Add subscription item request schema"""

    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)


class UpdateSubscriptionItemRequest(BaseModel):
    """Update subscription item request schema"""

    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)


class SubscriptionPreviewRequest(_OneFrequency):
    """Subscription preview request schema: one `frequency`, and the chosen slot, whose days a daily delivery is
    counted on (spec 2026-10-27 §6.4)."""

    items: List[Dict[str, Any]] = Field(..., min_length=1)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)


class SubscriptionPreviewResponse(BaseModel):
    """Subscription preview response schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    items_total: MoneyFloat
    discount_amount: MoneyFloat
    billing_amount: MoneyFloat

    # Delivery estimates
    deliveries_per_month: int
    monthly_cost_estimate: MoneyFloat

    # Savings calculation
    regular_price_total: MoneyFloat
    total_savings: MoneyFloat
    savings_percentage: float

    # Item breakdown
    items_breakdown: List[Dict[str, Any]]


class SubscriptionStatistics(BaseModel):
    """Subscription statistics schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    total_subscriptions: int
    active_subscriptions: int
    paused_subscriptions: int
    cancelled_subscriptions: int

    # Financial metrics
    total_spent: MoneyFloat
    total_savings: MoneyFloat
    average_monthly_spending: MoneyFloat

    # Delivery metrics
    upcoming_deliveries: int
    total_deliveries_completed: int

    # Trend data
    monthly_spending_trend: Dict[str, MoneyFloat]


class SubscriptionLogSchema(BaseModel):
    """Subscription log schema"""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)

    id: int
    subscription_id: int
    action: str
    details: str
    created_at: datetime
    user_name: Optional[str] = None
    extra_data: Optional[Dict[str, Any]] = None


class SubscriptionItemLine(BaseModel):
    """One item of a whole item list: what the website's Items save and its preview send."""

    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0)
    model_config = ConfigDict(extra="forbid")


class ReplaceSubscriptionItemsRequest(BaseModel):
    """`PUT /subscriptions/<id>/items`: the whole item list, replaced in one transaction (spec 2026-10-26
    §5 B7). An empty list reaches the service, which refuses it with the "at least one product" rule."""

    items: List[SubscriptionItemLine]
    # What the customer's screen showed (spec 2026-10-27 §16 M2): the items preview's `expected_state`. A
    # stale or missing one answers 409 STATE_CHANGED.
    expected_state: Optional[str] = Field(None, max_length=64)
    model_config = ConfigDict(extra="forbid")


class SubscriptionChangePreviewRequest(BaseModel):
    """`POST /subscriptions/<id>/preview`: the change the customer is drafting, before Save (spec 2026-10-26
    §5 B4). The schedule and address fields are the PUT's; `items` is the replace-all save's list."""

    frequency: Optional[SubscriptionFrequency] = None
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    # The address change (spec 2026-10-27 §7, §17 Q6), and the method re-picked from the new address's menu.
    delivery_address_id: Optional[int] = Field(None, gt=0)
    payment_method: Optional[str] = None
    items: Optional[List[SubscriptionItemLine]] = None
    model_config = ConfigDict(extra="forbid")


class ChangePaymentMethodRequest(BaseModel):
    """Change payment method request schema"""

    payment_method: str
    # What the customer's screen showed (spec 2026-10-27 §16 M2): `allowed_actions.expected_state`. A
    # stale or missing one answers 409 STATE_CHANGED.
    expected_state: Optional[str] = Field(None, max_length=64)


class SkipReasonFields(BaseModel):
    """Why a delivery is skipped: an optional chip (`subscription_display.SKIP_REASON_CODES`), with the customer's
    own words only for "other". A stray key is a 400."""

    reason_code: Optional[Literal[SKIP_REASON_CODES]] = None
    reason_text: Optional[str] = Field(None, min_length=1, max_length=500)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _text_only_for_other(self):
        if self.reason_text is not None and self.reason_code != "other":
            # A custom error carries no exception object, so the 400 body stays JSON.
            raise PydanticCustomError("reason_text_needs_other", "reason_text goes with reason_code 'other'")
        return self


class SkipDeliveryRequest(SkipReasonFields):
    """Skip the upcoming delivery (spec 2026-10-26 §12). `expected_date` is the date the customer's button
    named; when a skip would now land elsewhere the backend answers 409 and skips nothing."""

    expected_date: date


# Export all schemas for easy importing
__all__ = [
    "CreateSubscriptionRequest",
    "UpdateSubscriptionRequest",
    "PauseSubscriptionRequest",
    "CancelSubscriptionRequest",
    "AddSubscriptionItemRequest",
    "UpdateSubscriptionItemRequest",
    "SubscriptionPreviewRequest",
    "SubscriptionPreviewResponse",
    "SubscriptionStatistics",
    "SubscriptionLogSchema",
    "ChangePaymentMethodRequest",
    "SkipDeliveryRequest",
    "SubscriptionItemLine",
    "ReplaceSubscriptionItemsRequest",
    "SubscriptionChangePreviewRequest",
]


def serialize_subscription(subscription, include_items=False, include_address=False) -> Dict[str, Any]:
    """
    Serialize subscription object to dictionary

    Args:
        subscription: Subscription model instance
        include_items: Include subscription items in serialization
        include_address: Include delivery address information

    Returns:
        Serialized subscription data
    """
    from business_app.services.subscription_service import SubscriptionService

    try:
        subscription_data = {
            "id": subscription.id,
            "user_id": subscription.user_id,
            "name": subscription.name,
            "description": getattr(subscription, "description", None),
            "status": subscription.status.value if hasattr(subscription.status, "value") else str(subscription.status),
            "billing_amount": float(subscription.billing_amount),
            "discount_percentage": getattr(subscription, "discount_percentage", 0.0),
            "delivery_day_of_week": getattr(subscription, "delivery_day_of_week", None),
            "delivery_day_of_month": getattr(subscription, "delivery_day_of_month", None),
            "delivery_time_slot_id": getattr(subscription, "delivery_time_slot_id", None),
            "delivery_time_slot": (
                subscription.delivery_time_slot.to_dict() if getattr(subscription, "delivery_time_slot", None) else None
            ),
            "payment_method": (
                subscription.payment_method.value
                if hasattr(subscription.payment_method, "value")
                else str(subscription.payment_method)
            ),
            "auto_renew": getattr(subscription, "auto_renew", True),
            "total_amount_billed": float(getattr(subscription, "total_amount_billed", 0)),
            "failed_billing_attempts": getattr(subscription, "failed_billing_attempts", 0),
            "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
            "end_date": subscription.end_date.isoformat() if getattr(subscription, "end_date", None) else None,
            "pause_start_date": (
                subscription.pause_start_date.isoformat() if getattr(subscription, "pause_start_date", None) else None
            ),
            "pause_end_date": (
                subscription.pause_end_date.isoformat() if getattr(subscription, "pause_end_date", None) else None
            ),
            # When the subscription was paused (spec 2026-10-26 §12); None unless it is paused.
            "paused_at": subscription.paused_at.isoformat() if getattr(subscription, "paused_at", None) else None,
            "created_at": subscription.created_at.isoformat(),
            "updated_at": subscription.updated_at.isoformat() if getattr(subscription, "updated_at", None) else None,
            # The ONE answer every client shows, never re-derived by a client: frequency, next/last delivery,
            # deliveries_per_month and upcoming_delivery (spec 2026-10-23 §9); what the customer may do now and
            # why not, with each action's consequences, from the guard every write also calls (spec 2026-10-26
            # §5 B1); and the page's waterline, estimate, sentences and labels (B2-B5).
            **SubscriptionService.published_fields(subscription),
        }

        if include_items and hasattr(subscription, "subscription_items"):
            subscription_data["subscription_items"] = [
                serialize_subscription_item(item) for item in subscription.subscription_items
            ]

        if include_address and hasattr(subscription, "delivery_address") and subscription.delivery_address:
            subscription_data["delivery_address"] = serialize_subscription_address(subscription.delivery_address)

        return subscription_data

    except Exception as e:
        # Fallback serialization
        return {
            "id": subscription.id,
            "user_id": subscription.user_id,
            "name": subscription.name,
            "status": str(subscription.status),
            "billing_amount": float(subscription.billing_amount),
            "created_at": subscription.created_at.isoformat(),
            "error": f"Partial serialization due to: {str(e)}",
        }


def serialize_subscription_item(item: SubscriptionItem) -> Dict[str, Any]:
    """Serialize subscription item object. `available` (spec 2026-10-26 §8.5) and the two labels (the
    current customer price, as the estimate counts it) are the page's; `unit_price` stays the stored one."""
    from business_app.services.subscription_service import SubscriptionService

    base = {
        "id": item.id,
        "subscription_id": item.subscription_id,
        "product_id": item.product_id,
        "quantity": item.quantity,
        "unit_price": float(item.unit_price),
        "total_price": float(item.total_price),
    }
    product = item.product
    try:
        # A deleted product is never delivered again, and shown as such rather than dropped (spec §8.5).
        return {
            **base,
            "product": product.to_dict() if product is not None else None,
            **SubscriptionService.line_labels(product, item.quantity),
        }
    except Exception:
        return base


def serialize_subscription_address(address) -> Dict[str, Any]:
    """Serialize subscription delivery address"""
    try:
        return {
            "id": address.id,
            "title": address.title,
            "full_address": address.full_address,
            "city": address.city,
            "district": getattr(address, "district", None),
            "postal_code": getattr(address, "postal_code", None),
            "latitude": getattr(address, "latitude", None),
            "longitude": getattr(address, "longitude", None),
            "delivery_instructions": getattr(address, "delivery_instructions", None),
        }
    except Exception:
        return {"id": address.id, "title": address.title, "full_address": address.full_address, "city": address.city}


def serialize_subscription_statistics(stats_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize subscription statistics"""
    return {
        "total_subscriptions": stats_data.get("total_subscriptions", 0),
        "active_subscriptions": stats_data.get("active_subscriptions", 0),
        "paused_subscriptions": stats_data.get("paused_subscriptions", 0),
        "cancelled_subscriptions": stats_data.get("cancelled_subscriptions", 0),
        "total_spent": float(stats_data.get("total_spent", 0)),
        "total_savings": float(stats_data.get("total_savings", 0)),
        "average_monthly_spending": float(stats_data.get("average_monthly_spending", 0)),
        "upcoming_deliveries": stats_data.get("upcoming_deliveries", 0),
        "total_deliveries_completed": stats_data.get("total_deliveries_completed", 0),
        "monthly_spending_trend": {k: float(v) for k, v in stats_data.get("monthly_spending_trend", {}).items()},
    }


def serialize_subscription_preview(preview_data: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize subscription preview data"""
    return {
        "items_total": float(preview_data.get("items_total", 0)),
        "discount_amount": float(preview_data.get("discount_amount", 0)),
        "billing_amount": float(preview_data.get("billing_amount", 0)),
        "deliveries_per_month": preview_data.get("deliveries_per_month", 0),
        "monthly_cost_estimate": float(preview_data.get("monthly_cost_estimate", 0)),
        "regular_price_total": float(preview_data.get("regular_price_total", 0)),
        "total_savings": float(preview_data.get("total_savings", 0)),
        "savings_percentage": preview_data.get("savings_percentage", 0.0),
        "items_breakdown": preview_data.get("items_breakdown", []),
    }


def serialize_subscription_log(log) -> Dict[str, Any]:
    """Serialize subscription log entry"""
    try:
        return {
            "id": log.id,
            "subscription_id": log.subscription_id,
            "action": log.action,
            "details": log.details,
            "created_at": log.created_at.isoformat(),
            "user_name": getattr(log.user, "full_name", None) if hasattr(log, "user") and log.user else None,
            "extra_data": getattr(log, "extra_data", None),
        }
    except Exception:
        return {"id": log.id, "action": log.action, "details": log.details, "created_at": log.created_at.isoformat()}


# ---------------------------------------------------------------------------
# Admin request schemas (admin can create/edit any user's subscription).
# Snake_case in, snake_case admin responses out (see business_app/api/admin.py).
# ---------------------------------------------------------------------------


class AdminCreateSubscriptionRequest(_OneFrequency):
    """Admin: create a subscription for any user."""

    user_id: int = Field(..., gt=0)
    name: str = Field(..., min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: int = Field(..., gt=0)
    payment_method: str
    auto_renew: bool = Field(default=True)
    discount_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    loyalty_points_multiplier: Optional[float] = Field(None, ge=0.0)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    items: List[Dict[str, Any]] = Field(..., min_length=1)


class AdminUpdateSubscriptionRequest(_NoExplicitNull, _OneFrequency):
    """Admin: update subscription fields, with optional warned overrides.

    All content fields are optional. The ``override_*`` flags gate the
    dangerous fields: billing_amount / the next delivery date are only applied when
    their flag is set, and a cancelled/expired subscription can only be
    edited when ``override_edit_any_status`` is true (see the service).

    An explicit ``delivery_time_slot_id: null`` is "Any time" (clears the slot). The form's nulls on the day fields,
    ``loyalty_points_multiplier`` and ``description`` stay ignored.
    """

    NEVER_NULL: ClassVar[FrozenSet[str]] = frozenset(
        {"name", "frequency", "delivery_address_id", "payment_method", "auto_renew"}
    )

    frequency: Optional[SubscriptionFrequency] = None

    name: Optional[str] = Field(None, min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    delivery_day_of_week: Optional[int] = Field(None, ge=0, le=6)
    delivery_day_of_month: Optional[int] = Field(None, ge=1, le=31)
    delivery_time_slot_id: Optional[int] = Field(None, gt=0)
    delivery_address_id: Optional[int] = Field(None, gt=0)
    payment_method: Optional[str] = None
    auto_renew: Optional[bool] = None
    discount_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    loyalty_points_multiplier: Optional[float] = Field(None, ge=0.0)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    # Override-gated fields:
    billing_amount: Optional[float] = Field(None, ge=0.0)
    # "Set next delivery date" (spec 2026-10-23 §9), checked by the schedule rule.
    next_delivery_date: Optional[date] = None
    # Override flags:
    override_edit_any_status: bool = Field(default=False)
    override_manual_billing_amount: bool = Field(default=False)
    override_manual_billing_dates: bool = Field(default=False)


class AdminAddSubscriptionItemRequest(BaseModel):
    """Admin: add an item to a subscription."""

    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)


class AdminUpdateSubscriptionItemRequest(BaseModel):
    """Admin: update a subscription item."""

    quantity: int = Field(..., gt=0)
    special_instructions: Optional[str] = Field(None, max_length=200)
