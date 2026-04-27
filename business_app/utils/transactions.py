"""
Explicit transaction-boundary utilities for multi-step service flows (ARCH-008).

Multi-step service operations (create_order, process_payment, etc.) historically
relied on implicit `db.session.commit()` calls scattered through the call graph.
This made it impossible to reason about partial-failure recovery: one step could
commit before a later step failed, leaving orphaned rows and forcing reconciliation.

This module provides two primitives:

  - ``atomic_transaction()`` — context manager that wraps a block in a single
    DB transaction. Commits on clean exit, rolls back on any exception.

  - ``@transactional`` — decorator that wraps an entire function in
    ``atomic_transaction()``. Use for service methods that should be atomic
    end-to-end.

Usage:

    from business_app.utils.transactions import atomic_transaction, transactional

    # Context manager — clear scope:
    def create_order(...):
        with atomic_transaction():
            db.session.add(order)
            db.session.flush()  # get order.id without committing
            for item in items:
                db.session.add(OrderItem(order_id=order.id, ...))
        # commit happens here on clean exit; rollback on any raised exception

    # Decorator — full-method scope:
    @transactional
    def process_payment_atomically(payment_id):
        ...

Cross-system compensation (Redis, external gateways): if a step writes to a
non-DB system between begin and commit, the DB rollback won't undo it. Either:

  1. Defer the non-DB write until AFTER the DB commit (preferred).
  2. Inside the ``except`` handler, explicitly call the compensation function
     for that non-DB system (e.g. ``inventory_service.release_reservations``).
  3. If neither is feasible, document a Celery beat task that reconciles the
     drift over time (e.g. ``reconcile_pending_payments`` for PAY-007).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

from business_app import db

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def atomic_transaction() -> Iterator[None]:
    """Run a block as a single DB transaction.

    Commits on clean exit, rolls back on any raised exception. Re-raises the
    exception so callers can react / compensate non-DB side effects.

    Nested calls: this opens a top-level transaction. If you need nested
    rollback semantics within an already-open transaction (rare), use
    ``db.session.begin_nested()`` directly for SAVEPOINT support.
    """
    try:
        yield
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "Transaction rolled back due to %s: %s",
            exc.__class__.__name__,
            exc,
        )
        raise


def transactional(func: F) -> F:
    """Decorator: wrap a function in :func:`atomic_transaction`.

    The wrapped function runs inside a single DB transaction. Any exception
    triggers rollback before propagating. Callers shouldn't call
    ``db.session.commit()`` from inside — let the decorator own commit timing.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with atomic_transaction():
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
