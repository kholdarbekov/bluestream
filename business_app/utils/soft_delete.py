"""
Global soft-delete query filter (ARCH-001).

``SoftDeleteMixin`` has existed on :mod:`business_app.models.base` but the
``deleted_at IS NULL`` predicate was never enforced anywhere — any future model
adopting the mixin would silently leak soft-deleted rows into lists, details,
exports, and analytics. This module installs a SQLAlchemy ``do_orm_execute``
listener that appends the predicate for every ORM SELECT touching a class that
inherits ``SoftDeleteMixin``.

Escape hatches (for admin views, audit screens, restore flows):

- ``Model.query.execution_options(include_deleted=True).all()``
- ``session.execute(stmt.execution_options(include_deleted=True))``
- Within a block, ``with include_deleted_scope():`` (Flask-g based).

Design notes:
- ``with_loader_criteria`` with ``include_aliases=True`` makes the predicate
  survive joins, aliased selects, and relationship loads.
- ``is_column_load``/``is_relationship_load`` are preserved so existing eager
  loads are not accidentally filtered twice.
- Availability wins over strict correctness: if the listener raises we fall
  through rather than crashing every query in the app.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from business_app.models.base import SoftDeleteMixin


_INCLUDE_DELETED_OPTION = "include_deleted"


def install_soft_delete_filter(session_cls: type = Session) -> None:
    """Attach the global soft-delete filter to the given Session class.

    Call once during app startup (after models are imported). Safe to call more
    than once: the listener is ONE module-level function, so a repeat call is a
    no-op. It used to be a closure defined inside this function — a new object
    per call, which SQLAlchemy's (target, identifier, fn) dedupe cannot match —
    so every create_app() stacked another copy onto the global Session class and
    each copy rewrote every SELECT. Tests build an app per pg-backed test, which
    left hundreds of copies per xdist worker and slowed the suite to hours.
    """
    if not event.contains(session_cls, "do_orm_execute", _apply_soft_delete_filter):
        event.listen(session_cls, "do_orm_execute", _apply_soft_delete_filter)


def _apply_soft_delete_filter(execute_state) -> None:  # pragma: no cover - exercised via ORM
    try:
        if not execute_state.is_select:
            return
        if execute_state.is_column_load or execute_state.is_relationship_load:
            return
        if execute_state.execution_options.get(_INCLUDE_DELETED_OPTION, False):
            return
        if _flask_scope_wants_deleted():
            return

        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                SoftDeleteMixin,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
            )
        )
    except Exception:
        # Never take the app down over a query-rewrite glitch.
        return


def _flask_scope_wants_deleted() -> bool:
    """Check the Flask ``g`` scope for an opt-in flag."""
    try:
        from flask import g, has_request_context, has_app_context

        if not (has_request_context() or has_app_context()):
            return False
        return bool(getattr(g, "_include_deleted", False))
    except Exception:
        return False


@contextmanager
def include_deleted_scope() -> Iterator[None]:
    """Temporarily disable the soft-delete filter for the current request.

    Use sparingly — only admin/audit/restore flows should see soft-deleted rows.
    """
    from flask import g

    previous = getattr(g, "_include_deleted", False)
    g._include_deleted = True
    try:
        yield
    finally:
        g._include_deleted = previous


__all__ = [
    "install_soft_delete_filter",
    "include_deleted_scope",
]
