"""The stop sets and rows every stage of the plan shares: which deliveries a driver holds, which of those are on the
road, which are committed to their door, and which promises are the engine's own.

The engine's supply, its demand and its recorder (honest-ETA Phase 2 plan, R1, R6, R7, R12) each ask "is this stop in
flight?", "does a driver hold it?" or "is it committed?". They ask here, so the three cannot draw the line in three
places. The recorder, the calibration and the report read the engine's rows through `engine_rows_filter`.

May import models, `state_validators`, `StaffService` and `route_optimization_service`. Neither `delivery_eta_service`
nor `common` may import this module: it pulls in `StaffService`, and `notification_service` imports
`delivery_eta_service` at module level.
"""

from typing import Optional

from sqlalchemy import and_

from business_app.models.delivery import Delivery
from business_app.models.delivery_promise import MODEL_ENGINE_S1, DeliveryPromise
from business_app.models.order import Order
from business_app.services.route_optimization_service import COMMITTED_STATUSES
from business_app.services.staff_service import StaffService
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES, DELIVERY_WITH_DRIVER_STATES
from shared.constants import ETA_STAGE_WAITING
from shared.enums import DeliveryStatus, OrderStatus

# A driver is on the road with the stop (R6): picked up, in transit or arrived, the set a customer's cancel calls "with
# the driver". Sorted by value only so an SQL `IN` list is stable. Supply subtracts these stops' bottles once, and the
# planner never subtracts them again or reloads for them.
IN_FLIGHT_DELIVERY_STATUSES = tuple(sorted(DELIVERY_WITH_DRIVER_STATES, key=lambda status: status.value))

# The orders a driver can hold a stop on (R7): the live ones, less the unconfirmed. A pickup moves the order to
# OUT_FOR_DELIVERY (`DELIVERY_TO_ORDER_STATUS_SYNC`), so a confirmed/preparing-only set would drop every in-flight stop.
DRIVER_HELD_ORDER_STATUSES = ACTIVE_ORDER_STATUSES - {OrderStatus.PENDING}


def driver_held_rows_filter():
    """The deliveries a driver holds, as a bare SQL clause (R7): a driver set, the delivery in
    `StaffService.ACTIVE_DELIVERY_STATUSES` (assigned to arrived), and its order in `DRIVER_HELD_ORDER_STATUSES`.

    The caller must already have `orders` AND `deliveries` in FROM through an explicit join, as for
    `OrderScheduleService.awaiting_new_date_filter`. A FAILED delivery keeps its driver but is no longer held, and a
    RESCHEDULED or pool row has none to hold it.
    """
    return and_(
        Delivery.delivery_person_id.isnot(None),
        Delivery.status.in_(StaffService.ACTIVE_DELIVERY_STATUSES),
        Order.status.in_(DRIVER_HELD_ORDER_STATUSES),
    )


def is_driver_held(status: Optional[DeliveryStatus], driver_id: Optional[int]) -> bool:
    """`driver_held_rows_filter()` for a delivery already read, whose order demand has gated: a driver set, and the
    delivery assigned to arrived. The planner asks it of a `Stop` (its `status` and `driver_id`)."""
    return driver_id is not None and status in StaffService.ACTIVE_DELIVERY_STATUSES


def is_committed(status: Optional[DeliveryStatus]) -> bool:
    """The stop is under way to its door: in_transit or arrived (`route_optimization_service.COMMITTED_STATUSES`). S2
    promises it from the "on the way" tap (spec §4.8), so the recorder writes it no waiting row (R12)."""
    return status in COMMITTED_STATUSES


def engine_rows_filter():
    """The engine's own promises, as a bare SQL clause over `delivery_promises`: model `engine_s1_v1`, stage `waiting`
    (R12). The recorder's latest rows, the report's first rows and change messages, and the fit's window buckets read
    these rows and no other."""
    return and_(DeliveryPromise.model_version == MODEL_ENGINE_S1, DeliveryPromise.stage == ETA_STAGE_WAITING)
