"""Read model for the admin customer map.

One pin per UNGROUPED geocoded address (`kind == "address"`), and one pin per
shared place (`kind == "place"`) instead of one coincident pin per member
address. Every pin carries every key, so the UI never has to guess a shape.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List

from sqlalchemy import case, func

from business_app import db
from business_app.models.bottle import BottleBalance
from business_app.models.customer_link import AddressGroup
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.payment_projection import DEAD_ORDER_STATUSES, open_receivable_clause
from shared.enums import UserRole, UserType, OrderStatus
from shared.user_names import joined_name

# The COD-restriction flag is asked of the service, in ONE batched call per map
# load. This module deliberately holds NO copy of the cap rule and no copy of
# its thresholds — the cap has two arms now, and a local count comparison could
# only ever contradict it.
from business_app.services.cash_collection_service import CashCollectionService

# An order dates a pin and counts toward it only once it is past the
# pending gate. Pending orders still let the customer appear (see the
# `!= CANCELLED` gate below), but they never date or count.
COUNTED_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(OrderStatus) - DEAD_ORDER_STATUSES - {OrderStatus.PENDING}


class CustomerMapService:
    @staticmethod
    def get_customer_map_pins() -> List[Dict[str, Any]]:
        """Address pins ordered by `(user_id, address_id)`, then place pins by group id."""
        last_order = CustomerMapService._last_order_subquery()
        cod_debt = CustomerMapService._cod_debt_subquery()
        address_rows = CustomerMapService._ungrouped_address_rows(last_order, cod_debt)
        places = CustomerMapService._places(last_order, cod_debt)

        # Cluster-aware restriction flags in ONE batched lookup for every person
        # the payload names — address-pin owners and place members alike (spec
        # 5.5): the per-user `active_cod_debt_count` is only this account's
        # slice, so deciding the flag from it would contradict what the cap
        # enforces for a linked customer — and would ignore exemptions entirely.
        user_ids = [r.user_id for r in address_rows]
        user_ids += [m["user_id"] for place in places for m in place["members"]]
        cod_restricted_flags = CashCollectionService().get_cod_restricted_flags(list(dict.fromkeys(user_ids)))

        pins = CustomerMapService._address_pins(address_rows, cod_restricted_flags)
        for place in places:
            for member in place["members"]:
                # SSOT: the batch above, never a local re-derivation. Ids the
                # batch cannot resolve degrade to False, the same answer the
                # batch itself gives them.
                member["cod_restricted"] = cod_restricted_flags.get(member["user_id"], False)
            pins.append(place)
        return pins

    # -- shared building blocks ------------------------------------------------

    @staticmethod
    def _last_order_subquery():
        # Last counted order per user (recency + count). The gate keeps any
        # non-cancelled customer in the map; only counted statuses date/count.
        counted = Order.status.in_(list(COUNTED_ORDER_STATUSES))
        return (
            db.session.query(
                Order.user_id.label("user_id"),
                func.max(case((counted, Order.created_at))).label("last_order_date"),
                func.count(case((counted, Order.id))).label("order_count"),
            )
            .filter(Order.status != OrderStatus.CANCELLED)
            .group_by(Order.user_id)
            .subquery()
        )

    @staticmethod
    def _cod_debt_subquery():
        # Open delivered receivables per user — ANY payment rail. Shares the SSOT
        # clause with cash_collection_service rather than keeping a fourth
        # hand-rolled copy of the predicate (plan 2026-08-08-open-receivable-ssot).
        return (
            db.session.query(
                Payment.user_id.label("user_id"),
                func.count(Payment.id).label("active_cod_debt_count"),
                func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")).label("outstanding_debt"),
            )
            .join(Order, Order.id == Payment.order_id)
            .filter(
                open_receivable_clause(),
                Order.status == OrderStatus.DELIVERED,
            )
            .group_by(Payment.user_id)
            .subquery()
        )

    @staticmethod
    def _map_customer_clauses():
        """Who the map is about: customers, individual or entity."""
        return (
            User.role == UserRole.CUSTOMER,
            User.user_type.in_([UserType.INDIVIDUAL, UserType.ENTITY]),
        )

    @staticmethod
    def _address_label(row) -> str:
        return row.full_address or row.district or ""

    @staticmethod
    def _enum_value(x):
        return x.value if hasattr(x, "value") else x

    # -- address pins ----------------------------------------------------------

    @staticmethod
    def _ungrouped_address_rows(last_order, cod_debt):
        # A grouped address is drawn by its PLACE pin, never by its owner, so the
        # owner query stops at the group boundary. That also leaves this query
        # only the address-keyed balance shape: an ungrouped address's balance
        # row has `address_group_id` NULL. A stale address-keyed row on a GROUPED
        # address (the spec §7.2 strand) can no longer surface here, because the
        # address itself is filtered out; the nightly sweep reports such rows
        # under `stranded_address_balances`.
        return (
            db.session.query(
                UserAddress.id.label("address_id"),
                UserAddress.user_id.label("user_id"),
                UserAddress.latitude,
                UserAddress.longitude,
                UserAddress.is_default,
                UserAddress.full_address,
                UserAddress.district,
                User.first_name,
                User.last_name,
                User.phone,
                User.user_type,
                User.entity_subtype,
                last_order.c.last_order_date,
                last_order.c.order_count,
                func.coalesce(BottleBalance.balance, Decimal("0.00")).label("bottle_balance"),
                func.coalesce(cod_debt.c.active_cod_debt_count, 0).label("active_cod_debt_count"),
                func.coalesce(cod_debt.c.outstanding_debt, Decimal("0.00")).label("outstanding_debt"),
            )
            .join(User, UserAddress.user_id == User.id)
            .join(last_order, last_order.c.user_id == User.id)  # INNER -> ordered customers only
            .outerjoin(
                BottleBalance,
                (BottleBalance.address_id == UserAddress.id) & (BottleBalance.address_group_id.is_(None)),
            )
            .outerjoin(cod_debt, cod_debt.c.user_id == User.id)
            .filter(
                UserAddress.address_group_id.is_(None),
                UserAddress.latitude.isnot(None),
                UserAddress.longitude.isnot(None),
                *CustomerMapService._map_customer_clauses(),
            )
            .order_by(UserAddress.user_id, UserAddress.id)
            .all()
        )

    @staticmethod
    def _address_pins(rows, cod_restricted_flags) -> List[Dict[str, Any]]:
        by_user: Dict[int, list] = defaultdict(list)
        for r in rows:
            by_user[r.user_id].append(r)

        val = CustomerMapService._enum_value
        pins: List[Dict[str, Any]] = []
        for _uid, urows in by_user.items():
            count = len(urows)
            for idx, r in enumerate(urows, start=1):
                outstanding = r.outstanding_debt or Decimal("0.00")
                pins.append(
                    {
                        "kind": "address",
                        "address_id": r.address_id,
                        "user_id": r.user_id,
                        "full_name": joined_name(r.first_name, r.last_name),
                        "phone": r.phone,
                        "user_type": val(r.user_type),
                        "entity_subtype": val(r.entity_subtype) if r.entity_subtype else None,
                        "lat": float(r.latitude),
                        "lng": float(r.longitude),
                        "is_default": bool(r.is_default),
                        "address_label": CustomerMapService._address_label(r),
                        # Among the owner's UNGROUPED geocoded addresses only.
                        "address_index": idx,
                        "address_count": count,
                        "last_order_date": r.last_order_date,
                        "order_count": int(r.order_count or 0),
                        "bottle_balance": r.bottle_balance or Decimal("0.00"),
                        "outstanding_debt": outstanding,
                        "active_cod_debt_count": int(r.active_cod_debt_count or 0),
                        "cod_restricted": cod_restricted_flags.get(r.user_id, False),
                        "is_shared_place": False,
                        "has_debt": outstanding > 0,
                        "place_group_id": None,
                        "place_label": None,
                        "last_order_by": None,
                        "members": [],
                    }
                )
        return pins

    # -- place pins ------------------------------------------------------------

    @staticmethod
    def _places(last_order, cod_debt) -> List[Dict[str, Any]]:
        """One pin per place that has a geocoded address and ≥1 member past the gate.

        `members[].cod_restricted` is filled in by the caller, from the single
        batched flag lookup.
        """
        rows = (
            db.session.query(
                UserAddress.id.label("address_id"),
                UserAddress.user_id.label("user_id"),
                UserAddress.address_group_id.label("group_id"),
                UserAddress.latitude,
                UserAddress.longitude,
                UserAddress.full_address,
                UserAddress.district,
                AddressGroup.label.label("place_label"),
                User.first_name,
                User.last_name,
                User.phone,
                last_order.c.user_id.label("gate_user_id"),
                func.coalesce(cod_debt.c.active_cod_debt_count, 0).label("active_cod_debt_count"),
                func.coalesce(cod_debt.c.outstanding_debt, Decimal("0.00")).label("outstanding_debt"),
            )
            .select_from(UserAddress)
            .join(AddressGroup, AddressGroup.id == UserAddress.address_group_id)
            .join(User, UserAddress.user_id == User.id)
            .outerjoin(last_order, last_order.c.user_id == User.id)  # the member gate
            .outerjoin(cod_debt, cod_debt.c.user_id == User.id)
            .filter(*CustomerMapService._map_customer_clauses())
            .order_by(UserAddress.address_group_id, UserAddress.id)
            .all()
        )

        groups: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            group = groups.setdefault(
                r.group_id, {"label": r.place_label, "anchor": None, "passes_gate": False, "owners": {}}
            )
            # Rows arrive by address id, so the first geocoded one is the lowest.
            if group["anchor"] is None and r.latitude is not None and r.longitude is not None:
                group["anchor"] = r
            group["passes_gate"] = group["passes_gate"] or r.gate_user_id is not None
            group["owners"].setdefault(r.user_id, r)  # one row per OWNER, not per address

        live = {gid: g for gid, g in groups.items() if g["passes_gate"] and g["anchor"] is not None}
        if not live:
            return []
        group_ids = sorted(live)

        # Counted orders delivered to any CURRENT group address, per orderer —
        # members or not. The place date is the newest of these, so a member's
        # order to their own home never refreshes it.
        orders_here: Dict[int, List[Any]] = defaultdict(list)
        for row in (
            db.session.query(
                UserAddress.address_group_id.label("group_id"),
                Order.user_id.label("user_id"),
                func.max(Order.created_at).label("last_order_date"),
                func.count(Order.id).label("order_count"),
            )
            .select_from(Order)
            .join(UserAddress, UserAddress.id == Order.delivery_address_id)
            .filter(
                UserAddress.address_group_id.in_(group_ids),
                Order.status.in_(list(COUNTED_ORDER_STATUSES)),
            )
            .group_by(UserAddress.address_group_id, Order.user_id)
            .order_by(UserAddress.address_group_id, Order.user_id)
            .all()
        ):
            orders_here[row.group_id].append(row)

        # The newest order's placer; `max` keeps the FIRST maximum, and the rows
        # are by user id, so a tie names the lowest user id.
        newest_by_group = {gid: max(entries, key=lambda e: e.last_order_date) for gid, entries in orders_here.items()}

        names = {uid: joined_name(r.first_name, r.last_name) for g in live.values() for uid, r in g["owners"].items()}
        missing = {e.user_id for e in newest_by_group.values()} - set(names)
        if missing:
            # The newest order here was placed by a non-member (an ex-member or
            # another customer): one plain select by `Order.user_id`'s ids, no join.
            for uid, first, last in (
                db.session.query(User.id, User.first_name, User.last_name).filter(User.id.in_(missing)).all()
            ):
                names[uid] = joined_name(first, last)

        bottles = BottleTrackingService.get_place_balances_by_group(group_ids)
        # What each place OWES: the one decision the Grouped Addresses list
        # (`CustomerLinkService.list_place_groups`) also reads, so the reader,
        # its row ceiling and the miss rule (0.0 / 0) are decided in one place.
        exposure = CashCollectionService().get_place_cod_exposure_by_group()

        places: List[Dict[str, Any]] = []
        for gid in group_ids:
            group = live[gid]
            anchor = group["anchor"]
            here = orders_here.get(gid, [])
            newest = newest_by_group.get(gid)
            member_dates = {e.user_id: e.last_order_date for e in here}
            members = CustomerMapService._sorted_members(
                [
                    {
                        "user_id": uid,
                        "full_name": names[uid],
                        "phone": r.phone,
                        "last_order_date": member_dates.get(uid),
                        "outstanding_debt": r.outstanding_debt or Decimal("0.00"),
                        "active_cod_debt_count": int(r.active_cod_debt_count or 0),
                        "cod_restricted": False,
                    }
                    for uid, r in group["owners"].items()
                ]
            )
            cod = exposure[gid]
            place_debt = cod.total_outstanding_amount
            places.append(
                {
                    "kind": "place",
                    "address_id": anchor.address_id,
                    "user_id": None,
                    "full_name": "",
                    "phone": None,
                    "user_type": UserType.INDIVIDUAL.value,
                    "entity_subtype": None,
                    "lat": float(anchor.latitude),
                    "lng": float(anchor.longitude),
                    "is_default": False,
                    "address_label": CustomerMapService._address_label(anchor),
                    "address_index": 1,
                    "address_count": 1,
                    "last_order_date": newest.last_order_date if newest else None,
                    "order_count": sum(int(e.order_count or 0) for e in here),
                    "bottle_balance": bottles.get(gid, Decimal("0.00")),
                    "outstanding_debt": place_debt,
                    "active_cod_debt_count": cod.active_cod_debt_count,
                    "cod_restricted": False,
                    "is_shared_place": True,
                    "has_debt": place_debt > 0 or any(m["outstanding_debt"] > 0 for m in members),
                    "place_group_id": gid,
                    "place_label": group["label"],
                    "last_order_by": names.get(newest.user_id) if newest else None,
                    "members": members,
                }
            )
        return places

    @staticmethod
    def _sorted_members(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Newest order here first, never-ordered-here last; ties by user id."""
        by_id = sorted(members, key=lambda m: m["user_id"])
        dated = [m for m in by_id if m["last_order_date"] is not None]
        dated.sort(key=lambda m: m["last_order_date"], reverse=True)  # stable: ties keep id order
        return dated + [m for m in by_id if m["last_order_date"] is None]
