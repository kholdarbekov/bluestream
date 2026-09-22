"""Who confirms an order a sales agent placed on a store's behalf.

Three outcomes, decided HERE and published as one `state` string so neither bot
re-derives it (D15):

* ``auto_confirmed`` — ``create_order``'s instant-COD block already confirmed
  the order (a returning store), so there is nothing left to ask and a request
  row would be a question nobody would ever answer;
* ``pending_confirmation`` — the store has a Telegram account, so it is asked
  in the customer bot and the order stays PENDING until it answers, or until
  the expiry sweep confirms it;
* ``confirmed`` — there is nobody to ask, so the order is confirmed on the spot
  and the driver pool sees it at once (the phone-order behaviour).

Task 5b extends this module with the customer's answer (``respond``), the
pending-request filter, the expiry sweep and the real push. ``_enqueue_push``
is the seam it replaces — a module-level function, not a method, so the
replacement is one line and so the after-commit dispatch point is watchable
from a test.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from flask import current_app

from business_app import db
from business_app.models.order import Order
from business_app.models.sales import Outlet
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.serializers.order_serializers import serialize_order
from business_app.services.inventory_service import get_inventory_service
from business_app.services.order_service import OrderService
from business_app.services.sales import notifications
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import OrderStatus, PaymentMethod
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_CONFIRMED, SALES_EVENT_AGENT_ORDER_DECLINED

logger = logging.getLogger(__name__)

# The three answers `open_or_confirm` can give. One definition, because the staff bot renders a
# different line for each and the admin drawer reads the same word: a fourth state invented in
# the service, or a dropped one, must break a pin rather than a screen in Tashkent.
#
# DELIBERATELY NOT `CONFIRMATION_STATUSES` (models/sales_visits.py), which are the statuses of the
# request ROW — pending / confirmed / declined / expired. The two vocabularies overlap on the word
# "confirmed" and mean different things by it: a STATE of "confirmed" says there was nobody to ask
# and the backend confirmed the order itself, while a request ROW of "confirmed" says the store
# answered yes. Merging them would lose that distinction; the names are unpacked from the tuple
# below so the tuple is what the returns are made of.
CONFIRMATION_STATES = ("auto_confirmed", "pending_confirmation", "confirmed")
STATE_AUTO_CONFIRMED, STATE_PENDING_CONFIRMATION, STATE_CONFIRMED = CONFIRMATION_STATES


def _enqueue_push(order_id: int) -> None:
    """After-commit hand-off to the customer bot (D18).

    Imported lazily: the tasks module imports services, so a module-level import
    here would close the loop.
    """
    from business_app.tasks.sales_agent_tasks import push_agent_order_confirmation

    push_agent_order_confirmation.delay(order_id)


class AgentOrderConfirmationService:
    """Confirmation policy for orders created by an agent at the door."""

    @staticmethod
    def needs_request(outlet: Outlet) -> bool:
        """True when there is somebody to ask: the store has a Telegram account."""
        return bool(outlet.user and outlet.user.telegram_id)

    @staticmethod
    def _hold_stock_for_the_question(order: Order, ttl_hours) -> None:
        """Keep the inventory reserved for as long as the store has to answer (M28).

        `create_order` holds these units for `INVENTORY_RESERVATION_TTL` — 30 minutes, sized for
        a checkout somebody is still typing. An agent order asked back to the shop instead waits
        `SALES_CONFIRMATION_TTL_HOURS` and is then CONFIRMED either way, which decrements the
        stock: for the hours in between the water was held by nobody, so another channel could
        sell it and the confirmed order had nothing behind it. Re-stamped with the window's own
        number, read from the SAME config key that sets `expires_at` above, so the hold and the
        question cannot be different lengths.

        WHICH lines to hold is `create_order`'s question, and it is asked through
        `create_order`'s own answer rather than re-derived here. A cash order draws no marking
        code, so a marking-code product's pool does not gate it and `create_order` never
        reserved it: holding it anyway stamped a phantom hold on a pool `confirm_reservations`
        never looks at, and made the whole re-stamp all-or-nothing against stock that has
        nothing to do with this order — so a short code pool silently cost the PLAIN lines
        their extended hold too.

        EXTENDING, not re-reserving (C00). `reserve_inventory` rolls back on any error by
        releasing the order's whole namespace, which cannot distinguish the keys it just
        wrote from the 30-minute keys `create_order` wrote milliseconds earlier — so a Redis
        drop between two of its writes DESTROYED the hold this call exists to lengthen.
        `extend_reservations` only ever stamps a longer expiry onto keys that already exist.

        Best effort, and deliberately not a failure — including when it RAISES. The order and
        the request row are already committed and the store has still to be asked, so an
        exception escaping here (a Redis outage raises out of the client itself) would turn
        the agent's POST into a 500 over an order that really exists, with the store never
        asked. Because nothing here deletes, every outcome — a refusal, a raise, a fault
        part-way through the lines — leaves the hold `create_order` left, so the worst case
        really is the old 30 minutes plus a line in the log, and never less.
        """
        # Private on purpose, and called rather than copied: "which lines gate this order" must
        # have exactly one implementation, and it is `create_order`'s.
        is_cash = order.payment_method == PaymentMethod.CASH if order.payment_method else False
        items = OrderService()._stock_gated_items(
            [{"product_id": item.product_id, "quantity": int(item.quantity)} for item in order.order_items],
            is_cash=is_cash,
        )
        if not items:
            return
        try:
            result = get_inventory_service().extend_reservations(
                order_id=order.id,
                items=items,
                ttl=int(ttl_hours) * 3600,
            )
        except Exception:
            logger.exception(
                "Inventory hold for agent order %s could not be extended to the confirmation window",
                order.id,
            )
            return
        if not result.get("success"):
            logger.warning(
                "Inventory hold for agent order %s not extended to the confirmation window: %s",
                order.id,
                result.get("reason"),
            )

    @staticmethod
    def open_or_confirm(order: Order, outlet: Outlet, agent_user_id: int) -> str:
        """Decide who confirms this order, act on it, and name the outcome."""
        if order.status != OrderStatus.PENDING:
            # `create_order` already confirmed it (returning-customer COD). Nothing to ask.
            return STATE_AUTO_CONFIRMED

        if AgentOrderConfirmationService.needs_request(outlet):
            now = get_utc_now()
            ttl_hours = current_app.config["SALES_CONFIRMATION_TTL_HOURS"]
            request = OrderConfirmationRequest(
                order_id=order.id,
                outlet_id=outlet.id,
                status="pending",
                requested_at=now,
                expires_at=now + timedelta(hours=ttl_hours),
                # 16 hex chars: the dedup key the customer bot echoes back as X-Request-ID.
                request_id=secrets.token_hex(8),
            )
            db.session.add(request)
            db.session.commit()
            AgentOrderConfirmationService._hold_stock_for_the_question(order, ttl_hours)
            _enqueue_push(order.id)
            return STATE_PENDING_CONFIRMATION

        OrderService().update_order_status(
            order.id,
            OrderStatus.CONFIRMED,
            updated_by=agent_user_id,
            notes="Confirmed at sales visit",
        )
        return STATE_CONFIRMED

    @staticmethod
    def pending_request_filter():
        """NOT EXISTS clause for "this order is still the store's to answer".

        Correlates on `Order.id`, so it drops straight into any `Order.query`.
        Published as a clause rather than restated in the task because the rule
        ("a pending request owns the order") must have exactly one definition.
        """
        return ~(
            db.session.query(OrderConfirmationRequest.id)
            .filter(
                OrderConfirmationRequest.order_id == Order.id,
                OrderConfirmationRequest.status == "pending",
            )
            .exists()
        )

    @staticmethod
    def respond(order_id: int, user_id: int, action: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """The store's own answer, from the customer bot.

        `get_order(order_id, user_id)` is the ownership gate: another customer's
        order is a 404, never a 403 that confirms the order exists.
        """
        order_service = OrderService()
        order = order_service.get_order(order_id, user_id)

        request_row = (
            OrderConfirmationRequest.query.filter(
                OrderConfirmationRequest.order_id == order.id,
                OrderConfirmationRequest.status == "pending",
            )
            .order_by(OrderConfirmationRequest.id.desc())
            .first()
        )
        if request_row is None:
            raise ConflictError(
                "This order is not awaiting your confirmation",
                error_code="SALES_CONFIRMATION_NOT_PENDING",
            )
        if action not in ("confirm", "decline"):
            raise ValidationError(
                "action must be 'confirm' or 'decline'",
                error_code="SALES_CONFIRMATION_ACTION_INVALID",
            )

        outlet = request_row.outlet

        # The ORDER moves first; the row is marked answered only afterwards, in one commit with
        # the push that follows it. Marking first made the answer durable before anything acted
        # on it, so a raising update/cancel stranded the order: PENDING for ever, unanswerable
        # (409), unpushed, and invisible to the expiry sweep, which only looks at pending rows
        # (L57). `update_order_status` and `cancel_order` are `@transactional` and commit inside
        # themselves, so this is not one database transaction — it is the only ORDER of the two
        # writes in which a failure leaves a state somebody can still resolve.
        if action == "confirm":
            current_status = order.status if isinstance(order.status, OrderStatus) else OrderStatus(order.status)
            if current_status == OrderStatus.PENDING:
                order = order_service.update_order_status(
                    order.id,
                    OrderStatus.CONFIRMED,
                    updated_by=user_id,
                    notes="Confirmed by customer via Telegram",
                )
            answer, decline_reason = "confirmed", None
            event, event_reason = SALES_EVENT_AGENT_ORDER_CONFIRMED, None
        else:
            order = order_service.cancel_order(
                order.id,
                user_id=user_id,
                reason=reason or "customer_declined_agent_order",
                actor_user_id=user_id,
            )
            answer, decline_reason = "declined", reason
            event, event_reason = SALES_EVENT_AGENT_ORDER_DECLINED, reason

        request_row.status = answer
        request_row.decline_reason = decline_reason
        request_row.responded_at = get_utc_now()
        request_row.response_channel = "telegram"
        db.session.commit()
        notifications.notify_agent_order_event(order, outlet, event, reason=event_reason)
        return {"order": serialize_order(order), "confirmation": {"status": request_row.status}}

    @staticmethod
    def expire_due(*, now: Optional[datetime] = None) -> int:
        """The D5 fallback: an unanswered request expires and the order is confirmed
        anyway — the driver's delivery becomes the acceptance."""
        moment = now or get_utc_now()
        rows = (
            OrderConfirmationRequest.query.filter(
                OrderConfirmationRequest.status == "pending",
                OrderConfirmationRequest.expires_at <= moment,
            )
            .order_by(OrderConfirmationRequest.id)
            .all()
        )
        if not rows:
            return 0

        for row in rows:
            row.status = "expired"
            row.responded_at = moment
        db.session.commit()

        order_service = OrderService()
        for row in rows:
            order = Order.query.get(row.order_id)
            if order is None:
                continue
            current_status = order.status if isinstance(order.status, OrderStatus) else OrderStatus(order.status)
            if current_status != OrderStatus.PENDING:
                continue
            try:
                order_service.update_order_status(
                    order.id,
                    OrderStatus.CONFIRMED,
                    notes="Confirmed after confirmation timeout — delivery confirms",
                )
            except Exception:  # noqa: BLE001 — one stuck order must not block the sweep
                db.session.rollback()
                logger.exception("Timeout confirmation failed for order %s", order.id)
        return len(rows)
