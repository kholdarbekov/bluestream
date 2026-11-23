"""The one path a saved address's pin moves by (spec 2026-11-18-duplicate-address-guard §6.3).

The customer's move route (`move`), the customer's, the legacy and the admin address update (`edit`) decide "does
the pin change?" here and nowhere else: when it does they go through `change_pin`, else through the plain write,
which never carries a point. `change_pin` asks the zone, then (customers only) the pin rules, then the duplicate
question, and hands over to `apply_pin_change`, which locks every open order to the address and then the address
itself, asks the pin rules again under those locks, writes the address, and runs the admin address change's own
destination-changed steps on each order: the driver holding the stop is told the pin moved, and the order's history
records it. The try-out conversion's first pin is a staff `change_pin`.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy.exc import InvalidRequestError

from business_app import db
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.address_guard_service import (
    ADDRESS_DETAIL_FIELDS,
    ADDRESS_LOCATION_FIELDS,
    as_point,
    assert_not_duplicate,
    assert_pin_movable,
    distance_m,
    pin_changed,
)
from business_app.services.order_address_change_service import (
    POST_COMMIT_FAILED_EVENT,
    OrderAddressChangeService,
)
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils.address_helpers import get_address_line
from business_app.utils.exceptions import NotFoundError
from business_app.utils.geo_validation import ensure_within_delivery_zone
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.translations import get_translation


# What each door's address edit may write; the pin in it travels only through `change_pin`. The admin edit and the
# legacy PUT (`PUT /api/v1/addresses/<id>`):
UPDATABLE_FIELDS = (
    "title",
    "full_address",
    "street_address",
    "city",
    "district",
    "postal_code",
    "country",
    "latitude",
    "longitude",
    "is_business",
    "delivery_instructions",
    "landmark",
    "floor_number",
    "apartment_number",
)
# The customer's own edit (`PUT /api/v1/auth/addresses/<id>`, the website and the bot) never wrote the country.
CUSTOMER_UPDATABLE_FIELDS = tuple(field for field in UPDATABLE_FIELDS if field != "country")
_PIN_FIELDS = ("latitude", "longitude")


def _carried(value: Any) -> bool:
    """A body or stored value that says something: not missing, and not blank once stripped."""
    return value is not None and bool(str(value).strip())


class AddressPinService:
    # The history row's fixed internal note, as the subscription move writes "subscription address change".
    CUSTOMER_REASON = "customer moved the address pin"
    ADMIN_REASON = "admin edited the address pin"

    @staticmethod
    def move_fields(address: UserAddress, body: Mapping[str, Any]) -> Dict[str, Any]:
        """What a move writes (the §6.3 table): the pin, as numbers; each part of the written line the body carries;
        and each door detail the saved address lacks. Nothing else: never the title, the default flag, the city or
        the id. The caller has checked that the body carries a placed pin."""
        fields: Dict[str, Any] = {"latitude": float(body["latitude"]), "longitude": float(body["longitude"])}
        for field in ADDRESS_LOCATION_FIELDS:
            if _carried(body.get(field)):
                fields[field] = body[field]
        for field in ADDRESS_DETAIL_FIELDS:
            if not _carried(getattr(address, field)) and _carried(body.get(field)):
                fields[field] = body[field]
        return fields

    @classmethod
    def edit(
        cls,
        address: UserAddress,
        data: Mapping[str, Any],
        *,
        actor: User,
        customer_rules: bool,
        confirm_duplicate: bool,
        fields: Sequence[str] = UPDATABLE_FIELDS,
    ) -> UserAddress:
        """Write an edit body; `fields` is what the caller's door may write (`UPDATABLE_FIELDS`, or
        `CUSTOMER_UPDATABLE_FIELDS` for the customer's own edit). The caller commits."""
        return cls._write(
            address,
            {k: data[k] for k in fields if k in data},
            actor=actor,
            customer_rules=customer_rules,
            confirm_duplicate=confirm_duplicate,
        )

    @classmethod
    def move(
        cls, address: UserAddress, body: Mapping[str, Any], *, actor: User, confirm_duplicate: bool
    ) -> UserAddress:
        """Write a customer's move body (`move_fields`) under the customer pin rules. The caller has checked that the
        body carries a placed pin, and commits."""
        return cls._write(
            address,
            cls.move_fields(address, body),
            actor=actor,
            customer_rules=True,
            confirm_duplicate=confirm_duplicate,
        )

    @classmethod
    def _write(
        cls, address: UserAddress, fields: Dict[str, Any], *, actor: User, customer_rules: bool, confirm_duplicate: bool
    ) -> UserAddress:
        """The one branch every edit and move takes: a pin that changes goes through `change_pin`; anything else is
        the plain write, which never carries a point. A null, invalid or sub-metre point clears or moves nothing:
        written plainly, it would skip every rule, and repeated small steps could walk a pin under a carried order."""
        if pin_changed(address, fields):
            return cls.change_pin(
                address, fields, actor=actor, customer_rules=customer_rules, confirm_duplicate=confirm_duplicate
            )
        cls._write_plain(address, fields)
        return address

    @staticmethod
    def _write_plain(address: UserAddress, fields: Mapping[str, Any]) -> None:
        for field, value in fields.items():
            if field not in _PIN_FIELDS:
                setattr(address, field, value)

    @staticmethod
    def _lock_address(address: UserAddress) -> None:
        """Lock the address row and read it again (the last lock a pin change takes: delivery, order, address). A move
        that committed while this one queued is the pin this one starts from. A row deleted since the checks is a
        404, as a missing one is at the door."""
        try:
            db.session.refresh(address, with_for_update=True)
        except InvalidRequestError as gone:
            raise NotFoundError(get_translation("api.auth.address_not_found")) from gone

    @classmethod
    def change_pin(
        cls,
        address: UserAddress,
        fields: Dict[str, Any],
        *,
        actor: User,
        customer_rules: bool,
        confirm_duplicate: bool,
    ) -> UserAddress:
        """Move the address's pin and write `fields`, after the checks in their fixed order: the delivery zone (400),
        the customer pin rules when `customer_rules` (400), the duplicate question (409; `confirm_duplicate` answers
        only that). Callers call it only when `pin_changed(address, fields)`. A half pair is read against the stored
        other half."""
        latitude = fields.get("latitude", address.latitude)
        longitude = fields.get("longitude", address.longitude)
        ensure_within_delivery_zone(latitude, longitude)
        if customer_rules:
            assert_pin_movable(address, latitude, longitude, actor_id=actor.id)
        assert_not_duplicate(
            address.user_id,
            latitude,
            longitude,
            apartment_number=fields.get("apartment_number", address.apartment_number),
            exclude_address_id=address.id,
            confirmed=confirm_duplicate,
        )
        return cls.apply_pin_change(address, fields, actor=actor, customer_rules=customer_rules)

    @classmethod
    def apply_pin_change(
        cls, address: UserAddress, fields: Dict[str, Any], *, actor: User, customer_rules: bool
    ) -> UserAddress:
        """Write `fields` and redirect every open order to the moved pin, in one transaction (§6.3 steps 1-7).

        Each open order (status in `ACTIVE_ORDER_STATUSES`, scheduled future orders included) is locked delivery
        first, then order, in ascending id: the lock order of the driver's taps and of the admin move, so they queue
        instead of deadlocking. An order that closed or moved to another address meanwhile is left alone. The address
        row is locked last and read again, so two moves of one address queue even without orders, and the second
        starts from the pin the first wrote: its history, audit, `pin_moved_m` and pin rules read that pin, and a
        second move to the same pin is a no-op. With `customer_rules` the pin rules are asked again under the locks,
        so a pickup that committed first refuses the move (400 `ADDRESS_PIN_WITH_DRIVER`) and nothing is written.
        After the commit each order's outcome goes through `OrderAddressChangeService.run_post_commit`, and one audit
        event records the address's own move: the trace of the past orders that now show the moved place.
        """
        from business_app.utils.transactions import atomic_transaction, best_effort_step

        open_order_ids = [
            order_id
            for (order_id,) in db.session.query(Order.id)
            .filter(Order.delivery_address_id == address.id, Order.status.in_(ACTIVE_ORDER_STATUSES))
            .order_by(Order.id)
        ]
        with atomic_transaction():
            locked = []
            for order_id in open_order_ids:
                delivery, order = OrderScheduleService.lock_delivery_then_order(order_id)
                if order.status in ACTIVE_ORDER_STATUSES and order.delivery_address_id == address.id:
                    locked.append((order, delivery))
            cls._lock_address(address)
            if not pin_changed(address, fields):
                # A move to this same pin committed while this one waited: nothing moves now, so nothing is
                # recorded, sent or audited, and the rest of `fields` is the plain write.
                cls._write_plain(address, fields)
                return address
            new = as_point(fields.get("latitude", address.latitude), fields.get("longitude", address.longitude))
            if customer_rules:
                assert_pin_movable(address, new.latitude, new.longitude, actor_id=actor.id)
            before = OrderAddressChangeService.snapshot(address)
            old_line = get_address_line(address)
            old = as_point(address.latitude, address.longitude)
            pin_moved_m = round(distance_m(old, new)) if old is not None else None

            for field, value in fields.items():
                setattr(address, field, value)
            # The pin is stored as the point the rules read: a numeric string is written as the number.
            address.latitude, address.longitude = new.latitude, new.longitude

            now = get_utc_now()
            outcomes = [
                OrderAddressChangeService.destination_changed(
                    order,
                    delivery,
                    before=before,
                    after=address,
                    old_line=old_line,
                    actor=actor,
                    reason=cls.CUSTOMER_REASON if customer_rules else cls.ADMIN_REASON,
                    now=now,
                    extra_diff={"pin_moved_m": pin_moved_m},
                )
                for order, delivery in locked
            ]

        for outcome in outcomes:
            OrderAddressChangeService.run_post_commit(outcome)
        best_effort_step(
            POST_COMMIT_FAILED_EVENT,
            None,
            "address_audit",
            cls._log_address_audit,
            address,
            before,
            actor,
            pin_moved_m,
            [outcome.order_id for outcome in outcomes],
        )
        return address

    @staticmethod
    def _log_address_audit(
        address: UserAddress,
        before: Dict[str, Any],
        actor: User,
        pin_moved_m: Optional[int],
        order_ids: List[int],
    ) -> None:
        """After the commit, so only a move that happened is ever recorded (the logger commits)."""
        from business_app.utils.audit_logger import AuditEventType, audit_logger

        audit_logger.log_event(
            event_type=AuditEventType.USER_UPDATED,
            action="address_pin_moved",
            resource_type="address",
            resource_id=str(address.id),
            old_values=before,
            new_values=OrderAddressChangeService.snapshot(address),
            additional_data={
                "user_id": address.user_id,
                "actor_id": actor.id,
                "pin_moved_m": pin_moved_m,
                "order_ids": order_ids,
            },
        )
