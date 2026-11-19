"""Response shapes of the prepayment return routes (spec 2026-11-10).

Money is ``MoneyFloat`` (Decimal in, float out). Date-times are written in UTC with a ``Z``: a value
read back from a database that drops the offset is UTC, the way every writer stores it.
"""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

from pydantic import AfterValidator, BaseModel, Field

from business_app.serializers.types import MoneyFloat
from business_app.utils.timezone_utils import ensure_utc

UtcDateTime = Annotated[datetime, AfterValidator(ensure_utc)]


class DrawnCreditRow(BaseModel):
    """The part of one credit event a return draws, and how that credit is classified (P7, P9)."""

    event_id: int
    customer_id: int
    amount: MoneyFloat
    revenue_counted: bool
    card_click: bool


class PendingOrderWarning(BaseModel):
    """An order a return leaves alone: not yet delivered, or holding credit that is already out of the free balance."""

    order_id: int
    order_number: str
    status: str
    held_amount: MoneyFloat


class LiveSubscriptionWarning(BaseModel):
    """A subscription that still supplies the customer (its next order may need the credit)."""

    subscription_id: int
    status: str


class PlaceDebtWarning(BaseModel):
    """Open delivered COD debt at a place the customer shares. Info only: credit never pools across a place."""

    place_group_id: int
    label: Optional[str] = None
    amount: MoneyFloat


class PayoutWarnings(BaseModel):
    """What the admin should know before returning money. Never blocks."""

    pending_orders: List[PendingOrderWarning] = Field(default_factory=list)
    live_subscriptions: List[LiveSubscriptionWarning] = Field(default_factory=list)
    place_debts: List[PlaceDebtWarning] = Field(default_factory=list)


class SessionEffect(BaseModel):
    """What a return "from his current trip's cash" does to the driver's open cash session (spec §4.3).

    ``cap`` is the trip's "Hand in now" (on hand minus handed in minus ``expenses_waiting``, unclamped); a return
    above it is blocked. ``expenses_waiting`` is the claims still waiting on the trip, which is why the cap can
    be lower than on hand minus handed in: a return cannot eat into a claim the office has yet to decide.
    ``will_close`` is the session close rule applied to the on-hand figure after the return
    (``DriverReconciliationService.session_closes``).
    """

    session_id: int
    status: str
    on_hand_before: MoneyFloat
    handed_in: MoneyFloat
    cap: MoneyFloat
    expenses_waiting: MoneyFloat
    on_hand_after: MoneyFloat
    will_close: bool


class ContractEffect(BaseModel):
    """What a return posts on a grocery store's money (AMOUNT-mode) contract that mirrored the credit it draws:
    ``amount`` onto the contract's outstanding, so the contract credit shrinks by the money handed back."""

    contract_id: int
    contract_number: str
    name: str
    amount: MoneyFloat


class PayoutPlanSummary(BaseModel):
    """The preview: exactly what an apply with the same inputs would write, and what blocks it."""

    customer_id: int
    pool_user_ids: List[int]
    free_balance: MoneyFloat
    amount: MoneyFloat
    target_balance: Optional[MoneyFloat] = None
    paid_by: str
    driver_user_id: Optional[int] = None
    paid_at: UtcDateTime
    drawn: List[DrawnCreditRow]
    card_click_amount: MoneyFloat
    revenue_counted_amount: MoneyFloat
    warnings: PayoutWarnings
    session_effect: Optional[SessionEffect] = None
    contract_effect: List[ContractEffect] = Field(default_factory=list)
    blocking_reasons: List[str]
    is_applicable: bool


class PayoutDebitRow(BaseModel):
    event_id: int
    amount: MoneyFloat


class PayoutRow(BaseModel):
    """One recorded return. Its card/Click and revenue totals are sums of its debit rows."""

    id: int
    customer_id: int
    amount: MoneyFloat
    paid_by: str
    driver_user_id: Optional[int] = None
    driver_name: Optional[str] = None
    driver_cash_session_id: Optional[int] = None
    paid_at: UtcDateTime
    reason: str
    actor_user_id: int
    actor_name: Optional[str] = None
    created_at: Optional[UtcDateTime] = None
    voided_at: Optional[UtcDateTime] = None
    voided_by_user_id: Optional[int] = None
    void_reason: Optional[str] = None
    card_click_amount: MoneyFloat
    revenue_counted_amount: MoneyFloat
    can_undo: bool
    undo_block_code: Optional[str] = None
    debits: List[PayoutDebitRow]


class PayoutUndoResponse(BaseModel):
    """The undo route's answer: the payout (now void), the balance left and its funding session, if any.

    ``driver_cash_session`` is the funding session (a return from the driver's current trip cash) as the
    reconciliation screens serialize it.
    """

    payout: PayoutRow
    available_prepayment_balance: MoneyFloat
    driver_cash_session: Optional[Dict[str, Any]] = None


class PayoutApplyResponse(PayoutUndoResponse):
    """The apply route's answer: the same three fields, as the undo answers them. A replay of a key already
    used answers the original payout.

    ``next_session_id`` is the driver's new session when the return closed the funding session.
    """

    next_session_id: Optional[int] = None
    replayed: bool
