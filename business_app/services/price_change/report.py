"""The price change's read model: notice counts, unreachable customers and granted exceptions (spec §5.2).

Reads the Task 1 tables only. The notice rows are written by the notice fan-out, the courtesy rows by the courtesy
builders and the exception grant; nothing here writes.
"""

from typing import Dict, List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import aliased

from business_app import db
from business_app.models.order import Order
from business_app.models.price_change import (
    CourtesySource,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceCourtesy,
)
from business_app.models.user import User

NOTICE_CHANNELS = ("telegram", "email")
UNREACHABLE_CHANNEL = "none"


def _notice_counts(price_change_id: int, kind: PriceChangeNoticeKind) -> Dict[str, int]:
    rows = (
        db.session.query(PriceChangeNotice.channel, func.count(PriceChangeNotice.id))
        .filter(PriceChangeNotice.price_change_id == price_change_id, PriceChangeNotice.kind == kind)
        .group_by(PriceChangeNotice.channel)
        .all()
    )
    by_channel = {channel: int(count) for channel, count in rows}
    return {
        "telegram": by_channel.get("telegram", 0),
        "email": by_channel.get("email", 0),
        "none": by_channel.get(UNREACHABLE_CHANNEL, 0),
        "queued": by_channel.get(None, 0),
    }


def price_change_counts(price_change_id: int) -> dict:
    """Notices per kind by channel (`queued` = channel not yet chosen), the automatic courtesies, how many
    courtesies an order has used (either source) and the manager exceptions."""
    courtesies = PriceCourtesy.query.filter(PriceCourtesy.price_change_id == price_change_id)
    return {
        "announcement": _notice_counts(price_change_id, PriceChangeNoticeKind.ANNOUNCEMENT),
        "reminder": _notice_counts(price_change_id, PriceChangeNoticeKind.REMINDER),
        "eligible": courtesies.filter(PriceCourtesy.source == CourtesySource.AUTOMATIC).count(),
        "courtesy_used": courtesies.filter(PriceCourtesy.order_id.isnot(None)).count(),
        "exceptions": courtesies.filter(PriceCourtesy.source == CourtesySource.MANAGER).count(),
    }


def unreachable_customers(price_change_id: int, *, page: int, per_page: int) -> Tuple[List[dict], int]:
    """The customers no announcement could reach, one page, ordered by name; with the total."""
    query = (
        db.session.query(User)
        .join(PriceChangeNotice, PriceChangeNotice.user_id == User.id)
        .filter(
            PriceChangeNotice.price_change_id == price_change_id,
            PriceChangeNotice.kind == PriceChangeNoticeKind.ANNOUNCEMENT,
            PriceChangeNotice.channel == UNREACHABLE_CHANNEL,
        )
    )
    total = query.count()
    users = query.order_by(User.first_name, User.last_name, User.id).offset((page - 1) * per_page).limit(per_page)
    return [{"user_id": user.id, "name": user.full_name, "phone": user.phone} for user in users], total


def granted_exceptions(price_change_id: int) -> List[dict]:
    """The manager exceptions, newest grant first: who, why, and the order that used it."""
    granter = aliased(User)
    rows = (
        db.session.query(PriceCourtesy, User, granter, Order)
        .join(User, User.id == PriceCourtesy.user_id)
        .outerjoin(granter, granter.id == PriceCourtesy.granted_by_id)
        .outerjoin(Order, Order.id == PriceCourtesy.order_id)
        .filter(PriceCourtesy.price_change_id == price_change_id, PriceCourtesy.source == CourtesySource.MANAGER)
        .order_by(PriceCourtesy.granted_at.desc(), PriceCourtesy.id.desc())
        .all()
    )
    return [
        {
            "id": courtesy.id,
            "user_id": customer.id,
            "customer_name": customer.full_name,
            "customer_phone": customer.phone,
            "reason": courtesy.reason,
            "granted_by_id": courtesy.granted_by_id,
            "granted_by_name": granted_by.full_name if granted_by else None,
            "granted_at": courtesy.granted_at,
            "used_order_id": courtesy.order_id,
            "used_order_number": order.order_number if order else None,
            "used_at": courtesy.used_at,
        }
        for courtesy, customer, granted_by, order in rows
    ]
