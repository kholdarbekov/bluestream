"""Serializers for returnable bottle tracking models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel

from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION


# ------------------------------------------------------------------
# Pydantic request models
# ------------------------------------------------------------------


# The config EVERY bottle body that carries a `float` reads — one name, so the
# refusal cannot be half-applied.
#
# `allow_inf_nan: False` is NOT boilerplate. Python's own `json` parser accepts
# the BARE `NaN` / `Infinity` / `-Infinity` literals, and pydantic's default
# `float` happily carries them through — so without this a non-finite bottle
# quantity/amount reaches the DATABASE, where Postgres `numeric` ACCEPTS 'NaN'
# and the place's stored balance is poisoned permanently (`reconcile_balance`
# re-writes the same poison, because the ledger sum is non-finite too). This is
# the request boundary; `BottleTrackingService._as_decimal` carries the same
# refusal as the SSOT backstop for every non-HTTP caller.
#
# It was ONE `model_config` short of covering the shape it was written for: the
# three admin place-writes had it and `BottleCollectionRequest` — the same
# `quantity: float`, the driver's doorstep pickup — did not. Named for the
# FIELD rather than for the admin route so the next float body reaches for it,
# and swept by tests/unit/test_bottle_request_bodies_refuse_non_finite.py so a
# body that forgets is a failure rather than a reading exercise.
_FINITE_QUANTITY_CONFIG = {
    "alias_generator": to_camel,
    "populate_by_name": True,
    "allow_inf_nan": False,
}


# `user_id` is OPTIONAL on all three admin place-write bodies below: an admin
# adjusts a PLACE, not a member (there is no coworker selection anywhere), so
# the service derives the audit attribution from the place's representative
# address. It stays accepted — not forbidden — for callers that still name one.
class BottleAdjustmentRequest(BaseModel):
    user_id: Optional[int] = None
    address_id: int
    adjustment: float
    notes: str

    model_config = _FINITE_QUANTITY_CONFIG


class BottleInitialBalanceRequest(BaseModel):
    user_id: Optional[int] = None
    address_id: int
    quantity: float
    notes: Optional[str] = None

    model_config = _FINITE_QUANTITY_CONFIG


class BottleFineCreateRequest(BaseModel):
    user_id: Optional[int] = None
    address_id: int
    quantity: float
    fine_amount: float
    notes: Optional[str] = None

    model_config = _FINITE_QUANTITY_CONFIG


class BottleFineUpdateRequest(BaseModel):
    action: str = Field(..., pattern=r"^(waive|mark_paid)$")
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class BottleCollectionRequest(BaseModel):
    # A float quantity, so it reads the shared config for the same reason the
    # place-writes above do — a bare `NaN` / `Infinity` off the wire must not
    # reach `BottleTrackingService.record_standalone_collection`.
    customer_id: int
    address_id: int
    quantity: float
    notes: Optional[str] = None

    model_config = _FINITE_QUANTITY_CONFIG


class DriverBottleSessionOpenRequest(BaseModel):
    # `le=` is the SAME rule the staff bot enforces at the keypad, read from the
    # same name — the bot's refusal is only advice until the boundary carries
    # it too. Without it a direct API call, a replayed request or any future
    # client puts an unbounded count on `DriverBottleSession.bottles_loaded`, a
    # 4-byte PostgreSQL integer, and the write dies as a DataError 500.
    bottles_loaded: int = Field(..., gt=0, le=MAX_BOTTLES_PER_SESSION)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class DriverBottleSessionCloseRequest(BaseModel):
    # `le=` is a STORAGE bound, not a business one — see
    # `BOTTLE_RETURN_COLUMN_CEILING`. Over-returning is legitimate here (the
    # load plus every empty collected at a door comes back through this field),
    # so nothing plausible is refused; what is refused is a count
    # `DriverBottleSession.bottles_returned_to_warehouse` — a 4-byte PostgreSQL
    # integer — cannot hold, which the bot answers at the keypad and which
    # otherwise reaches a direct API caller as a DataError 500.
    bottles_returned_to_warehouse: int = Field(..., ge=0, le=BOTTLE_RETURN_COLUMN_CEILING)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class AdminForceCloseSessionRequest(BaseModel):
    # Same column, same width, same bound: an admin force-closing an abandoned
    # session writes `bottles_returned_to_warehouse` too, and leaving this one
    # unbounded would keep the 500 alive for the admin UI after the driver's
    # path was fixed.
    bottles_returned_to_warehouse: int = Field(default=0, ge=0, le=BOTTLE_RETURN_COLUMN_CEILING)
    reason: str

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class DriverBottleTransferCreateRequest(BaseModel):
    receiver_driver_id: int
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class DriverBottleTransferConfirmRequest(BaseModel):
    confirmed_quantity: int = Field(..., ge=0)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class AdminResolveTransferRequest(BaseModel):
    resolved_quantity: int = Field(..., ge=0)
    resolution_notes: str

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


# ------------------------------------------------------------------
# Serializer functions
# ------------------------------------------------------------------


def serialize_bottle_balance(balance, include_user: bool = False) -> Dict[str, Any]:
    """Serialize a place's BottleBalance.

    A place row has no single owner, so identity is `place_label` plus
    `member_names` rather than a user. `include_user` is retained for call-site
    compatibility and ignored.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    data = balance.to_dict()
    data["place_label"] = BottleTrackingService._scope_label(balance)
    data["member_names"] = BottleTrackingService._scope_member_names(balance)
    data["is_shared_place"] = balance.address_group_id is not None
    if balance.address:
        data["address_title"] = balance.address.title
        data["full_address"] = balance.address.full_address

    member_ids = BottleTrackingService._scope_member_address_ids(balance)
    data["member_address_ids"] = member_ids
    data["representative_address_id"] = member_ids[0] if member_ids else None
    return data


def serialize_bottle_balance_list(balances: List, include_user: bool = True) -> List[Dict[str, Any]]:
    """Serialize a list of BottleBalance records."""
    return [serialize_bottle_balance(b, include_user=include_user) for b in balances]


def serialize_bottle_fine_row(fine) -> Dict[str, Any]:
    """Admin-facing fine row: names instead of raw ids.

    `place_label` resolves the fine's FROZEN scope (`address_group_id` /
    `address_id` as recorded at issue time — never re-resolved, see
    `BottleTrackingService._fine_scope`) to its balance row and labels it the
    same way a balance row is labeled elsewhere. If that place has no balance
    row (yet), falls back to the fine's own address title.
    """
    from business_app.models.bottle import BottleBalance
    from business_app.services.bottle_tracking_service import BottleTrackingService

    data = fine.to_dict()
    data["user_name"] = f"{fine.user.first_name or ''} {fine.user.last_name or ''}".strip() if fine.user else None
    data["address_title"] = fine.address.title if fine.address else None

    scope = BottleTrackingService._fine_scope(fine)
    place_row = BottleBalance.query.filter(*scope.balance_filter()).first()
    data["place_label"] = (
        BottleTrackingService._scope_label(place_row) if place_row is not None else data["address_title"]
    )
    return data


def serialize_bottle_ledger_entry(entry) -> Dict[str, Any]:
    """Serialize a BottleLedger entry with actor details."""
    data = entry.to_dict()

    if entry.actor_user:
        data["actor_name"] = f"{entry.actor_user.first_name or ''} {entry.actor_user.last_name or ''}".strip()

    if entry.user:
        data["user_name"] = f"{entry.user.first_name or ''} {entry.user.last_name or ''}".strip()
        data["user_phone"] = entry.user.phone

    if entry.address:
        data["address_title"] = entry.address.title
        data["full_address"] = entry.address.full_address

    return data


def serialize_customer_place_ledger_entry(entry, viewer_user_id: int) -> Dict[str, Any]:
    """Customer-facing place-ledger row (spec §7): member names only — no
    phones, no internal actor ids, no idempotency/metadata/notes/balance
    internals. `is_own` lets the bot mark the viewer's own entries.

    PLACE-LEVEL CORRECTIONS ARE NOT ATTRIBUTED TO A MEMBER (spec §7.4). A
    `merge_correction` / `merge_backfill` entry carries a member's
    `(user_id, address_id)` only because `bottle_ledger` requires both NOT
    NULL; it describes the PLACE. This view deliberately suppresses `notes`,
    so leaving the attribution in place would show one coworker an unexplained
    +/-N flagged `is_own` — a number they did not cause and cannot account for.
    `member_name` is dropped and `is_own` is forced False for those.

    `merge_exclude` reversals are attributed correctly (to the very entry they
    neutralise) and are deliberately left alone.

    NO new key is emitted for this. The row shape is pinned as a WHITELIST by
    `tests/unit/test_customer_place_ledger_gate.py` — a redaction fence from an
    earlier task — and widening it to advertise "this is a place-level row"
    would weaken that fence for a label the renderer does not need: with
    `member_name` None and `is_own` False, `telegram_bot/handlers/bottles.py`
    already prints the line with no attribution at all.
    """
    from business_app.services.bottle_tracking_service import PLACE_LEVEL_LEDGER_SOURCES

    is_place_level = (entry.entry_metadata or {}).get("source") in PLACE_LEVEL_LEDGER_SOURCES
    member_name = None
    if entry.user and not is_place_level:
        member_name = f"{entry.user.first_name or ''} {entry.user.last_name or ''}".strip() or None
    return {
        "id": entry.id,
        "address_id": entry.address_id,
        "event_type": entry.event_type.value if hasattr(entry.event_type, "value") else entry.event_type,
        "quantity": float(entry.quantity or 0),
        "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at else None,
        "order_id": entry.order_id,
        "order_number": entry.order.order_number if entry.order else None,
        "member_name": member_name,
        "is_own": (not is_place_level) and entry.user_id == viewer_user_id,
    }


def serialize_bottle_session(
    session,
    *,
    include_orders: bool = False,
    include_transfers: bool = False,
    include_members: bool = False,
) -> Optional[Dict[str, Any]]:
    """Serialize a DriverBottleSession with driver info and optional relations."""
    if session is None:
        return None

    data = session.to_dict()

    if session.driver:
        data["driver_name"] = f"{session.driver.first_name or ''} {session.driver.last_name or ''}".strip()
        data["driver_phone"] = session.driver.phone

    if include_orders:
        orders_out = []
        for so in session.session_orders or []:
            o = so.order
            if o:
                accepted_by = so.accepted_by_driver
                entry = {
                    "order_id": so.order_id,
                    "order_number": o.order_number,
                    "customer_name": o.user.full_name if o.user else None,
                    "status": o.status.value if o.status else None,
                    "total_amount": float(o.total_amount) if o.total_amount else None,
                    "accepted_by_driver_id": so.accepted_by_driver_id,
                    "accepted_by_driver_name": (
                        f"{accepted_by.first_name or ''} {accepted_by.last_name or ''}".strip() if accepted_by else None
                    ),
                    "items": [
                        {
                            "product_name": item.product.name if item.product else None,
                            "quantity": item.quantity,
                        }
                        for item in (o.order_items or [])
                    ],
                    "added_at": so.added_at.isoformat() if so.added_at else None,
                }
                orders_out.append(entry)
            else:
                orders_out.append(
                    {
                        "order_id": so.order_id,
                        "accepted_by_driver_id": so.accepted_by_driver_id,
                        "added_at": so.added_at.isoformat() if so.added_at else None,
                    }
                )
        data["orders"] = orders_out

    if include_transfers:
        data["transfers_out"] = [serialize_bottle_transfer(t) for t in (session.transfers_out or [])]
        data["transfers_in"] = [serialize_bottle_transfer(t) for t in (session.transfers_in or [])]

    if include_members:
        members_out = []
        for m in session.memberships or []:
            member_driver = m.member_driver
            member_name = (
                f"{member_driver.first_name or ''} {member_driver.last_name or ''}".strip() if member_driver else None
            )
            members_out.append(
                {
                    "membership_id": m.id,
                    "member_driver_id": m.member_driver_id,
                    "member_name": member_name,
                    "member_phone": member_driver.phone if member_driver else None,
                    "status": m.status.value if hasattr(m.status, "value") else m.status,
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                    "left_at": m.left_at.isoformat() if m.left_at else None,
                }
            )
        data["members"] = members_out

    return data


def serialize_bottle_transfer(transfer) -> Dict[str, Any]:
    """Serialize a DriverBottleTransfer with driver names."""
    data = transfer.to_dict()

    if transfer.sender_driver:
        data["sender_name"] = (
            f"{transfer.sender_driver.first_name or ''} {transfer.sender_driver.last_name or ''}".strip()
        )
        data["sender_phone"] = transfer.sender_driver.phone

    if transfer.receiver_driver:
        data["receiver_name"] = (
            f"{transfer.receiver_driver.first_name or ''} {transfer.receiver_driver.last_name or ''}".strip()
        )
        data["receiver_phone"] = transfer.receiver_driver.phone

    return data


# ------------------------------------------------------------------
# Co-driver session membership serializers & request models
# ------------------------------------------------------------------


class JoinSessionRequest(BaseModel):
    """Request body for POST /staff/bottles/session/join."""

    session_id: int = Field(..., description="ID of the DriverBottleSession to join")


def serialize_session_membership(membership) -> Dict[str, Any]:
    """Serialize a DriverSessionMembership with owner/member names."""
    data = membership.to_dict()

    if membership.session_owner:
        data["owner_name"] = (
            f"{membership.session_owner.first_name or ''} {membership.session_owner.last_name or ''}".strip()
        )
        data["owner_phone"] = membership.session_owner.phone

    if membership.member_driver:
        data["member_name"] = (
            f"{membership.member_driver.first_name or ''} {membership.member_driver.last_name or ''}".strip()
        )
        data["member_phone"] = membership.member_driver.phone

    return data


def serialize_joinable_session(session) -> Dict[str, Any]:
    """Compact session view for the join-session list."""
    owner = session.driver
    owner_name = f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else None
    return {
        "session_id": session.id,
        "session_ref": session.session_ref,
        "owner_user_id": session.driver_user_id,
        "owner_name": owner_name,
        "owner_phone": owner.phone if owner else None,
        "bottles_loaded": session.bottles_loaded,
        "current_inventory": session.current_inventory,
        "bottles_delivered": session.bottles_delivered,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "active_members_count": sum(1 for m in (session.memberships or []) if m.status.value == "active"),
    }


def serialize_membership_session_info(membership, session) -> Dict[str, Any]:
    """Current membership info for GET /staff/bottles/session/membership."""
    owner = session.driver
    owner_name = f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else None
    return {
        "membership_id": membership.id,
        "session_id": session.id,
        "session_ref": session.session_ref,
        "owner_user_id": session.driver_user_id,
        "owner_name": owner_name,
        "owner_phone": owner.phone if owner else None,
        "current_inventory": session.current_inventory,
        "bottles_loaded": session.bottles_loaded,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
        "status": membership.status.value if hasattr(membership.status, "value") else membership.status,
    }
