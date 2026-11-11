"""Opening a shared-place case when a delivery is completed (spec 2026-11-10 §4.1; §9 binds).

One rule, read from the ledger the delivery has just written, never from the request:

1. the order brought back at least one empty (its RETURN_ON_DELIVERY row);
2. it is the account's first DELIVERED order (S1; per account, so an account only linked to an older one is still
   asked: linking never moves bottles, which belong to places);
3. the customer is an eligible individual customer (`CustomerLinkService.is_eligible_individual_customer`);
4. an admin had not already handled the address (S7): it was not in a place group, and its place balance just before
   this delivery was not positive (a starting count, an adjustment or a hand-set figure).

The one caller is the DELIVERED branch of `OrderService._handle_status_change_actions`, right after the bottles are
booked and before anything commits. It sees both paths that can carry returns: the driver's door flow (which passes
the acting driver as `asked_driver_id`) and the admin Orders "Bottles Returned" dialog (None: the case goes straight
to admin review).

A case must never fail, roll back or alter the delivery. So all of this method's database work runs inside one
SAVEPOINT: on Postgres a failing statement outside one would abort the delivery's own transaction. It takes no row
lock, writes no bottles, calls nothing that commits, and never raises.
"""

import logging
from typing import NamedTuple, Optional

from sqlalchemy.exc import IntegrityError

from flask import current_app

from business_app import db
from business_app.models.order import Order
from business_app.models.shared_place_case import SharedPlaceCase
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.order_service import OrderService
from business_app.services.shared_place.candidates import CandidateResult, find_candidates
from business_app.utils.shared_place_case_dispatch import queue_case_alert

logger = logging.getLogger(__name__)


class _Opened(NamedTuple):
    """What the savepoint answered: the case, its candidates when this call built it, and whether it is new."""

    case: SharedPlaceCase
    found: Optional[CandidateResult]
    created: bool


class SharedPlaceDetection:
    """The detection rule and the case insert, as one never-raising call."""

    @staticmethod
    def open_for_delivery(order: Order, *, asked_driver_id: Optional[int]) -> Optional[SharedPlaceCase]:
        """Open the order's case if the rule holds; return it (an existing one as it is), else None.

        Idempotent on `order_id`: `_claim_status_transition` already runs the DELIVERED branch once per transition,
        and the unique order_id plus the read before the insert are the backstop. A lost insert race re-reads the
        winner's row in a second short savepoint. Any other failure is logged and answered with None.
        """
        order_id = order.id  # read before any savepoint can expire it
        try:
            with db.session.begin_nested():
                opened = SharedPlaceDetection._open_in_savepoint(order, asked_driver_id)
        except IntegrityError:
            existing = SharedPlaceDetection._case_for_order(order_id)
            if existing is None:
                logger.error("[SHARED_PLACE] case_open_failed order=%s", order_id, exc_info=True)
            return existing
        except Exception:  # noqa: BLE001 - a case failure must never reach the delivery
            logger.error("[SHARED_PLACE] case_open_failed order=%s", order_id, exc_info=True)
            return None

        if opened is None:
            return None
        case, found = opened.case, opened.found
        if opened.created:
            # Parked only now, after the savepoint released cleanly: a failed open parks nothing, and an existing
            # case was alerted when it was created. Published by the real commit (nested-safe dispatch).
            try:
                window = current_app.config["SHARED_PLACE_DRIVER_ANSWER_WINDOW_S"]
                queue_case_alert(case.id, countdown=window if asked_driver_id else 0)
            except Exception:  # noqa: BLE001 - the alert is best-effort; the case is saved and listed for admins
                logger.error("[SHARED_PLACE] alert_queue_failed case=%s", case.id, exc_info=True)
        if found is not None:
            logger.info(
                "[SHARED_PLACE] case_opened case=%s order=%s address=%s before=%s returned=%s candidates=[%s] "
                "empty_reason=%s",
                case.id,
                order_id,
                case.address_id,
                case.place_balance_before,
                case.returned_qty,
                ",".join(f"{c.ref_kind}:{c.ref_id}" for c in found.candidates),
                found.empty_reason,
            )
        return case

    @staticmethod
    def _open_in_savepoint(order: Order, asked_driver_id: Optional[int]) -> Optional[_Opened]:
        """The rule, the existing-case read, the candidates and the insert. Returns None when the rule fails, an
        `_Opened` with `created=False` when the order already has a case, else one with `created=True`."""
        summary = BottleTrackingService.get_order_bottle_summary(order)
        returned = int(summary["bottles_collected"])  # the RETURN row stores -N; the summary gives abs
        if returned < 1:
            return None
        if OrderService.customer_has_delivered_order(order.user_id, exclude_order_id=order.id):
            return None
        if not CustomerLinkService.is_eligible_individual_customer(order.user):
            return None
        if summary["delivery_place_group_id"] is not None:
            return None  # S7: grouped before the delivery
        before = summary["place_balance_before_delivery"]
        if before is None or before > 0:
            return None  # S7: a positive balance was already entered

        existing = SharedPlaceDetection._existing_case(order.id)
        if existing is not None:
            return _Opened(existing, None, False)

        found: CandidateResult = find_candidates(order.delivery_address)
        case = SharedPlaceCase(
            order_id=order.id,
            delivery_id=order.delivery.id if order.delivery else None,
            address_id=order.delivery_address_id,
            returned_qty=returned,
            place_balance_before=before,
            asked_driver_id=asked_driver_id,
            candidates=[candidate.to_json() for candidate in found.candidates],
            empty_reason=found.empty_reason,
            status="awaiting_driver" if asked_driver_id is not None else "awaiting_admin",
        )
        db.session.add(case)
        db.session.flush()
        return _Opened(case, found, True)

    @staticmethod
    def _existing_case(order_id: int) -> Optional[SharedPlaceCase]:
        return SharedPlaceCase.query.filter_by(order_id=order_id).first()

    @staticmethod
    def _case_for_order(order_id: int) -> Optional[SharedPlaceCase]:
        """The existing case, read in its own short savepoint after the insert's savepoint rolled back."""
        try:
            with db.session.begin_nested():
                return SharedPlaceDetection._existing_case(order_id)
        except Exception:  # noqa: BLE001 - logged here; the caller answers None
            logger.error("[SHARED_PLACE] case_reread_failed order=%s", order_id, exc_info=True)
            return None
