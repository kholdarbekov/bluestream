"""Candidate places a driver is offered when a first-order customer hands back empties.

Spec 2026-11-10 section 4.3. One read-only function, ``find_candidates``: from the delivery address's pin it
returns up to ``limit`` nearby places that hold empties and are owned by eligible individual customers. A place
is a place group (one row per group) or an ungrouped address, the same unit a bottle balance belongs to.

Nothing here writes, locks or commits. The result is frozen by the caller into ``SharedPlaceCase.candidates``.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import and_, or_

from business_app import db
from business_app.models.bottle import BottleBalance
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.helpers import calculate_distance
from shared.enums import OrderStatus

NAME_PLACEHOLDER = "—"
# `_bbox_prefilter` uses 111 km per degree of latitude (a true degree is ~111.2), so widen its box a little:
# the exact distance test below decides, the box only has to never cut a true neighbour.
_BBOX_SLACK = 1.05


@dataclass(frozen=True)
class PlaceCandidate:
    ref_kind: str  # "group" | "address"
    ref_id: int
    first_name: str  # NAME_PLACEHOLDER when empty
    empties: int
    distance_m: int  # nearest surviving member, whole metres
    member_address_ids: Tuple[int, ...]

    def to_json(self) -> dict:
        """The frozen-candidate shape stored on the case."""
        return {
            "ref": {"kind": self.ref_kind, "id": self.ref_id},
            "first_name": self.first_name,
            "empties": self.empties,
            "distance_m": self.distance_m,
        }


@dataclass(frozen=True)
class CandidateResult:
    candidates: Tuple[PlaceCandidate, ...]
    empty_reason: Optional[str]  # None when candidates is non-empty


def _m_to_km(metres: float) -> float:
    return metres / 1000.0


def _km_to_m(km: float) -> float:
    return km * 1000.0


def _place_balance_join():
    """Join condition from an address to its PLACE balance row (mirrors ``BottleScope.balance_filter``)."""
    return or_(
        and_(
            UserAddress.address_group_id.is_(None),
            BottleBalance.address_id == UserAddress.id,
            BottleBalance.address_group_id.is_(None),
        ),
        and_(
            UserAddress.address_group_id.isnot(None),
            BottleBalance.address_group_id == UserAddress.address_group_id,
        ),
    )


def _norm_apartment(value) -> str:
    return (value or "").strip().casefold()


def _apartments_differ(a, b) -> bool:
    left, right = _norm_apartment(a), _norm_apartment(b)
    return bool(left) and bool(right) and left != right


def find_candidates(address: UserAddress, *, limit: int = 3) -> CandidateResult:
    if address.latitude is None or address.longitude is None:
        # Before the prefilter: with empty coordinates it does not filter at all.
        return CandidateResult((), "no_pin")

    radius_m = float(current_app.config["SHARED_PLACE_CANDIDATE_RADIUS_M"])
    lat, lng = address.latitude, address.longitude

    query = (
        db.session.query(
            UserAddress.id,
            UserAddress.user_id,
            UserAddress.address_group_id,
            UserAddress.latitude,
            UserAddress.longitude,
            UserAddress.apartment_number,
            BottleBalance.balance,
        )
        .join(BottleBalance, _place_balance_join())
        .filter(UserAddress.id != address.id, BottleBalance.balance > 0)
    )
    if address.address_group_id is not None:
        # The delivery address's own place is never offered, whichever member is nearby.
        query = query.filter(
            or_(UserAddress.address_group_id.is_(None), UserAddress.address_group_id != address.address_group_id)
        )
    query = CustomerLinkService._bbox_prefilter(query, [(lat, lng)], _m_to_km(radius_m) * _BBOX_SLACK)

    nearby = []  # (address_id, user_id, group_id, balance, distance_km, apartment)
    for addr_id, user_id, group_id, a_lat, a_lng, apartment, balance in query.all():
        distance_km = calculate_distance(lat, lng, a_lat, a_lng)
        if _km_to_m(distance_km) <= radius_m:
            nearby.append((addr_id, user_id, group_id, balance, distance_km, apartment))
    if not nearby:
        return CandidateResult((), "none_within_radius")

    nearby = [row for row in nearby if not _apartments_differ(address.apartment_number, row[5])]

    group_ids = sorted({row[2] for row in nearby if row[2] is not None})
    members_by_group: Dict[int, List[Tuple[int, int]]] = {gid: [] for gid in group_ids}
    if group_ids:
        for member_id, member_user_id, gid in (
            db.session.query(UserAddress.id, UserAddress.user_id, UserAddress.address_group_id)
            .filter(UserAddress.address_group_id.in_(group_ids))
            .order_by(UserAddress.id)
            .all()
        ):
            members_by_group[gid].append((member_id, member_user_id))

    owner_ids = {row[1] for row in nearby if row[2] is None}
    owner_ids.update(uid for members in members_by_group.values() for _, uid in members)
    owners = {u.id: u for u in User.query.filter(User.id.in_(owner_ids)).all()} if owner_ids else {}

    def eligible(user_id: int) -> bool:
        user = owners.get(user_id)
        return user is not None and CustomerLinkService.is_eligible_individual_customer(user)

    places: Dict[Tuple[str, int], dict] = {}
    for addr_id, user_id, group_id, balance, distance_km, _ in nearby:
        if group_id is None:
            if not eligible(user_id):
                continue
            key, member_ids = ("address", addr_id), (addr_id,)
        else:
            if not all(eligible(uid) for _, uid in members_by_group[group_id]):
                continue
            key = ("group", group_id)
            member_ids = tuple(mid for mid, _ in members_by_group[group_id])
        place = places.setdefault(key, {"empties": int(balance), "distance_km": distance_km, "member_ids": member_ids})
        place["distance_km"] = min(place["distance_km"], distance_km)
    if not places:
        return CandidateResult((), "none_eligible")

    ranked = sorted(
        ((round(_km_to_m(p["distance_km"])), -p["empties"], key[1], key, p) for key, p in places.items()),
        key=lambda item: item[:3],
    )[:limit]

    candidates = tuple(
        PlaceCandidate(
            ref_kind=key[0],
            ref_id=key[1],
            first_name=_shown_name(place["member_ids"]),
            empties=place["empties"],
            distance_m=distance_m,
            member_address_ids=place["member_ids"],
        )
        for distance_m, _, _, key, place in ranked
    )
    return CandidateResult(candidates, None)


def _shown_name(member_ids: Tuple[int, ...]) -> str:
    """First name of the newest non-cancelled order's customer at the place, else the lowest member's owner."""
    name = (
        db.session.query(User.first_name)
        .select_from(Order)
        .join(User, User.id == Order.user_id)  # orders has two FKs to users: pin the join
        .filter(Order.delivery_address_id.in_(member_ids), Order.status != OrderStatus.CANCELLED)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
        .scalar()
    )
    if name is None:
        name = (
            db.session.query(User.first_name)
            .join(UserAddress, UserAddress.user_id == User.id)
            .filter(UserAddress.id == min(member_ids))
            .scalar()
        )
    return (name or "").strip() or NAME_PLACEHOLDER
