"""Sales-agent pay: request schemas and the one converter to the wire (spec §5.1).

Pay services build RAW dicts, and `to_wire` is the one place those become JSON types. In a raw dict:
- money is `Decimal`, already whole UZS where it is pay;
- instants are `datetime`;
- local dates are `date`;
- months are already "YYYY-MM" strings (`local_windows.format_month`).

So no route or dict builder picks a float or a date format of its own. Flask sorts JSON keys, so
every display order is published as a list.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, constr, field_validator

from business_app.models.sales_pay import SalesPayPenaltyType
from business_app.serializers.sales_serializers import PageQuery, _iso, person_ref
from business_app.utils.local_windows import format_month, month_start
from shared.staff_constants import SALES_PAY_PENALTY_STATUSES, SALES_PAY_RATE_MODES


def to_wire(raw: Any) -> Any:
    """A raw read model -> JSON types: Decimal -> float, datetime -> ISO with its UTC offset,
    date -> ISO date, tuple -> list; dicts and lists recurse, everything else passes through."""
    if isinstance(raw, Decimal):
        return float(raw)
    if isinstance(raw, datetime):
        return _iso(raw)
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, dict):
        return {key: to_wire(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [to_wire(value) for value in raw]
    return raw


class _PayPayload(BaseModel):
    """Every pay request body. An unknown key is refused, never ignored, so a mistyped money key
    cannot land as a silent no-op (the `marking_code_admin.py` precedent). Bounds here are
    single-field only. Cross-field rules and minimum reason lengths belong to the services."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TierPayload(_PayPayload):
    """One tier as the plan editor sends it: its first unit and its rate. The upper bound is derived
    (`TierSchedule.bounds()`, I-29), so a `to_unit` key is refused like any unknown key. Types only:
    a schedule's limits live in `SalesPayPlanService.validate` alone, whose refusal names the tier
    and its product for the editor."""

    from_unit: int
    mode: Literal[SALES_PAY_RATE_MODES]
    value: Decimal


class ProductTiersPayload(_PayPayload):
    product_id: int
    tiers: List[TierPayload]


class GateBandPayload(_PayPayload):
    min_pct: Decimal = Field(ge=0, le=100)
    multiplier: Decimal = Field(ge=0, le=1)


class NewOutletBonusPayload(_PayPayload):
    amount: Decimal = Field(ge=0)
    window_days: int = Field(ge=1, le=365)
    prior_customer_lookback_days: int = Field(ge=0, le=730)
    min_orders_with_total: int = Field(ge=1, le=50)
    min_combined_total: Decimal = Field(ge=0)
    min_orders_any_amount: int = Field(ge=1, le=100)


# "YYYY-MM" on the wire. A named constant, not a literal inside `constr(...)`: flake8 reads a
# string in an annotation as a forward reference (F722).
_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class VersionPayload(_PayPayload):
    effective_month: constr(pattern=_MONTH_PATTERN)
    default_tiers: List[TierPayload]
    rates: List[ProductTiersPayload] = Field(default_factory=list)
    gate_bands: List[GateBandPayload] = Field(min_length=1, max_length=10)
    gate_min_visits_due: int = Field(ge=0, le=10000)
    new_outlet_bonus: NewOutletBonusPayload
    note: Optional[str] = Field(default=None, max_length=500)


class CreatePlanPayload(_PayPayload):
    name: constr(min_length=1, max_length=100)
    version: VersionPayload


# --- Months, statements, terms (Task 7: A0, A6, A7, A10, A11, A17, A18; A9's query) ---


class StartPayPayload(_PayPayload):
    """A0. `month` is parsed by `local_windows.parse_month` in the route, so a malformed month
    answers SALES_PAY_MONTH_INVALID like every other month in this API. `is_shadow` has no
    default: a trial month is a decision the admin states, never one they fall into."""

    month: constr(max_length=7)
    is_shadow: bool


class MarkPaidPayload(_PayPayload):
    paid_on: date


class DayNotePayload(_PayPayload):
    date: date
    note: Optional[str] = Field(default=None, max_length=100)


class HolidaysPayload(_PayPayload):
    """A7: the month's whole holiday list; the service checks the dates against the month."""

    days: List[DayNotePayload] = Field(max_length=31)  # required: an empty list clears, a missing key is a 400


class UnpaidDaysPayload(HolidaysPayload):
    """A10: the agent's whole unpaid-day list for the month (the same `{days}` shape as A7)."""


class AdjustmentPayload(_PayPayload):
    """A11. The sign is the meaning (a deduction is negative), so `amount` has no bound here;
    "non-zero and whole" and the minimum reason length are the service's rules (§4.11)."""

    amount: Decimal
    reason: str = Field(max_length=300)


class TermsPayload(_PayPayload):
    effective_month: constr(pattern=_MONTH_PATTERN)
    base_salary: Decimal
    plan_id: int
    note: Optional[str] = Field(default=None, max_length=500)


class EmploymentPayload(_PayPayload):
    start: date
    end: Optional[date] = None


class PayLinesQuery(PageQuery):
    """A9's query string. Not a `_PayPayload`: a query string is not a body, and `extra="forbid"`
    would refuse a harmless cache-buster. The page clamps are `PageQuery`'s, and its `_Payload`
    base is `extra="ignore"`, so an unknown query key is ignored. `kind` is not a Literal: a kind
    outside `line_kinds` simply matches no line (§5.2)."""

    kind: Optional[str] = None


# ---------------------------------------------------------------------------
# Penalties (compensation spec §5.2 A19-A26, §5.3 M1-M2)
# ---------------------------------------------------------------------------

# A20: 1-100 characters, the width of the canonical `sales_pay_penalty_types.name` column.
_PenaltyTypeName = constr(
    strip_whitespace=True, min_length=1, max_length=SalesPayPenaltyType.__table__.c.name.type.length
)


class PenaltyTypeNamesPayload(_PayPayload):
    """All three names are required (A20): agents read the type's name in their own language.
    Pinned to `PENALTY_TYPE_NAME_LANGUAGES` (tests/unit/test_sales_pay_guards.py)."""

    en: _PenaltyTypeName
    uz: _PenaltyTypeName
    ru: _PenaltyTypeName


class PenaltyTypeCreatePayload(_PayPayload):
    names: PenaltyTypeNamesPayload
    default_amount: Decimal


class PenaltyTypeUpdatePayload(_PayPayload):
    names: Optional[PenaltyTypeNamesPayload] = None
    default_amount: Optional[Decimal] = None
    is_active: Optional[bool] = None


def serialize_penalty_type(penalty_type, *, with_amount: bool) -> Dict[str, Any]:
    """A19's row for admins, or `{id, names}` for a manager's picker (M1 `types`).

    A default amount is pay, so a manager never receives one (C11).
    """
    from business_app.services.sales.pay_penalty_service import penalty_type_names

    row = {"id": penalty_type.id, "names": penalty_type_names(penalty_type)}
    if with_amount:
        row.update(default_amount=penalty_type.default_amount, is_active=penalty_type.is_active)
    return row


class ProposalPayload(_PayPayload):
    """M2's body. `extra="forbid"` (from `_PayPayload`) is what refuses a manager's `amount` (T-PEN-1).

    The texts' bounds (5-500, 5-2000) are the service's, so that both ends are refused with
    SALES_PAY_REASON_REQUIRED and its details.
    """

    agent_user_id: int
    penalty_type_id: int
    incident_date: date
    reason: str
    evidence: str


class PenaltyCreatePayload(ProposalPayload):
    """A23: M2's fields plus the admin's optional amount (the type's default when absent, I-12)."""

    amount: Optional[Decimal] = None


class PenaltyConfirmPayload(_PayPayload):
    amount: Optional[Decimal] = None


class PenaltyNotePayload(_PayPayload):
    note: str


class ProposalsQuery(PageQuery):
    """M1's query string. An empty filter from the UI's cleared select means "all"."""

    status: Optional[Literal[SALES_PAY_PENALTY_STATUSES]] = None
    agent_id: Optional[int] = None

    @field_validator("status", "agent_id", mode="before")
    @classmethod
    def _blank_is_absent(cls, value):
        return None if value == "" else value


class PenaltiesQuery(ProposalsQuery):
    """A22's query string. `month` is "YYYY-MM", parsed by `local_windows.parse_month` in the
    route, so a bad month answers SALES_PAY_MONTH_INVALID as every pay month does."""

    month: Optional[str] = None

    @field_validator("month", mode="before")
    @classmethod
    def _blank_month_is_absent(cls, value):
        return None if value == "" else value


def serialize_penalty_proposal(penalty) -> Dict[str, Any]:
    """`ProposalRow` (§5.3): EXACTLY these keys, and the base of the admin row.

    It is everything a manager may read about a penalty. The type is `{id, names}`, with no
    default amount; there is no posting month, no decision note and no cancel reason (an
    admin's free text may quote a figure, C11).
    """
    from business_app.services.sales.pay_penalty_service import penalty_type_names

    return {
        "id": penalty.id,
        "agent": {"user_id": penalty.agent_user_id, "name": penalty.agent.full_name if penalty.agent else None},
        "type": {"id": penalty.penalty_type_id, "names": penalty_type_names(penalty.penalty_type)},
        "incident_date": penalty.incident_date,
        "reason": penalty.reason,
        "evidence": penalty.evidence,
        "status": penalty.status,
        "origin": penalty.origin,
        "proposed_by": person_ref(penalty.proposed_by),
        "proposed_at": penalty.proposed_at,
        "decided_at": penalty.decided_at,
    }


def serialize_admin_penalty_row(penalty, *, now) -> Dict[str, Any]:
    """`AdminPenaltyRow` (§5.2): `ProposalRow` plus the admin-only keys.

    `target_month`, `target_is_late` and the `can_*` flags are the service's guards in check
    mode (`actions_for`). `is_late` is `SalesPayPeriodService.is_late` of the posting month
    against the incident month. `self_decided` is `is_self` over the row's three actors (§4.13).
    """
    from business_app.services.sales.pay_penalty_service import SalesPayPenaltyService, penalty_self_decided
    from business_app.services.sales.pay_period_service import SalesPayPeriodService

    actions = SalesPayPenaltyService.actions_for(penalty, now=now)
    period = penalty.period
    return {
        **serialize_penalty_proposal(penalty),
        "amount": penalty.amount,
        "default_amount": penalty.penalty_type.default_amount,
        "posting_month": format_month(period.month_start) if period is not None else None,
        "is_late": (
            SalesPayPeriodService.is_late(month_start(penalty.incident_date), period) if period is not None else False
        ),
        "target_month": actions["target_month"],
        "target_is_late": actions["target_is_late"],
        "decided_by": person_ref(penalty.decided_by),
        "decision_note": penalty.decision_note,
        "cancelled_at": penalty.cancelled_at,
        "cancel_reason": penalty.cancel_reason,
        "self_decided": penalty_self_decided(penalty),
        "can_confirm": actions["can_confirm"],
        "can_reject": actions["can_reject"],
        "can_cancel": actions["can_cancel"],
    }


# --- The agent's own pay (Task 10: S2's query) ---


class EarningsLinesQuery(BaseModel):
    """S2's query string, `?month=YYYY-MM&page=N`. A plain model like `PayLinesQuery`: a query
    string is not a body.

    `month` is parsed by `local_windows.parse_month` in the route, so a malformed month and a
    missing one (the default "") both answer SALES_PAY_MONTH_INVALID, the one code every month in
    this API answers with. The page size is the backend's (`EARNINGS_PAGE_SIZE`); the bot sends
    only the page.
    """

    month: str = ""
    # Final-review M2: an upper bound, so `page=10**19` is a 400 and never overflows the OFFSET.
    # The floor stays a clamp (page 0 reads page 1), as every other paged route here does.
    page: int = Field(default=1, le=10_000)

    @field_validator("page")
    @classmethod
    def _page_floor(cls, value: int) -> int:
        return max(1, value)
