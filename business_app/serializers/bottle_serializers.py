"""Serializers for returnable bottle tracking models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic.alias_generators import to_camel


# ------------------------------------------------------------------
# Pydantic request models
# ------------------------------------------------------------------


class BottleAdjustmentRequest(BaseModel):
    user_id: int
    address_id: int
    adjustment: float
    notes: str

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class BottleInitialBalanceRequest(BaseModel):
    user_id: int
    address_id: int
    quantity: float
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class BottleFineCreateRequest(BaseModel):
    user_id: int
    bottle_balance_id: Optional[int] = None
    address_id: Optional[int] = None
    quantity: float
    fine_amount: float
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class BottleFineUpdateRequest(BaseModel):
    action: str = Field(..., pattern=r"^(waive|mark_paid)$")
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class BottleCollectionRequest(BaseModel):
    customer_id: int
    address_id: int
    quantity: float
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class DriverBottleSessionOpenRequest(BaseModel):
    bottles_loaded: int = Field(..., gt=0)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class DriverBottleSessionCloseRequest(BaseModel):
    bottles_returned_to_warehouse: int = Field(..., ge=0)
    notes: Optional[str] = None

    model_config = {"alias_generator": to_camel, "populate_by_name": True}


class AdminForceCloseSessionRequest(BaseModel):
    bottles_returned_to_warehouse: int = Field(default=0, ge=0)
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
    """Serialize a BottleBalance with optional user/address details."""
    data = balance.to_dict()

    if balance.address:
        data["address_title"] = balance.address.title
        data["full_address"] = balance.address.full_address

    if include_user and balance.user:
        data["user_name"] = f"{balance.user.first_name or ''} {balance.user.last_name or ''}".strip()
        data["user_phone"] = balance.user.phone

    return data


def serialize_bottle_balance_list(balances: List, include_user: bool = True) -> List[Dict[str, Any]]:
    """Serialize a list of BottleBalance records."""
    return [serialize_bottle_balance(b, include_user=include_user) for b in balances]


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


def serialize_bottle_fine(fine) -> Dict[str, Any]:
    """Serialize a BottleFine with related details."""
    data = fine.to_dict()

    if fine.user:
        data["user_name"] = f"{fine.user.first_name or ''} {fine.user.last_name or ''}".strip()
        data["user_phone"] = fine.user.phone

    if fine.issuer:
        data["issuer_name"] = f"{fine.issuer.first_name or ''} {fine.issuer.last_name or ''}".strip()

    if fine.bottle_balance and fine.bottle_balance.address:
        data["address_title"] = fine.bottle_balance.address.title
        data["full_address"] = fine.bottle_balance.address.full_address

    return data


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
