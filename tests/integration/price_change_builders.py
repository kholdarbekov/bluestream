"""THE price-change rows module (plan 2026-11-16, Task 13 onwards).

Price changes, their items and courtesy entries come only from here: later tasks extend this module and never start a
second one; the channel world (`price_courtesy_builders.py`) is built on it.

`make_price_change`, `announced`, `applied_change`, `window_open_now`, `courtesy_entry` and `queued` write a stated
row state and decide nothing: no eligibility rule, no switch, and no courtesy date beyond the model properties
(`effective_at`, `courtesy_ends_at`) and the `window_open_now` offsets. The list itself comes from
`build_courtesy_list`; a test that needs the switch's effect calls `apply_due_price_changes`, one that exercises a grant
calls `grant_exception`. Those six flush and never commit, so a test commits when it needs the rows to outlive a
rollback. `bottles` and `make_admin` build world rows through the shared builders, which commit.

`schedule_change` schedules a change through the real admin route (`PriceChangeService.create` decides it), and
`active_contract` is `contract_pricing_factory.make_contract` with an end date: never a second contract-row builder.

Every instant is written in UTC, the form the real writers store: SQLite drops the offset, so a Tashkent instant
written as it is would read back five hours late. Every row builder takes `db`, so it runs on `db` and `pg_db` alike.

The world (spec §5.1): announcement A = 2027-01-04 09:00 Tashkent, change on 2027-02-01, switch applied at 00:00:30
that night, N = 14 courtesy days. 18.9 L: 18,000 -> 20,000, cap 2. 10 L: 11,000 -> 12,000, cap 3.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace
from typing import Dict, Optional

from business_app import db as _db
from business_app.models.corporate import CorporateContract
from business_app.models.price_change import (
    CourtesySource,
    PriceChange,
    PriceChangeItem,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
    PriceCourtesy,
)
from business_app.models.product import ProductCategory
from business_app.models.user import User
from business_app.utils.line_money import line_money
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc_or_none
from shared.enums import UserRole
from tests.integration.contract_pricing_factory import make_contract, make_staff
from tests.integration.reorder_reminder_builders import TASHKENT, make_product


def local(y: int, m: int, d: int, h: int = 12, mi: int = 0, s: int = 0) -> datetime:
    """A Tashkent wall-clock instant, noon unless the hour is given."""
    return datetime(y, m, d, h, mi, s, tzinfo=TASHKENT)


ANNOUNCED_AT = local(2027, 1, 4, 9)
CHANGE_ON = date(2027, 2, 1)
APPLIED_AT = local(2027, 2, 1, 0, 0, 30)
IN_WINDOW = local(2027, 2, 10)
# Derived from the property, so tests/unit/test_price_change_model.py stays the one literal pin of the window.
WINDOW_CLOSED = PriceChange(change_on=CHANGE_ON, courtesy_days=14).courtesy_ends_at

# The published coming-price block of the 18.9 L (what `upcoming_price_block` answers).
BLOCK = {"new_price": 20000.0, "effective_date": "2027-02-01"}

OLD_19, NEW_19, CAP_19 = Decimal("18000.00"), Decimal("20000.00"), 2
OLD_10, NEW_10, CAP_10 = Decimal("11000.00"), Decimal("12000.00"), 3


def make_admin(db) -> User:
    """An ADMIN staff account: a change's `created_by`."""
    return make_staff(db, UserRole.ADMIN, f"+99893{uuid4().int % 10**7:07d}")


def _bottle(db, category, name: str, size: str, price: Decimal, min_order_quantity: int):
    product = make_product(db, category, name)
    product.size, product.base_price, product.min_order_quantity = size, price, min_order_quantity
    return product


def bottles(db) -> SimpleNamespace:
    """The world's products, in their own category, at their old prices: `p19` and `p10` (the two the change moves;
    minimum order 2 and 3, as on dev) and `other`, a 5 L at 9,000 that no change touches."""
    category = ProductCategory(name="Price change water", is_active=True)
    db.session.add(category)
    db.session.commit()
    world = SimpleNamespace(
        p19=_bottle(db, category, "Water 18.9 L", "19L", OLD_19, 2),
        p10=_bottle(db, category, "Water 10 L", "10L", OLD_10, 3),
        other=_bottle(db, category, "Water 5 L", "5L", Decimal("9000.00"), 1),
    )
    db.session.commit()
    return world


def make_price_change(
    db,
    items,
    *,
    created_by: User,
    announce_on: date = date(2027, 1, 4),
    change_on: date = CHANGE_ON,
    reminder_lead_days: int = 7,
    courtesy_days: int = 14,
    status: PriceChangeStatus = PriceChangeStatus.ANNOUNCED,
    announcement_started_at=ANNOUNCED_AT,
    applied_at=None,
) -> PriceChange:
    """A price change in the stated state, with `items = [(product, new_price, cap, old_price_or_None)]`. Rows only:
    no service validation, so a test can write any state the constraints allow."""
    price_change = PriceChange(
        announce_on=announce_on,
        change_on=change_on,
        reminder_lead_days=reminder_lead_days,
        courtesy_days=courtesy_days,
        status=status,
        announcement_started_at=ensure_utc_or_none(announcement_started_at),
        applied_at=ensure_utc_or_none(applied_at),
        created_by_id=created_by.id,
        items=[
            PriceChangeItem(product_id=product.id, new_price=new_price, courtesy_cap=cap, old_price=old_price)
            for product, new_price, cap, old_price in items
        ],
    )
    db.session.add(price_change)
    db.session.flush()
    return price_change


def announced(db, price_change: PriceChange, at: datetime) -> PriceChange:
    """`price_change` as its announcement leaves it: ANNOUNCED, started at `at`. Rows only: no recipient is asked and
    no notice is queued (`queued` writes one)."""
    price_change.status = PriceChangeStatus.ANNOUNCED
    price_change.announcement_started_at = ensure_utc_or_none(at)
    db.session.flush()
    return price_change


def queued(db, price_change: PriceChange, user: User, kind: str, at: datetime) -> PriceChangeNotice:
    """One `kind` notice ("announcement" or "reminder") of `price_change` owed to `user`, queued at `at` and not yet
    sent: the outbox row the fan-out writes before it publishes the send."""
    notice = PriceChangeNotice(
        price_change_id=price_change.id,
        user_id=user.id,
        kind=PriceChangeNoticeKind(kind),
        channel=None,
        queued_at=ensure_utc_or_none(at),
    )
    db.session.add(notice)
    db.session.flush()
    return notice


def applied_change(db, b: SimpleNamespace, *, created_by: User, **columns) -> PriceChange:
    """The rows the switch leaves for the world's change: APPLIED at `APPLIED_AT`, each item's `old_price` stamped,
    both bottles at their new `base_price`. `columns` override `make_price_change`'s."""
    price_change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, OLD_19), (b.p10, NEW_10, CAP_10, OLD_10)],
        created_by=created_by,
        **{"status": PriceChangeStatus.APPLIED, "applied_at": APPLIED_AT, **columns},
    )
    b.p19.base_price, b.p10.base_price = NEW_19, NEW_10
    db.session.flush()
    return price_change


def window_open_now(db, b: SimpleNamespace, *, created_by: User) -> PriceChange:
    """`applied_change` on the wall clock: changed yesterday, applied 30 s after its instant, announced 28 days before,
    so the courtesy window is open now."""
    change_on = local_date() - timedelta(days=1)
    applied_at = PriceChange(change_on=change_on).effective_at + timedelta(seconds=30)
    announcement_started_at = applied_at - timedelta(days=28)
    return applied_change(
        db,
        b,
        created_by=created_by,
        announce_on=local_date(announcement_started_at),
        change_on=change_on,
        announcement_started_at=announcement_started_at,
        applied_at=applied_at,
    )


def priced_line(
    product, quantity: int, unit_price: Decimal, *, contract_product_price_id=None, is_reward_item=False
) -> dict:
    """A line as `_price_items_for_method` hands it on: the keys `apply_courtesy` and `OrderItem.from_priced_line`
    read, with the `line_money` fields of a line without a courtesy."""
    return {
        "product_id": product.id,
        "quantity": quantity,
        "unit_price": unit_price,
        "contract_product_price_id": contract_product_price_id,
        "is_reward_item": is_reward_item,
        **line_money(unit_price, quantity)._asdict(),
    }


def entries(price_change: PriceChange) -> Dict[int, PriceCourtesy]:
    """The change's courtesy entries, keyed by their anchor account."""
    rows = PriceCourtesy.query.filter_by(price_change_id=price_change.id).order_by(PriceCourtesy.id)
    return {entry.user_id: entry for entry in rows}


def courtesy_entry(
    db,
    price_change: PriceChange,
    user: User,
    *,
    source: CourtesySource = CourtesySource.AUTOMATIC,
    reason=None,
    granted_by=None,
    order=None,
) -> PriceCourtesy:
    """One entry anchored on `user`, granted when the change applied and ending with its window. `order` marks it used
    by that order, at the window's first instant (`ck_price_courtesies_used` pairs the two). Rows only: two entries in
    one person are two calls on linked accounts."""
    entry = PriceCourtesy(
        price_change_id=price_change.id,
        user_id=user.id,
        source=source,
        reason=reason,
        granted_by_id=granted_by.id if granted_by is not None else None,
        granted_at=price_change.applied_at,
        expires_at=price_change.courtesy_ends_at,
        order_id=order.id if order is not None else None,
        used_at=price_change.applied_at if order is not None else None,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def schedule_change(client, headers, product, *, new_price=Decimal("20000"), cap=2) -> PriceChange:
    """A SCHEDULED change of `product` to `new_price`, scheduled through `POST /api/v1/admin/price-changes` with the
    body the admin modal sends: announced tomorrow, changed 29 days from today, the default reminder and courtesy."""
    today = local_date()
    response = client.post(
        "/api/v1/admin/price-changes",
        json={
            "announce_on": (today + timedelta(days=1)).isoformat(),
            "change_on": (today + timedelta(days=29)).isoformat(),
            "reminder_lead_days": 7,
            "courtesy_days": 14,
            "items": [{"product_id": product.id, "new_price": float(new_price), "courtesy_cap": cap}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return _db.session.get(PriceChange, response.get_json()["data"]["price_change"]["id"])


def active_contract(db, user, product, *, end_date: Optional[datetime] = None) -> CorporateContract:
    """`make_contract`'s contract of `user` covering `product` (in force since yesterday), ending at `end_date` (open
    when None). `user` must be a legal entity (`make_workplace`): only those count as under contract."""
    contract, _price_row = make_contract(db, user, product)
    contract.end_date = ensure_utc_or_none(end_date)
    db.session.commit()
    return contract
