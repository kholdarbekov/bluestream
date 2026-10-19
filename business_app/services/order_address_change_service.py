"""Admin order address change: the predicates every surface shares, and the write.

`block_code` answers "may this order's address change". The options list, the
`can_change_address` flag and the write guard all ask it, so the button an admin
sees and the write it sends cannot disagree. `_disabled_reason` does the same for
each address: the dropdown greys exactly what `change` refuses.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderEditHistory
from business_app.models.sales import Outlet
from business_app.models.user import User, UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.sales import pay_rules
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.address_helpers import door_fields, get_address_line
from business_app.utils.decorators import has_admin_permission
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.staff_order_info import build_staff_order_info
from business_app.utils.geo_validation import is_in_delivery_zone
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import PaymentMethod

# The admin permissions that may change an order's address (any one is enough).
ADDRESS_CHANGE_PERMISSIONS: Tuple[str, ...] = ("manage_orders", "edit_orders")

# The log line a failed post-commit step writes (`best_effort_step`).
POST_COMMIT_FAILED_EVENT = "order_address_change_post_commit_failed"


@dataclass(frozen=True)
class AddressChangeOutcome:
    """One committed address change, as its post-commit side effects need it.

    Captured under the locks: who held the stop is decided before the write, and
    everything that tells anyone runs after the commit.
    """

    order_id: int
    order_number: str
    delivery_id: Optional[int]
    # The driver whose route holds the stop (D2), and their chat when they have one.
    driver_id: Optional[int]
    driver_telegram_id: Optional[int]
    # The outlets that gain or lose the order (D13). Empty unless the branch changes.
    moved_outlet_ids: Tuple[int, ...]
    # The "Address changed" payload, built after the write; None unless the driver can be told.
    driver_payload: Optional[Dict[str, Any]]
    # The audit event's `additional_data`: the history diff, the order number and the history id.
    audit: Dict[str, Any]


class AddressChangeResult(NamedTuple):
    """What `change` hands the route: the order as committed, and what the write decided."""

    order: Order
    driver_notified: bool
    self_decided: bool


class OrderAddressChangeService:
    @classmethod
    def block_code(cls, order: Order) -> Optional[str]:
        """Why this order's address cannot change, or None when it can.

        The status window is the reschedule's own (`schedule_window_block`), so an
        order that may be moved to another day may also be moved to another address.
        A pending same-day agent hold comes last: only the approval queue acts on it.
        """
        from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService

        window = OrderScheduleService.schedule_window_block(order)
        if window == "order":
            return "ORDER_ADDRESS_LOCKED"
        if window == "delivery":
            return "DELIVERY_ADDRESS_LOCKED"
        if AgentOrderApprovalService.is_awaiting(order.id):
            return "ORDER_AWAITING_STAFF_APPROVAL"
        return None

    @classmethod
    def assert_open(cls, order: Order) -> None:
        """The write guard: raise the refusal `block_code` names, or return."""
        code = cls.block_code(order)
        if code is None:
            return
        if code == "ORDER_AWAITING_STAFF_APPROVAL":
            from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService

            AgentOrderApprovalService.assert_not_awaiting(order)
            return
        # Literal codes, one per site, so the error-code coverage scan sees each raise.
        if code == "ORDER_ADDRESS_LOCKED":
            raise ValidationError("This order's address can no longer change", error_code="ORDER_ADDRESS_LOCKED")
        raise ValidationError("This order's address can no longer change", error_code="DELIVERY_ADDRESS_LOCKED")

    # ------------------------------------------------------------------ options
    @staticmethod
    def moved_outlets(current: Sequence[Outlet], target: Sequence[Outlet]) -> Set[Outlet]:
        """The outlets that gain or lose this order. Outlets on both sides are untouched.

        R13's pay subjects and D13's replenishment recompute both read this one set, so the
        guard checks exactly the outlets whose due dates are republished.
        """
        return set(current) ^ set(target)

    @classmethod
    def pay_subjects(cls, current: Sequence[Outlet], target: Sequence[Outlet]) -> Set[int]:
        """Whose pay the move changes: the owner and the onboarder of every moved outlet (R13)."""
        return {
            uid
            for outlet in cls.moved_outlets(current, target)
            for uid in (OutletService.due_owner_id(outlet), outlet.onboarded_by_user_id)
        } - {None}

    @classmethod
    def _disabled_reason(
        cls, order: Order, address: UserAddress, *, actor: User, current: List[Outlet], target: List[Outlet]
    ) -> Optional[str]:
        """Why this address cannot take the order, or None. The first match wins."""
        if address.id == order.delivery_address_id:
            return "CURRENT"
        if address.latitude is None or address.longitude is None:
            return "NO_PIN"
        if not is_in_delivery_zone(address.latitude, address.longitude):
            return "OUTSIDE_ZONE"
        if order.payment_method == PaymentMethod.CASH and CashCollectionService().place_cod_capped(
            order.user_id, address.id
        ):
            return "COD_PLACE_CAPPED"
        if any(pay_rules.self_decision_refused(subject, actor) for subject in cls.pay_subjects(current, target)):
            return "SALES_PAY_SELF_DECISION"
        return None

    @staticmethod
    def _branch_view(outlets: List[Outlet], address_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """The outlet an order at `address_id` counts under, with the agent who owns it."""
        outlet = next((o for o in outlets if o.address_id == address_id), outlets[0] if outlets else None)
        if outlet is None:
            return None
        agent_id = OutletService.due_owner_id(outlet)
        agent = User.query.get(agent_id) if agent_id is not None else None
        return {
            "outlet_id": outlet.id,
            "outlet_name": outlet.name,
            "agent_id": agent_id,
            "agent_name": agent.full_name if agent is not None else None,
        }

    @staticmethod
    def _driver_view(delivery: Optional[Delivery]) -> Optional[Dict[str, Any]]:
        """The driver whose route still carries the stop, or None (a failed stop names nobody)."""
        driver_id = OrderScheduleService.driver_holding_stop(delivery)
        if driver_id is None:
            return None
        driver = User.query.get(driver_id)
        return {"id": driver_id, "name": (driver.full_name if driver is not None else "") or ""}

    @classmethod
    def options(cls, order: Order, *, actor: User) -> List[Dict[str, Any]]:
        """Every saved address of the customer, each with whether it can take this order and why not."""
        current = OutletService.outlets_for_delivery(order.user_id, order.delivery_address_id)
        current_owners = {OutletService.due_owner_id(o) for o in current}
        addresses = (
            UserAddress.query.filter_by(user_id=order.user_id)
            .order_by(UserAddress.is_default.desc(), UserAddress.id.asc())
            .all()
        )
        options = []
        for address in addresses:
            target = OutletService.outlets_for_delivery(order.user_id, address.id)
            reason = cls._disabled_reason(order, address, actor=actor, current=current, target=target)
            options.append(
                {
                    **address.to_dict(),
                    "selectable": reason is None,
                    "disabled_reason": reason,
                    "branch": cls._branch_view(target, address.id),
                    "agent_differs": current_owners != {OutletService.due_owner_id(o) for o in target},
                }
            )
        return options

    @classmethod
    def options_payload(cls, order_id: int, *, actor: User) -> Dict[str, Any]:
        """The `GET /address-options` answer. A locked or held order refuses with its own code."""
        order = Order.query.get(order_id)
        if order is None:
            raise NotFoundError("Order not found", error_code="ORDER_NOT_FOUND")
        cls.assert_open(order)
        current = OutletService.outlets_for_delivery(order.user_id, order.delivery_address_id)
        return {
            "options": cls.options(order, actor=actor),
            "current_address_id": order.delivery_address_id,
            "current_branch": cls._branch_view(current, order.delivery_address_id),
            "driver": cls._driver_view(order.delivery),
        }

    @classmethod
    def metadata(cls, order: Order, *, actor: Optional[User]) -> Dict[str, Any]:
        """The detail payload's address-change block: the button's state and the driver it warns about."""
        code = cls.block_code(order)
        return {
            "can_change_address": code is None and has_admin_permission(actor, ADDRESS_CHANGE_PERMISSIONS),
            "change_address_block_code": code,
            "change_address_driver": cls._driver_view(order.delivery) if code is None else None,
        }

    # ------------------------------------------------------------------ the write
    @classmethod
    def change(cls, order_id: int, *, address_id: int, note: Optional[str], actor: User) -> AddressChangeResult:
        """Move the order to another saved address of its own customer (the admin PATCH).

        One transaction. It locks the delivery, then the order, in the reschedule's order, and
        re-asks `block_code` and the option predicate for this one address. It captures the driver
        holding the stop, then writes the address, the distance and pool ETA (D11), the copied-notes
        cleanup (D12), the history row (D6) and the stale route metrics (D9). The delivery status,
        the driver, the price and the commission subjects never move (R5, R9, R12), and nothing
        customer-visible is written (R4). After the commit, `_run_post_commit` runs.

        Raises NotFoundError (ORDER_NOT_FOUND), ConflictError (ORDER_AWAITING_STAFF_APPROVAL),
        ForbiddenError (SALES_PAY_SELF_DECISION), or ValidationError with ORDER_ADDRESS_LOCKED,
        DELIVERY_ADDRESS_LOCKED, INVALID_DELIVERY_ADDRESS, ADDRESS_CURRENT, ADDRESS_NO_PIN,
        ADDRESS_OUTSIDE_ZONE or COD_DEBT_LIMIT_REACHED. Nothing is written when it raises.
        """
        from business_app.services.delivery_service import DeliveryService
        from business_app.services.order_service import OrderService
        from business_app.services.route_optimization_service import RouteOptimizationService
        from business_app.utils.transactions import atomic_transaction

        with atomic_transaction():
            delivery, order = OrderScheduleService.lock_delivery_then_order(order_id)
            cls.assert_open(order)
            # A foreign address is refused here (INVALID_DELIVERY_ADDRESS); it is never an option.
            _, new = OrderService().get_user_and_address_for_order(order.user_id, address_id)
            old, old_id = order.delivery_address, order.delivery_address_id  # None on a legacy order (RF4)
            current = OutletService.outlets_for_delivery(order.user_id, old_id)
            target = OutletService.outlets_for_delivery(order.user_id, new.id)
            cls._refuse_unpickable(cls._disabled_reason(order, new, actor=actor, current=current, target=target))
            # R13: a non-admin pay subject is refused here (SALES_PAY_SELF_DECISION, the option's
            # reason); an admin one is recorded. A list, so every subject is checked.
            self_decided = any(
                [pay_rules.check_self_decision(subject, actor) for subject in sorted(cls.pay_subjects(current, target))]
            )

            # The driver on the stop, captured before the write (D2).
            driver_id = OrderScheduleService.driver_holding_stop(delivery)
            driver = db.session.get(User, driver_id) if driver_id is not None else None
            driver_telegram_id = int(driver.telegram_id) if driver is not None and driver.telegram_id else None
            driver_notified = driver_telegram_id is not None

            now = get_utc_now()
            order.delivery_address = new
            if delivery is not None:
                deliveries = DeliveryService()
                delivery.distance_km = deliveries.distance_km(new)
                # A pool row's ETA follows the new distance; a driver-held row keeps its own.
                deliveries.refresh_pool_eta(delivery, now=now)
            # D12: subscription billing copied the old address's instructions into the notes. The
            # driver card shows the live address's instructions, so the copy goes; a typed note stays.
            if (
                old is not None
                and old.delivery_instructions is not None
                and order.delivery_notes == old.delivery_instructions
            ):
                order.delivery_notes = None
            diff = {
                "kind": "address_change",
                "address_before": cls._snap(old),
                "address_after": cls._snap(new),
                "driver_notified": driver_notified,
                "branch_before": cls._branch_view(current, old_id),
                "branch_after": cls._branch_view(target, new.id),
                "self_decided": self_decided,
            }
            history = OrderEditHistory(
                order_id=order.id,
                edited_by_user_id=actor.id,
                edited_at=now,
                reason=(note or "").strip(),
                is_post_delivery=False,
                diff=diff,
            )
            db.session.add(history)
            db.session.flush()
            if driver_id is not None:
                routes = RouteOptimizationService()
                route = routes.current_route(driver_id)
                if route is not None:
                    routes.mark_metrics_stale(route)
            driver_payload = cls._driver_payload(delivery, old, new, driver_telegram_id) if driver_notified else None
            outcome = AddressChangeOutcome(
                order_id=order.id,
                order_number=order.order_number,
                delivery_id=delivery.id if delivery is not None else None,
                driver_id=driver_id,
                driver_telegram_id=driver_telegram_id,
                moved_outlet_ids=tuple(sorted(outlet.id for outlet in cls.moved_outlets(current, target))),
                driver_payload=driver_payload,
                audit={"order_number": order.order_number, "history_id": history.id, **diff},
            )

        cls._run_post_commit(outcome)
        return AddressChangeResult(db.session.get(Order, order_id), driver_notified, self_decided)

    @staticmethod
    def _driver_payload(
        delivery: Delivery, old: Optional[UserAddress], new: UserAddress, telegram_id: int
    ) -> Dict[str, Any]:
        """The "Address changed" message body. Built after the address write, so the order info
        already names the new place; an empty old line is a legacy order that had none (RF4)."""
        return {
            "telegram_id": telegram_id,
            "delivery_id": delivery.id,
            "delivery_address_id": new.id,
            "order_info": build_staff_order_info(delivery),
            "old_address": get_address_line(old),
            "new_address": get_address_line(new),
            "lat": new.latitude,
            "lng": new.longitude,
            "door": door_fields(new),
        }

    @staticmethod
    def _refuse_unpickable(reason: Optional[str]) -> None:
        """The write's refusal for an address the options grey out, one literal code per site.

        SALES_PAY_SELF_DECISION falls through: `pay_rules.check_self_decision` raises it next,
        from the same `self_decision_refused` the option asked. Any other reason fails closed, so
        a reason added to `_disabled_reason` without a code here can never be greyed yet saved.
        """
        if reason == "CURRENT":
            raise ValidationError("The order already goes to this address", error_code="ADDRESS_CURRENT")
        if reason == "NO_PIN":
            raise ValidationError("This address has no map pin", error_code="ADDRESS_NO_PIN")
        if reason == "OUTSIDE_ZONE":
            raise ValidationError("This address is outside the delivery area", error_code="ADDRESS_OUTSIDE_ZONE")
        if reason == "COD_PLACE_CAPPED":
            raise ValidationError(
                "Cash on delivery is unavailable at this address: its workplace has reached the cash debt limit",
                error_code="COD_DEBT_LIMIT_REACHED",
            )
        if reason not in (None, "SALES_PAY_SELF_DECISION"):
            raise RuntimeError(f"unmapped address-change reason {reason}")

    @staticmethod
    def _snap(address: Optional[UserAddress]) -> Optional[Dict[str, Any]]:
        """The history's picture of an address (D6). Read off the model, so (0.0, 0.0) stays a pin."""
        if address is None:
            return None
        return {
            "id": address.id,
            "title": address.title,
            "full_address": address.full_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        }

    @classmethod
    def _run_post_commit(cls, outcome: AddressChangeOutcome) -> None:
        """The side effects of a committed address change. The only post-commit runner.

        Each step is isolated by `best_effort_step`, so one outage cannot swallow the steps after it.
        """
        from business_app.services.route_optimization_service import ADDRESS_CHANGE_TRIGGER
        from business_app.tasks.delivery_tasks import optimize_driver_route_task
        from business_app.tasks.staff_tasks import notify_order_address_changed
        from business_app.utils import bot_webhook
        from business_app.utils.transactions import best_effort_step

        if outcome.driver_id is not None:
            # Independent of each other: a broker outage on the notice must not stop the card
            # refresh or the re-sort. Only the notice makes a sound.
            if outcome.driver_payload is not None:
                best_effort_step(
                    POST_COMMIT_FAILED_EVENT,
                    outcome.order_id,
                    "driver_notice",
                    notify_order_address_changed.delay,
                    outcome.delivery_id,
                    outcome.driver_id,
                    outcome.driver_payload,
                )
            best_effort_step(
                POST_COMMIT_FAILED_EVENT,
                outcome.order_id,
                "route_refresh",
                lambda: bot_webhook.notify_route_updated(
                    outcome.driver_id, sound=False, trigger=ADDRESS_CHANGE_TRIGGER
                ),
            )
            best_effort_step(
                POST_COMMIT_FAILED_EVENT,
                outcome.order_id,
                "route_optimize",
                optimize_driver_route_task.delay,
                outcome.driver_id,
                ADDRESS_CHANGE_TRIGGER,
            )
        if outcome.moved_outlet_ids:
            best_effort_step(
                POST_COMMIT_FAILED_EVENT,
                outcome.order_id,
                "replenishment",
                cls._recompute_outlets,
                outcome.moved_outlet_ids,
            )
        best_effort_step(POST_COMMIT_FAILED_EVENT, outcome.order_id, "audit", cls._log_audit, outcome)

    @staticmethod
    def _recompute_outlets(outlet_ids: Tuple[int, ...]) -> None:
        """D13: the old and the new branch's incoming water moved at once, so their due dates are
        republished now rather than at the 01:00 job."""
        from business_app.services.sales.replenishment_service import ReplenishmentService

        for outlet_id in outlet_ids:
            ReplenishmentService.recompute_outlet(db.session.get(Outlet, outlet_id))
        db.session.commit()

    @staticmethod
    def _log_audit(outcome: AddressChangeOutcome) -> None:
        """After the commit, so only a change that happened is ever recorded (the logger commits)."""
        from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger

        audit_logger.log_event(
            event_type=AuditEventType.ORDER_UPDATED,
            action="order_address_changed",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(outcome.order_id),
            additional_data=outcome.audit,
        )
