"""Requests for a recompute of the capacity-aware ETA plan (honest-ETA Phase 2 plan, R15).

A commit that touches the plan's inputs (`PLAN_INPUTS`) requests one recompute. `request_recompute` is how a recompute
is asked for, and `_publish_recompute` is the ONE place one is published, used by every request, the locked run's
included (`EtaEngine.recompute`).

- The detector. One `before_flush` listener reads the attribute history of what the flush writes, and flags the
  session's transaction when it inserts, deletes or changes a plan input. It loads no relationship and runs no SQL.
  With the kill switch ETA_SHADOW_ENGINE_ENABLED off it returns at once, and once the transaction is flagged it looks
  no further. A failure inside it is logged as a warning, never raised into the write: that flush flags nothing, and
  the beat recomputes. Never watched: `delivery_promises` and `eta_model_fits` (the engine's and the weekly fit's
  own writes), a driver's live position, and `DeliveryRoute.extra_data`.
- The drain. One `after_commit` listener requests one recompute per committed transaction that was flagged, and a
  rollback drops the flag. A SAVEPOINT release or rollback is not the transaction: both listeners ignore it, so a flag
  is drained by the outer commit and dropped only by the outer rollback (as in `payment_confirmation_dispatch`). It
  runs no SQL, since it runs inside the commit's after-commit hook, and a request it makes that fails is logged, never
  raised into the committed write. Modelled on `route_card_refresh_dispatch`.
- Trailing-edge debounce. A request sets the "recompute queued" marker (RedisKeyspace.eta_recompute_scheduled, CACHE
  tier, `SET NX EX` ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS) and publishes only when it set it, so a burst of requests
  queues one run. The run clears the marker as it starts, so a request made during a run queues the next one. A
  publish that fails clears the marker again. Without Redis a request publishes anyway: losing the marker costs an
  extra recompute, never a missed one.
- Each run is published `countdown` seconds out and expires ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS after that, so a
  queued run outlives its countdown and a stale backlog is dropped. A request's countdown is
  ETA_RECOMPUTE_COUNTDOWN_SECONDS unless the caller names one: a run that found the lock held names the lock's
  remaining TTL.
- Eager mode. When the Celery app current at the request runs tasks eagerly, a publish would run the recompute inline,
  inside the caller, even inside an after-commit hook. Such a request publishes nothing and logs; the beat (every ten
  minutes) is the backstop. That app is the business app in a dev worker or beat, or in a web process that has
  imported `business_app.tasks.celery_app`; in a web request it is otherwise Celery's default app, which is not
  eager and publishes to the queue the workers consume. This module never imports `business_app.tasks.celery_app`,
  which would build the business app, and never reads the Flask `CELERY` dict, which says nothing about the app that
  would publish.
"""

import functools
import logging
from typing import Dict, NamedTuple, Optional, Tuple

from celery import current_app as current_celery_app
from flask import current_app
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import InstrumentedAttribute, RelationshipDirection

from business_app import redis_client
from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer, DriverSessionMembership
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order, OrderItem
from business_app.models.user import User, UserAddress
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

# The session.info key of a transaction the detector flagged: it touched a plan input.
PENDING_KEY = "pending_eta_recompute"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


class Watched(NamedTuple):
    """What of one model's rows the plan reads: a change to any of `columns`, and a row inserted or deleted."""

    columns: Tuple[InstrumentedAttribute, ...] = ()
    insert: bool = False
    delete: bool = False


# The plan's inputs (R15): a flush that inserts, deletes or changes any of these requests a recompute once it commits.
PLAN_INPUTS: Dict[type, Watched] = {
    Delivery: Watched(
        (Delivery.status, Delivery.delivery_person_id, Delivery.scheduled_date),
        insert=True,
        delete=True,
    ),
    Order: Watched(
        (
            Order.status,
            Order.delivery_address_id,
            Order.delivery_date,
            Order.delivery_window_start,
            Order.delivery_window_end,
        ),
        insert=True,
    ),
    # `order_id` as well: a line the admin order edit removes leaves its order's collection, and delete-orphan deletes
    # the row only inside the flush, after the detector has looked. Until then it is an item whose order changed.
    OrderItem: Watched((OrderItem.quantity, OrderItem.order_id), insert=True, delete=True),
    UserAddress: Watched((UserAddress.latitude, UserAddress.longitude)),
    DeliveryStatusHistory: Watched(insert=True),
    DriverBottleSession: Watched(
        (
            DriverBottleSession.status,
            # Every bottles_* counter.
            DriverBottleSession.bottles_loaded,
            DriverBottleSession.bottles_delivered,
            DriverBottleSession.bottles_collected_from_customers,
            DriverBottleSession.bottles_transferred_out,
            DriverBottleSession.bottles_transferred_in,
            DriverBottleSession.bottles_returned_to_warehouse,
        ),
        insert=True,
    ),
    DriverSessionMembership: Watched((DriverSessionMembership.status,), insert=True),
    DriverBottleTransfer: Watched((DriverBottleTransfer.status,), insert=True),
    DeliveryPerson: Watched(
        (
            DeliveryPerson.is_active,
            DeliveryPerson.is_available,
            DeliveryPerson.working_hours_start,
            DeliveryPerson.working_hours_end,
        ),
        insert=True,
    ),
    User: Watched((User.status,)),
    DeliveryRoute: Watched((DeliveryRoute.optimized_order,), insert=True),
}


def _touches_the_plan(session) -> bool:
    """True when what `session` is about to flush inserts, deletes or changes a plan input (`PLAN_INPUTS`). Attribute
    history only, of the watched columns and of the plain many-to-one relationships `_moved_by` adds, read through
    `AttributeState.history` (`PASSIVE_NO_INITIALIZE`). For these that loads nothing, so no SQL runs; the trigger
    tests count the statements. A column set to the value it had is no change."""
    for instance in session.new:
        watched = PLAN_INPUTS.get(type(instance))
        if watched is not None and watched.insert:
            return True
    for instance in session.deleted:
        watched = PLAN_INPUTS.get(type(instance))
        if watched is not None and watched.delete:
            return True
    for instance in session.dirty:
        watched = PLAN_INPUTS.get(type(instance))
        if watched is not None and watched.columns:
            attrs = sa_inspect(instance).attrs
            if any(attrs[key].history.has_changes() for key in _moved_by(type(instance))):
                return True
    return False


@functools.cache
def _moved_by(model: type) -> Tuple[str, ...]:
    """The attributes of `model` whose change moves one of its watched columns: each column, and each many-to-one
    relationship that writes one. An FK set through its relationship (`order.delivery_address = new`, the admin
    address change; `order.order_items.remove(line)`, which unsets `line.order`, the admin order edit) moves only when
    the flush syncs it, after `before_flush`, so the column's own history does not show it yet.

    Only a relationship joined on its target's primary key counts (`_joins_on_the_primary_key`). The history of such
    a relationship is read without a load today; one with a custom join keeps its history deferred, and reading it
    could load inside `before_flush`, so it is left out. Read from the mapper once per model, at the first flush that
    needs it, when the mappers are configured."""
    columns = PLAN_INPUTS[model].columns
    watched = {column for attribute in columns for column in attribute.property.columns}
    writers = tuple(
        relationship.key
        for relationship in sa_inspect(model).relationships
        if relationship.direction is RelationshipDirection.MANYTOONE
        and not relationship.viewonly
        and relationship.local_columns & watched
        and _joins_on_the_primary_key(relationship)
    )
    return tuple(attribute.key for attribute in columns) + writers


def _joins_on_the_primary_key(relationship) -> bool:
    """True when the many-to-one's join pairs its local columns with exactly its target's primary key: the plain FK
    to a row, as `Order.delivery_address`, `Delivery.delivery_person` and `OrderItem.order` are."""
    remote = {remote_column for _local, remote_column in relationship.local_remote_pairs}
    return remote == set(relationship.mapper.primary_key)


def request_recompute(*, countdown: Optional[int] = None) -> bool:
    """Queue a recompute `countdown` seconds from now (ETA_RECOMPUTE_COUNTDOWN_SECONDS when None). True when this
    request published it; False when a recompute is already queued, when Celery runs tasks eagerly here, or when the
    publish failed. A Redis or broker failure is logged, never raised."""
    if current_celery_app.conf.task_always_eager:
        logger.info("eta_recompute_request_skipped: Celery runs tasks eagerly here, so the beat recomputes instead")
        return False
    config = current_app.config
    if countdown is None:
        countdown = config["ETA_RECOMPUTE_COUNTDOWN_SECONDS"]
    marker = RedisKeyspace.eta_recompute_scheduled()
    try:
        marked = bool(redis_client.set(marker, "1", nx=True, ex=config["ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS"]))
    except Exception:  # noqa: BLE001 - a cache: without the marker the request still publishes
        logger.warning("eta_recompute: the queued marker is unavailable; publishing without it", exc_info=True)
        marked = None
    if marked is False:
        return False  # a recompute is already queued, and it reads everything this request would
    try:
        _publish_recompute(countdown)
    except Exception:  # noqa: BLE001 - the caller's work is done; the beat recomputes within ten minutes
        logger.exception("eta_recompute: the publish failed (countdown=%s)", countdown)
        if marked:
            _clear_marker(marker)
        return False
    return True


def _publish_recompute(countdown: int) -> None:
    """THE publish of a recompute: `countdown` seconds out, expiring ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS after that."""
    from business_app.tasks.delivery_eta_tasks import recompute_eta_plan

    expires = countdown + current_app.config["ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS"]
    recompute_eta_plan.apply_async(countdown=countdown, expires=expires)


def _clear_marker(marker: str) -> None:
    """A run that was never queued must not hold back the next request."""
    try:
        redis_client.delete(marker)
    except Exception:  # noqa: BLE001 - the marker expires by itself
        logger.warning("eta_recompute: could not clear the queued marker", exc_info=True)


def register_eta_recompute_dispatch(db) -> None:
    """Attach the detector (before_flush) and the drain (after_commit / after_rollback) to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "before_flush")
    def _detect_plan_inputs(session, flush_context, instances):  # noqa: ANN001
        try:
            if not current_app.config["ETA_SHADOW_ENGINE_ENABLED"] or session.info.get(PENDING_KEY):
                return
            if _touches_the_plan(session):
                session.info[PENDING_KEY] = True
        except Exception:  # noqa: BLE001 - never fail the write; the beat recomputes within ten minutes
            logger.warning("eta_recompute: the commit detector failed; this flush flags no recompute", exc_info=True)

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT release: the transaction has not committed yet
        if not session.info.pop(PENDING_KEY, None):
            return
        try:
            request_recompute()
        except Exception:  # best-effort — never break the committed write
            logger.exception("eta_recompute: the request after a commit failed")

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT rollback: the outer transaction may still commit what was flagged before it
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
