"""The admin's view of shared-place cases (spec 2026-11-10 §4.5; §9 binds): the list, one case, and what the case
screen offers.

Read-only. The frozen facts come from the case row. Everything else is read now: a candidate that was grouped,
dissolved, emptied or deleted since the case opened still renders (`live.exists` false for a place that is gone),
and a grouping the admin already made shows as done.

Every count in the detail is an `int`, because Flask serializes a `Decimal` as a string. The one exception is a
try-out's `outstanding_bottles_total`, which stays the try-out service's float.

`allowed_choices` decides only what the SCREEN offers. The resolve call verifies against live state itself and never
gates A or B on it (spec §9).
"""

from datetime import datetime, timedelta
from typing import Dict, List, Literal, Optional, Tuple

from flask import current_app
from sqlalchemy import case as sql_case
from sqlalchemy.orm import joinedload

from business_app.models.customer_link import AddressGroup
from business_app.models.order import Order
from business_app.models.shared_place_case import (
    SHARED_PLACE_CASE_STATUSES,
    SHARED_PLACE_CHOICE_KINDS,
    SHARED_PLACE_OPEN_STATUSES,
    SharedPlaceCase,
)
from business_app.models.user import User, UserAddress
from business_app.serializers.sales_serializers import _iso
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.tryout_service import TryoutService
from business_app.utils.exceptions import NotFoundError
from business_app.utils.timezone_utils import ensure_utc, get_utc_now

_LIST_FILTERS: Dict[str, Tuple[str, ...]] = {
    "open": SHARED_PLACE_OPEN_STATUSES,
    "resolved": ("resolved",),
    "closed": ("closed",),
}


def _gone() -> dict:
    """A place that no longer exists: a deleted address, or a group dissolved down to no members."""
    return {"exists": False, "place_group_id": None, "label": None, "empties": 0, "members": []}


def driver_status(case: SharedPlaceCase, *, now: Optional[datetime] = None) -> str:
    """What the driver did: `not_asked` (an admin-entered case), `answered`, `awaiting` (still inside the answer
    window), or `no_answer` (the window passed, or an admin settled the case before the driver answered)."""
    if case.asked_driver_id is None:
        return "not_asked"
    if case.driver_answer is not None:
        return "answered"
    window = timedelta(seconds=current_app.config["SHARED_PLACE_DRIVER_ANSWER_WINDOW_S"])
    if case.status == "awaiting_driver" and (now or get_utc_now()) - ensure_utc(case.created_at) <= window:
        return "awaiting"
    return "no_answer"


def allowed_choices(case: SharedPlaceCase) -> dict:
    """What the case screen offers: `{"kinds": [...], "move_allowed": bool, "grouping_done": bool}`.

    - A settled case offers nothing.
    - While a claim is stored, only its kind, and the move stays allowed so a half-done move can be finished.
    - When the case address is in a live place group now (by this case's own grouping or elsewhere), every kind, but
      no move: moving empties into a place that is already shared would count them twice. The screen then shows A and
      B as "confirm" actions that resolve without regrouping (spec §4.5, §9). "Live" is the predicate the resolve call
      verifies A and B with (`CustomerLinkService.is_in_live_place_group`), so "Confirm" is never offered for a
      grouping the resolve would refuse.
    """
    grouping_done = CustomerLinkService().is_in_live_place_group(case.address_id)
    if case.status not in SHARED_PLACE_OPEN_STATUSES:
        return {"kinds": [], "move_allowed": False, "grouping_done": grouping_done}
    if case.resolution_kind is not None:
        return {"kinds": [case.resolution_kind], "move_allowed": True, "grouping_done": grouping_done}
    return {"kinds": list(SHARED_PLACE_CHOICE_KINDS), "move_allowed": not grouping_done, "grouping_done": grouping_done}


def _person(user: Optional[User], *fields: str) -> Optional[dict]:
    if user is None:
        return None
    return {"id": user.id, **{field: getattr(user, field) for field in fields}}


class SharedPlaceReview:
    """The list and one case, for the admin's Driver reports tab."""

    @staticmethod
    def list_cases(*, status: Literal["open", "resolved", "closed"], page: int, per_page: int) -> dict:
        """One page of cases with `status`: awaiting the admin first, then awaiting the driver; newest first."""
        query = SharedPlaceCase.query.filter(SharedPlaceCase.status.in_(_LIST_FILTERS[status]))
        total = query.count()
        cases = (
            query.options(joinedload(SharedPlaceCase.order).joinedload(Order.user), joinedload(SharedPlaceCase.address))
            .order_by(
                sql_case((SharedPlaceCase.status == "awaiting_admin", 0), else_=1),
                SharedPlaceCase.created_at.desc(),
                SharedPlaceCase.id.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        now = get_utc_now()
        return {
            "cases": [SharedPlaceReview._list_row(case, now) for case in cases],
            "open_count": SharedPlaceCase.query.filter(SharedPlaceCase.status.in_(SHARED_PLACE_OPEN_STATUSES)).count(),
            "page": page,
            "per_page": per_page,
            "total": total,
            "statuses": list(SHARED_PLACE_CASE_STATUSES),
        }

    @staticmethod
    def _list_row(case: SharedPlaceCase, now: datetime) -> dict:
        customer = case.order.user
        return {
            "id": case.id,
            "status": case.status,
            "driver_status": driver_status(case, now=now),
            "created_at": _iso(case.created_at),
            "order_number": case.order.order_number,
            "customer_first_name": customer.first_name,
            "customer_last_name": customer.last_name,
            "address_title": case.address.title if case.address else None,
            "returned_qty": case.returned_qty,
            "answer_kind": case.answer_kind,
        }

    @staticmethod
    def case_detail(case_id: int) -> dict:
        """One case: what happened (frozen), what the driver said, and the live facts the admin decides on."""
        case = SharedPlaceCase.query.get(case_id)
        if case is None:
            raise NotFoundError("Shared-place case not found", error_code="SHARED_PLACE_CASE_NOT_FOUND")
        order = case.order
        customer = order.user
        address = case.address
        place_group = AddressGroup.query.get(address.address_group_id) if address.address_group_id else None
        cluster = CustomerLinkService().get_cluster_user_ids(order.user_id)
        summary = BottleTrackingService.get_order_bottle_summary(order)
        return {
            "id": case.id,
            "status": case.status,
            "driver_status": driver_status(case),
            "created_at": _iso(case.created_at),
            "order": {"id": order.id, "order_number": order.order_number},
            "delivery_id": case.delivery_id,
            "customer": _person(customer, "first_name", "last_name", "phone"),
            "address": {
                "id": address.id,
                "title": address.title,
                "full_address": address.full_address,
                "latitude": address.latitude,
                "longitude": address.longitude,
                "apartment_number": address.apartment_number,
                "place_group": {"id": place_group.id, "label": place_group.label} if place_group else None,
                "place_empties_now": int(BottleTrackingService.get_place_balance(address.id)),
            },
            "driver": _person(case.asked_driver, "first_name"),
            "delivered_qty": int(summary["bottles_delivered"]),
            "returned_qty": int(case.returned_qty),
            "place_balance_before": int(case.place_balance_before),
            "answer": SharedPlaceReview._answer(case),
            "candidates": [
                {
                    "index": index,
                    "ref": frozen["ref"],
                    "first_name": frozen["first_name"],
                    "empties": int(frozen["empties"]),
                    "distance_m": int(frozen["distance_m"]),
                    "live": SharedPlaceReview._live_place(frozen["ref"]),
                }
                for index, frozen in enumerate(case.candidates or [])
            ],
            "empty_reason": case.empty_reason,
            "facts": SharedPlaceReview._facts(customer, cluster),
            "tryouts": TryoutService.converted_tryouts_with_trial_bottles([order.user_id]),
            "move_sources": SharedPlaceReview._move_sources(cluster, address),
            "choices": allowed_choices(case),
            "claim": (
                {"kind": case.resolution_kind, "payload": case.resolution_payload}
                if case.status in SHARED_PLACE_OPEN_STATUSES and case.resolution_kind is not None
                else None
            ),
            "resolution": SharedPlaceReview._resolution(case),
        }

    @staticmethod
    def _answer(case: SharedPlaceCase) -> Optional[dict]:
        if case.driver_answer is None and case.driver_note is None:
            return None
        return {
            "kind": case.answer_kind,
            "picked_index": case.picked_index,
            "note": case.driver_note,
            "answered_at": _iso(case.answered_at),
        }

    @staticmethod
    def _live_place(ref: dict) -> dict:
        """The frozen candidate's place as it is now: a group with its members, or an address with its place."""
        if ref["kind"] == "group":
            group_id = ref["id"]
        else:
            address = UserAddress.query.get(ref["id"])
            if address is None:
                return _gone()
            if address.address_group_id is None:
                return SharedPlaceReview._place_view(None, [address])
            group_id = address.address_group_id
        group = AddressGroup.query.get(group_id)
        members = (
            UserAddress.query.filter(UserAddress.address_group_id == group_id).order_by(UserAddress.id.asc()).all()
        )
        if group is None or not members:
            return _gone()
        return SharedPlaceReview._place_view(group, members)

    @staticmethod
    def _place_view(group: Optional[AddressGroup], members: List[UserAddress]) -> dict:
        owner_ids = sorted({member.user_id for member in members})
        owners = {user.id: user for user in User.query.filter(User.id.in_(owner_ids)).all()}
        return {
            "exists": True,
            "place_group_id": group.id if group else None,
            "label": group.label if group else None,
            "empties": int(BottleTrackingService.get_place_balance(members[0].id)),
            "members": [
                {
                    "address_id": member.id,
                    "title": member.title,
                    "full_address": member.full_address,
                    "owner": _person(owners.get(member.user_id), "first_name", "last_name", "phone"),
                }
                for member in members
            ],
        }

    @staticmethod
    def _facts(customer: User, cluster: List[int]) -> dict:
        """The "already linked to …" and "referred by …" facts, read now."""
        linked = User.query.filter(User.id.in_(cluster), User.id != customer.id).order_by(User.id.asc()).all()
        referrer = User.query.get(customer.referred_by_user_id) if customer.referred_by_user_id else None
        return {
            "linked_accounts": [_person(user, "first_name", "last_name") for user in linked],
            "referred_by": _person(referrer, "first_name", "last_name"),
        }

    @staticmethod
    def _move_sources(cluster: List[int], address: UserAddress) -> List[dict]:
        """The places of the customer's linked cluster that hold empties now, other than the case address's own place.

        A place group can mix owners, so a group's `address_id` is its lowest member address OWNED BY A CLUSTER
        USER: the address whose owner the resolve call checks.
        """
        own_place = ("group", address.address_group_id) if address.address_group_id else ("address", address.id)
        first_member: Dict[Tuple[str, int], UserAddress] = {}
        for member in UserAddress.query.filter(UserAddress.user_id.in_(cluster)).order_by(UserAddress.id.asc()).all():
            place = ("group", member.address_group_id) if member.address_group_id else ("address", member.id)
            if place != own_place:
                first_member.setdefault(place, member)
        if not first_member:
            return []

        group_ids = [place_id for kind, place_id in first_member if kind == "group"]
        labels = {g.id: g.label for g in AddressGroup.query.filter(AddressGroup.id.in_(group_ids)).all()}
        owners = {user.id: user for user in User.query.filter(User.id.in_(cluster)).all()}
        sources = []
        for (kind, place_id), member in first_member.items():
            empties = int(BottleTrackingService.get_place_balance(member.id))
            if empties <= 0:
                continue
            owner = owners.get(member.user_id)
            sources.append(
                {
                    "address_id": member.id,
                    "place_group_id": place_id if kind == "group" else None,
                    "label": labels.get(place_id) if kind == "group" else None,
                    "empties": empties,
                    "owner_first_name": owner.first_name if owner else None,
                }
            )
        return sources

    @staticmethod
    def _resolution(case: SharedPlaceCase) -> Optional[dict]:
        if case.status in SHARED_PLACE_OPEN_STATUSES:
            return None
        return {
            # A closed case records no kind (ck_shared_place_cases_closed_kind); its stored claim says "close".
            "kind": "close" if case.status == "closed" else case.resolution_kind,
            "payload": case.resolution_payload,
            "reason": case.decision_reason,
            "decided_by": _person(case.decided_by, "first_name"),
            "decided_at": _iso(case.decided_at),
            "resulting_group_id": case.resulting_group_id,
        }
