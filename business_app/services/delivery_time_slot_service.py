"""The one writer of delivery time slots (spec 2026-10-27 §6.5, §17 Q7).

Every admin slot write (create, edit, delete) comes through here, so a slot is never switched off, narrowed, retimed
or removed without the subscriptions it serves following in the same transaction: each write locks every live
subscription and then the slot, checks the days and times, reads the delivery days before it
(`subscription_schedule.delivery_weekdays`), writes, and has `SubscriptionService.reconcile_with_slots` move every
schedule the write touched. The customer notices go out after the commit (`SubscriptionService._deliver`), never from
inside it.

Locks are taken in one order, subscription before slot (`SubscriptionService.lock_live_subscriptions`): a schedule
save locks its subscription and then reads the chosen slot FOR SHARE (`SubscriptionService._chosen_slot`), so a save
and a slot write on the same slot queue one after the other instead of deadlocking, and the reconcile always sees the
subscription's committed slot. Two slot writes queue on the same subscription rows, so the second reads the delivery
days the first left.
"""

import re
from typing import Any, Dict, List

from business_app import db
from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from business_app.services import subscription_schedule
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.delivery_window import parse_window_time
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from shared.enums import LIVE_SUBSCRIPTION_STATUSES

DAYS_INVALID = "api.admin.error.time_slot_days_invalid"
TIMES_INVALID = "api.admin.error.time_slot_times_invalid"
NOT_FOUND = "api.admin.error.time_slot_not_found"
IN_USE = "api.admin.error.time_slot_in_use"

_HH_MM = re.compile(r"^\d{2}:\d{2}$")
# The fields an admin slot write may set; anything else in the body is ignored, as before.
_FIELDS = (
    "name",
    "start_time",
    "end_time",
    "is_active",
    "max_orders",
    "delivery_fee",
    "is_premium",
    "premium_fee",
    "available_days",
)
_EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]


def _valid_days(raw: Any) -> List[int]:
    """A non-empty list of weekdays 0-6 (0 = Monday), de-duplicated and sorted."""
    if not isinstance(raw, list) or not raw:
        raise ValidationError(DAYS_INVALID)
    if any(not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6 for day in raw):
        raise ValidationError(DAYS_INVALID)
    return sorted(set(raw))


def _valid_time(raw: Any) -> str:
    """ "HH:MM" that is a time of the day."""
    if not isinstance(raw, str) or not _HH_MM.match(raw):
        raise ValidationError(TIMES_INVALID)
    try:
        parse_window_time(raw)
    except ValueError:
        raise ValidationError(TIMES_INVALID) from None
    return raw


def _valid_changes(data: Dict[str, Any]) -> Dict[str, Any]:
    """The body's slot fields, with the days and times checked."""
    changes = {field: data[field] for field in _FIELDS if field in data}
    if "available_days" in changes:
        changes["available_days"] = _valid_days(changes["available_days"])
    for field in ("start_time", "end_time"):
        if field in changes:
            changes[field] = _valid_time(changes[field])
    return changes


def _locked(slot_id: int) -> DeliveryTimeSlot:
    slot = DeliveryTimeSlot.query.filter_by(id=slot_id).with_for_update().populate_existing().first()
    if slot is None:
        raise NotFoundError(NOT_FOUND)
    return slot


class DeliveryTimeSlotService:
    """Create, edit and delete a delivery time slot, each with its schedule cascade (§6.5)."""

    @staticmethod
    def create_slot(data: Dict[str, Any], *, actor_id: int) -> DeliveryTimeSlot:
        """A new slot. Its days may add delivery days: daily subscribers are told (owner, 2026-10-27)."""
        changes = _valid_changes({"is_active": True, "available_days": _EVERY_DAY, **data})
        SubscriptionService.lock_live_subscriptions()
        before_days = subscription_schedule.delivery_weekdays()
        slot = DeliveryTimeSlot(**changes)
        db.session.add(slot)
        db.session.flush()
        logs = SubscriptionService.reconcile_with_slots(before_delivery_days=before_days, actor_user_id=actor_id)
        db.session.commit()
        SubscriptionService._deliver(logs)
        return slot

    @staticmethod
    def update_slot(slot_id: int, changes: Dict[str, Any], *, actor_id: int) -> DeliveryTimeSlot:
        """Edit a slot. Switched off or narrowed: the subscriptions it no longer serves become Any time; new times:
        they keep it and are told; delivery days lost or gained move or tell the rest."""
        SubscriptionService.lock_live_subscriptions()
        slot = _locked(slot_id)
        changes = _valid_changes(changes)
        before = subscription_schedule.SlotState.of(slot)
        before_days = subscription_schedule.delivery_weekdays()
        for field, value in changes.items():
            setattr(slot, field, value)
        db.session.flush()
        logs = SubscriptionService.reconcile_with_slots(
            changed=slot, before=before, before_delivery_days=before_days, actor_user_id=actor_id
        )
        db.session.commit()
        SubscriptionService._deliver(logs)
        return slot

    @staticmethod
    def delete_slot(slot_id: int, *, actor_id: int) -> None:
        """Delete a slot no live subscription uses (a live one is refused: switch it off instead). Ended
        subscriptions let go of it; the delivery days it took away move the schedules that fell on them.

        `subscriptions.delivery_time_slot_id` is the only reference to a slot: orders keep their own window."""
        SubscriptionService.lock_live_subscriptions()
        slot = _locked(slot_id)
        users = Subscription.query.filter(Subscription.delivery_time_slot_id == slot.id)
        live = users.filter(Subscription.status.in_(LIVE_SUBSCRIPTION_STATUSES)).count()
        if live:
            raise ConflictError(IN_USE, details={"count": live})
        before_days = subscription_schedule.delivery_weekdays()
        users.update({Subscription.delivery_time_slot_id: None}, synchronize_session="fetch")
        db.session.delete(slot)
        db.session.flush()
        logs = SubscriptionService.reconcile_with_slots(before_delivery_days=before_days, actor_user_id=actor_id)
        db.session.commit()
        SubscriptionService._deliver(logs)
