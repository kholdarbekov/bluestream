"""The duplicate-address guard: the rules, and nothing that writes (spec 2026-11-18-duplicate-address-guard §6.1,
§6.3, §6.5).

- A new address, or an address whose pin moves, is a possible duplicate of a saved one of the same customer when
  `is_possible_duplicate_pair` holds. The save path asks the customer (409 `ADDRESS_POSSIBLE_DUPLICATE`) unless they
  confirmed; the admin suggestions use the same predicate.
- A saved address's pin may move only under the customer pin rules (`pin_move_refusal`): not while an order to it is
  with the driver, not when the place is shared with another customer, not when an outlet holds it, and not further
  than `ADDRESS_MOVE_MAX_M` once it has history. `pin_move_refusals` is the same answer for every address of a
  customer, for `GET /auth/addresses`.

`AddressPinService` writes and locks; it asks these rules again under its locks.

Write paths that never call the guard, by design (spec §6.4). A new write path is checked against this list:
- outlet addresses (`OutletService._adopt_or_create_address`, `OutletService._create_address`): R39/R42 already answer
  "same place" at 150 m, and "which address is this shop" keeps one answer;
- account merge (`CrossPlatformSyncService._transfer_user_references`): it moves existing rows to the surviving
  account and creates no place;
- the route-optimisation geocode fill (`RouteOptimizationService._geocode_missing_addresses`): it fills a missing pin
  from the address's own text;
- try-out conversion (`TryoutService._upsert_converted_user_address`): it adopts the customer's nearest own address
  instead of asking, never moves an existing pin, and creates with `confirm_duplicate=True`.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.bottle import BottleBalance
from business_app.models.order import Order
from business_app.models.sales import Outlet
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from business_app.utils.address_helpers import apartments_differ
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.geo_validation import is_fallback_point
from business_app.utils.helpers import calculate_distance
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.utils.translations import get_translation

logger = logging.getLogger(__name__)

# The written line a move replaces when the body carries it, and the door details it only fills where the saved
# address has none (spec §6.3). Used only by the move operation.
ADDRESS_LOCATION_FIELDS = ("full_address", "street_address", "district")
ADDRESS_DETAIL_FIELDS = ("apartment_number", "floor_number", "delivery_instructions", "landmark")
# The `point_source` values that mean the customer placed the point; `search`, `typed`, a missing or an unknown
# value do not.
PIN_POINT_SOURCES = frozenset({"map_pin", "gps"})
# A pin is unchanged within this many metres: the website and the admin form resend the stored point on every edit.
PIN_CHANGED_TOLERANCE_M = 1.0
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class GuardPoint:
    latitude: float
    longitude: float
    apartment_number: Optional[str] = None


@dataclass(frozen=True)
class _PinFacts:
    """What the pin rules read about one saved address."""

    with_driver_order_number: Optional[str]  # an open order to it that the driver has, else None
    shared_with_another_customer: bool  # its place group holds another customer's address
    held_by_outlet: bool
    has_history: bool  # any order to it, or a bottle balance for it or its place


def as_point(latitude, longitude, apartment_number=None) -> Optional[GuardPoint]:
    """The pair as a point, or None when either value is missing, not a number (numeric strings count) or not a place
    on Earth: a latitude outside [-90, 90] or a longitude outside [-180, 180], which no distance can be measured to."""
    try:
        point = GuardPoint(float(latitude), float(longitude), apartment_number)
    except (TypeError, ValueError):
        return None
    on_earth = -90.0 <= point.latitude <= 90.0 and -180.0 <= point.longitude <= 180.0  # False for NaN, too
    return point if on_earth else None


def distance_m(a: GuardPoint, b: GuardPoint) -> float:
    return calculate_distance(a.latitude, a.longitude, b.latitude, b.longitude) * 1000


def _is_real_pin(point: Optional[GuardPoint]) -> bool:
    return point is not None and not is_fallback_point(point.latitude, point.longitude)


def is_possible_duplicate_pair(a: GuardPoint, b: GuardPoint) -> bool:
    """The §6.1 rule, pairwise and never chained: neither point is a fallback point, they are within
    `ADDRESS_DUPLICATE_RADIUS_M`, and their apartments are not both known and different. The title decides nothing."""
    return (
        _is_real_pin(a)
        and _is_real_pin(b)
        and distance_m(a, b) <= current_app.config["ADDRESS_DUPLICATE_RADIUS_M"]
        and not apartments_differ(a.apartment_number, b.apartment_number)
    )


# One degree of latitude, and of longitude up to 44 deg N (Tashkent is at ~41.3 N), is at least this many metres.
_MIN_METRES_PER_DEGREE = 80_000.0


def within_duplicate_reach(a: GuardPoint, b: GuardPoint) -> bool:
    """A pre-filter for `is_possible_duplicate_pair` that needs no geodesic: False when the points are further apart
    on either axis than `ADDRESS_DUPLICATE_RADIUS_M` can reach, so the rule refuses them too. It can pass a pair the
    rule then refuses, never drop one the rule accepts. For callers that test many pairs (the suggestions read)."""
    reach_deg = current_app.config["ADDRESS_DUPLICATE_RADIUS_M"] / _MIN_METRES_PER_DEGREE
    return abs(a.latitude - b.latitude) <= reach_deg and abs(a.longitude - b.longitude) <= reach_deg


def possible_duplicates(
    user_id: int, latitude, longitude, *, apartment_number=None, exclude_address_id=None
) -> List[Tuple[UserAddress, int]]:
    """The customer's own saved addresses the point may duplicate, nearest first (ties by id), at most
    `MAX_CANDIDATES`, each with its distance in whole metres. A pinless point, or a pinless saved row, never pairs."""
    point = as_point(latitude, longitude, apartment_number)
    if point is None:
        return []
    query = UserAddress.query.filter(
        UserAddress.user_id == user_id,
        UserAddress.latitude.isnot(None),
        UserAddress.longitude.isnot(None),
    )
    if exclude_address_id is not None:
        query = query.filter(UserAddress.id != exclude_address_id)
    found = []
    for address in query.all():
        saved = as_point(address.latitude, address.longitude, address.apartment_number)
        if saved is not None and is_possible_duplicate_pair(point, saved):
            found.append((distance_m(point, saved), address))
    found.sort(key=lambda pair: (pair[0], pair[1].id))
    return [(address, round(distance)) for distance, address in found[:MAX_CANDIDATES]]


def pin_changed(address: UserAddress, data: Mapping[str, Any]) -> bool:
    """Whether `data` moves the address's pin: a payload point more than `PIN_CHANGED_TOLERANCE_M` from the stored
    one, or any point when none is stored. A half pair is read against the stored other half; a payload without a
    valid point changes nothing."""
    if "latitude" not in data and "longitude" not in data:
        return False
    new = as_point(data.get("latitude", address.latitude), data.get("longitude", address.longitude))
    if new is None:
        return False
    stored = as_point(address.latitude, address.longitude)
    if stored is None:
        return True
    return distance_m(stored, new) > PIN_CHANGED_TOLERANCE_M


def is_placed_pin(point_source: Optional[str], latitude, longitude) -> bool:
    """The customer placed this point (a map pin or their GPS), and it is a real point, not a fallback."""
    return (
        isinstance(point_source, str)
        and point_source in PIN_POINT_SOURCES
        and _is_real_pin(as_point(latitude, longitude))
    )


def _pin_facts(addresses: Sequence[UserAddress]) -> Dict[int, _PinFacts]:
    """The pin rules' facts for every address, one query per fact whatever the number of addresses."""
    if not addresses:
        return {}
    ids = [address.id for address in addresses]
    group_ids = {address.address_group_id for address in addresses if address.address_group_id is not None}

    # With the driver: OrderService's own predicate over the open orders, never a SQL copy of it.
    with_driver: Dict[int, str] = {}
    open_orders = (
        Order.query.options(selectinload(Order.delivery))
        .filter(Order.delivery_address_id.in_(ids), Order.status.in_(ACTIVE_ORDER_STATUSES))
        .order_by(Order.id)
        .all()
    )
    for order in open_orders:
        if OrderService.order_is_with_driver(order):
            with_driver.setdefault(order.delivery_address_id, order.order_number)

    ordered_to = {
        address_id
        for (address_id,) in db.session.query(Order.delivery_address_id)
        .filter(Order.delivery_address_id.in_(ids))
        .distinct()
    }

    customers_by_group: Dict[int, set] = {}
    if group_ids:
        members = (
            db.session.query(UserAddress.address_group_id, UserAddress.user_id)
            .filter(UserAddress.address_group_id.in_(group_ids))
            .distinct()
        )
        for group_id, member_user_id in members:
            customers_by_group.setdefault(group_id, set()).add(member_user_id)

    outlet_held = {
        address_id for (address_id,) in db.session.query(Outlet.address_id).filter(Outlet.address_id.in_(ids))
    }

    balance_scope = BottleBalance.address_id.in_(ids)
    if group_ids:
        balance_scope = or_(balance_scope, BottleBalance.address_group_id.in_(group_ids))
    balance_addresses, balance_groups = set(), set()
    for address_id, group_id in db.session.query(BottleBalance.address_id, BottleBalance.address_group_id).filter(
        balance_scope
    ):
        if address_id is not None:
            balance_addresses.add(address_id)
        if group_id is not None:
            balance_groups.add(group_id)

    return {
        address.id: _PinFacts(
            with_driver_order_number=with_driver.get(address.id),
            shared_with_another_customer=bool(
                customers_by_group.get(address.address_group_id, set()) - {address.user_id}
            ),
            held_by_outlet=address.id in outlet_held,
            has_history=(
                address.id in ordered_to
                or address.id in balance_addresses
                or (address.address_group_id is not None and address.address_group_id in balance_groups)
            ),
        )
        for address in addresses
    }


def _refusal(address: UserAddress, facts: _PinFacts, latitude, longitude) -> Optional[str]:
    """The §6.3 customer pin rules, in order; the first that matches refuses. TOO_FAR needs the new point."""
    stored = as_point(address.latitude, address.longitude)
    if stored is not None and facts.with_driver_order_number is not None:
        return "WITH_DRIVER"
    if address.address_group_id is not None and facts.shared_with_another_customer:
        return "SHARED_PLACE"
    if facts.held_by_outlet:
        return "OUTLET"
    new = as_point(latitude, longitude)
    if (
        new is not None
        and facts.has_history
        and _is_real_pin(stored)
        and distance_m(stored, new) > current_app.config["ADDRESS_MOVE_MAX_M"]
    ):
        return "TOO_FAR"
    return None


def pin_move_refusal(address: UserAddress, latitude=None, longitude=None) -> Optional[str]:
    """Why the customer may not move this address's pin to the point: "WITH_DRIVER", "SHARED_PLACE", "OUTLET" or
    "TOO_FAR"; None when they may. Without a point, TOO_FAR is never the answer."""
    return _refusal(address, _pin_facts([address])[address.id], latitude, longitude)


def pin_move_refusals(user_id: int) -> Dict[int, Optional[str]]:
    """`pin_move_refusal` without a point for every address of the customer, batched: published on
    `GET /auth/addresses` before the customer acts, so it is never "TOO_FAR"."""
    addresses = UserAddress.query.filter(UserAddress.user_id == user_id).all()
    facts = _pin_facts(addresses)
    return {address.id: _refusal(address, facts[address.id], None, None) for address in addresses}


def _message(key: str, english: str) -> str:
    message = get_translation(key)
    return english if message == key else message


def assert_pin_movable(address: UserAddress, latitude, longitude, *, actor_id: int) -> None:
    """Raise the customer pin rule that refuses moving the address's pin to the point (400), else return."""
    facts = _pin_facts([address])[address.id]
    reason = _refusal(address, facts, latitude, longitude)
    if reason is None:
        return
    logger.info("[ADDRESS_PIN] refused user=%s address=%s reason=%s", actor_id, address.id, reason)
    if reason == "WITH_DRIVER":
        raise ValidationError(
            _message(
                "api.addresses.error.pin_with_driver",
                "Your order is already on its way to this address. You can move its pin as soon as it's delivered.",
            ),
            error_code="ADDRESS_PIN_WITH_DRIVER",
            details={"reason": reason, "order_number": facts.with_driver_order_number},
        )
    if reason == "SHARED_PLACE":
        raise ValidationError(
            _message(
                "api.addresses.error.pin_shared_place",
                "This address is shared with other customers, so our team looks after its pin. "
                "Write to us in Support and we'll move it for you.",
            ),
            error_code="ADDRESS_PIN_SHARED_PLACE",
            details={"reason": reason},
        )
    if reason == "OUTLET":
        raise ValidationError(
            _message(
                "api.addresses.error.pin_outlet",
                "This address belongs to a business branch, so our team looks after its pin. "
                "Write to us in Support and we'll move it for you.",
            ),
            error_code="ADDRESS_PIN_OUTLET",
            details={"reason": reason},
        )
    raise ValidationError(
        _message(
            "api.addresses.error.move_too_far",
            "That's a new place! Add it as a new address, so each place keeps its own bottle count.",
        ),
        error_code="ADDRESS_MOVE_TOO_FAR",
        details={"reason": reason},
    )


def assert_not_duplicate(
    user_id: int,
    latitude,
    longitude,
    *,
    apartment_number=None,
    exclude_address_id=None,
    confirmed: bool,
    offer_move: bool = False,
) -> None:
    """Ask the duplicate question: 409 `ADDRESS_POSSIBLE_DUPLICATE` naming the candidates, unless there are none or
    the customer confirmed (`confirmed is True`; any other value asks). `move_address_id` is the nearest candidate,
    offered only with `offer_move` and only when its pin may move to the point."""
    candidates = possible_duplicates(
        user_id, latitude, longitude, apartment_number=apartment_number, exclude_address_id=exclude_address_id
    )
    if not candidates:
        return
    if confirmed is True:
        near = ",".join(str(address.id) for address, _ in candidates)
        logger.info("[ADDRESS_DUP] confirmed user=%s near=[%s]", user_id, near)
        return
    nearest = candidates[0][0]
    movable = offer_move and pin_move_refusal(nearest, latitude, longitude) is None
    raise ConflictError(
        _message("api.addresses.error.possible_duplicate", "You already have an address saved at this spot."),
        error_code="ADDRESS_POSSIBLE_DUPLICATE",
        details={
            "move_address_id": nearest.id if movable else None,
            "candidates": [{**address.to_dict(), "distance_m": distance} for address, distance in candidates],
        },
    )


def take_confirm_flag(body: dict) -> bool:
    """Pop `confirm_duplicate`: only a JSON `true` confirms. It is never stored and never reaches a model."""
    return body.pop("confirm_duplicate", None) is True


def take_point_source(body: dict) -> Optional[str]:
    """Pop `point_source` (`map_pin`, `gps`, `search` or `typed`; never stored). A non-string value is no source."""
    point_source = body.pop("point_source", None)
    return point_source if isinstance(point_source, str) else None
