"""Account merges carry the price-change rows (spec 2026-11-16, merges).

`price_courtesies.user_id`, `price_change_notices.user_id` and `price_changes.created_by_id` are ON DELETE
RESTRICT, so the merge must re-point them before it deletes the secondary user. The two actor stamps that are
SET NULL (`cancelled_by_id`, `granted_by_id`) are re-pointed too, so a row keeps naming the survivor.
"""

import logging

from business_app import db
from business_app.models.price_change import PriceChange, PriceChangeNotice, PriceCourtesy

logger = logging.getLogger(__name__)


def on_users_merged(primary_id: int, secondary_id: int) -> None:
    """Move the secondary's price-change rows to the survivor. Flushes, never commits."""
    _repoint_actor_stamps(primary_id, secondary_id)
    _merge_courtesies(primary_id, secondary_id)
    _merge_notices(primary_id, secondary_id)
    db.session.flush()


def _repoint_actor_stamps(primary_id: int, secondary_id: int) -> None:
    for column in (PriceChange.created_by_id, PriceChange.cancelled_by_id):
        PriceChange.query.filter(column == secondary_id).update({column.key: primary_id}, synchronize_session=False)
    PriceCourtesy.query.filter(PriceCourtesy.granted_by_id == secondary_id).update(
        {"granted_by_id": primary_id}, synchronize_session=False
    )


def _merge_courtesies(primary_id: int, secondary_id: int) -> None:
    """One entry per (change, person): a used entry outlives an unused one, otherwise the primary's stays."""
    primary_by_change = {
        entry.price_change_id: entry for entry in PriceCourtesy.query.filter_by(user_id=primary_id).all()
    }
    moving = []
    for entry in PriceCourtesy.query.filter_by(user_id=secondary_id).order_by(PriceCourtesy.id).all():
        kept = primary_by_change.get(entry.price_change_id)
        if kept is None:
            moving.append(entry)
        elif entry.order_id is not None and kept.order_id is None:
            db.session.delete(kept)
            moving.append(entry)
        else:
            if entry.order_id is not None:
                logger.warning(
                    "Merge %s into %s: two used courtesies for price change %s; kept entry %s (order %s), "
                    "dropped entry %s (order %s)",
                    secondary_id,
                    primary_id,
                    entry.price_change_id,
                    kept.id,
                    kept.order_id,
                    entry.id,
                    entry.order_id,
                )
            db.session.delete(entry)
    # The deletes go first: uq_price_courtesies_change_user would refuse the re-point otherwise.
    db.session.flush()
    for entry in moving:
        entry.user_id = primary_id


def _merge_notices(primary_id: int, secondary_id: int) -> None:
    primary_keys = {
        (notice.price_change_id, notice.kind) for notice in PriceChangeNotice.query.filter_by(user_id=primary_id).all()
    }
    for notice in PriceChangeNotice.query.filter_by(user_id=secondary_id).all():
        if (notice.price_change_id, notice.kind) in primary_keys:
            db.session.delete(notice)
        else:
            notice.user_id = primary_id
