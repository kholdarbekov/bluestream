"""The reorder-reminder rule: one pure function (spec §5).

`evaluate` is the ONLY expression of "is this customer lapsed, may we message them, and how".
The daily run, the send-time re-check and the admin page all call it; no JS, bot or second
Python site re-derives any part of it. It reads only the frozen facts `facts.py` loads and never
touches the database, so every branch is table-testable.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence, Tuple

from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders.config import ReminderConfig
from business_app.services.reorder_reminders.facts import LiveOrderFacts, MemberFacts, ReminderCustomer
from business_app.utils.delivery_cadence import typical_interval_days
from business_app.utils.local_windows import local_date
from shared.enums import UserStatus

PATH_CUSTOMER, PATH_OUTLET, PATH_NONE = "customer", "outlet", "none"
REASON_STOPPED, REASON_BLOCKED, REASON_NO_CHANNEL = "stopped", "blocked", "no_channel"


@dataclass(frozen=True)
class ChannelDecision:
    channel: Optional[str]
    reason: Optional[str]


@dataclass(frozen=True)
class Evaluation:
    path: str
    in_scope: bool
    out_of_scope_reason: Optional[str]
    recipient: Optional[MemberFacts]
    own_anchor: Optional[Tuple[int, datetime]]
    supply: Optional[Tuple[int, datetime]]
    usual_gap: Optional[int]
    threshold: Decimal
    due_on: Optional[date]
    due: bool
    fresh: bool
    paused: bool
    own_live_all_stuck: bool
    channel: ChannelDecision
    contact_restriction: Optional[dict]


def days_since(supply_instant: datetime, today: date) -> int:
    """Whole LOCAL calendar days from the supply delivery's day to `today` (contract amendment 8).

    The ONE "days since" rule: the due test in `evaluate`, the Telegram payload, the email and the
    worklist all read it, so the copy's "N days" and the ladder never disagree. A naive instant is
    UTC (how SQLite returns a DateTime).
    """
    return (today - local_date(supply_instant)).days


def is_in_progress(order: LiveOrderFacts, *, today: date, grace_days: int) -> bool:
    """D12: is this live order plausibly on its way? Anything else is stuck.

    Not when it waits for a new date after a failed delivery. Otherwise yes while it is at most
    `grace_days` local days old, or dated today or later. A plain date test on purpose: not
    `is_awaiting_release`, which answers "held back from drivers?" and turns false on the delivery
    day itself.
    """
    if order.awaiting_new_date:
        return False
    if (today - order.created_on).days <= grace_days:
        return True
    return order.delivery_date is not None and order.delivery_date >= today


def channel_for(customer: ReminderCustomer, recipient: Optional[MemberFacts]) -> ChannelDecision:
    """§7.1 item 3, in order: customer-off -> none (`stopped`); the recipient blocked the bot ->
    none (`blocked`; by the §4 ranking only when every member did, D19); else the shared
    Telegram-else-real-email resolver applied to the recipient; nothing -> none (`no_channel`)."""
    if customer.setting.customer_off:
        return ChannelDecision(None, REASON_STOPPED)
    if recipient is None:
        return ChannelDecision(None, REASON_NO_CHANNEL)
    if recipient.telegram_blocked_at is not None:
        return ChannelDecision(None, REASON_BLOCKED)
    channels = NotificationService._resolve_telegram_else_email(recipient)
    if not channels:
        return ChannelDecision(None, REASON_NO_CHANNEL)
    return ChannelDecision(channels[0].value, None)


def evaluate(customer: ReminderCustomer, *, today: date, cfg: ReminderConfig) -> Evaluation:
    """The whole rule for one customer on one local day (spec §4, §5)."""
    recipient = CustomerLinkService.notification_recipient(customer.members)
    supply = customer.supply
    instants = [instant for _order_id, instant in customer.own_deliveries]
    usual_gap = typical_interval_days(instants, min_days=cfg.min_deliveries) if instants else None
    threshold = cfg.ratio * usual_gap if usual_gap is not None else Decimal(cfg.fallback_days)
    # Lapsed once the days since the supply delivery exceed the threshold (§4); `due_on` is the
    # first such day: supply day + floor(threshold) + 1.
    due_on = local_date(supply[1]) + timedelta(days=int(threshold) + 1) if supply is not None else None
    due = supply is not None and days_since(supply[1], today) > threshold
    fresh = due and (today - due_on).days <= cfg.fresh_days
    paused = any(
        is_in_progress(order, today=today, grace_days=cfg.live_order_grace_days) for order in customer.live_orders
    )
    own_live = [order for order in customer.live_orders if order.is_own]
    path, reason = _scope(customer, recipient)
    return Evaluation(
        path=path,
        in_scope=path == PATH_CUSTOMER,
        out_of_scope_reason=reason,
        recipient=recipient,
        own_anchor=customer.own_anchor,
        supply=supply,
        usual_gap=usual_gap,
        threshold=threshold,
        due_on=due_on,
        due=due,
        fresh=fresh,
        paused=paused,
        own_live_all_stuck=bool(own_live) and not paused,
        channel=channel_for(customer, recipient),
        contact_restriction=_contact_restriction(customer, recipient),
    )


def _scope(customer: ReminderCustomer, recipient: Optional[MemberFacts]) -> Tuple[str, Optional[str]]:
    """E3, E6, E4, E1/E2, then E5 (spec §5). An outlet account takes the outlet path (D21) only
    when E1, E4 and E6 still hold."""
    if not customer.own_deliveries:
        return PATH_NONE, "no_deliveries"
    if customer.setting.admin_off:
        return PATH_NONE, "admin_off"
    if customer.has_live_subscription:
        return PATH_NONE, "subscription"
    if recipient is None:
        return PATH_NONE, _no_recipient_reason(customer.members)
    if customer.has_any_outlet:
        return PATH_OUTLET, "outlet"
    return PATH_CUSTOMER, None


def _no_recipient_reason(members: Sequence[MemberFacts]) -> str:
    if members and all(member.is_staff for member in members):
        return "staff"
    if not any(member.status == UserStatus.ACTIVE.value for member in members if not member.is_staff):
        return "inactive"
    return "no_recipient"


def _contact_restriction(customer: ReminderCustomer, recipient: Optional[MemberFacts]) -> Optional[dict]:
    """The worklist banner (§9.1), from the CURRENT setting and block state, never from the flag."""
    setting = customer.setting
    if setting.customer_off:
        return {
            "kind": REASON_STOPPED,
            "since": setting.changed_at.isoformat() if setting.changed_at else None,
            "via": setting.changed_via,
        }
    if recipient is not None and recipient.telegram_blocked_at is not None:
        return {"kind": REASON_BLOCKED, "since": recipient.telegram_blocked_at.isoformat(), "via": None}
    return None
