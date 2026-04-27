"""Tests for the global SoftDeleteMixin query filter (ARCH-001)."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String

from business_app import db
from business_app.models.base import SoftDeleteMixin, TimestampMixin
from business_app.utils.soft_delete import include_deleted_scope


class _SDWidget(db.Model, TimestampMixin, SoftDeleteMixin):
    """Throwaway soft-deletable model for exercising the global filter."""

    __tablename__ = "_sd_widget_test"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)


@pytest.fixture()
def sd_widgets(app, db):
    """Create three widgets, mark one as soft-deleted."""
    with app.app_context():
        db.create_all()
        alive1 = _SDWidget(name="alive-1")
        alive2 = _SDWidget(name="alive-2")
        dead = _SDWidget(name="dead")
        db.session.add_all([alive1, alive2, dead])
        db.session.flush()
        dead.soft_delete()
        db.session.commit()
        yield [alive1.id, alive2.id, dead.id]


def test_default_query_excludes_soft_deleted(app, sd_widgets):
    with app.app_context():
        names = sorted(w.name for w in _SDWidget.query.all())
        assert names == ["alive-1", "alive-2"]


def test_get_also_excludes_soft_deleted(app, sd_widgets):
    _, _, dead_id = sd_widgets
    with app.app_context():
        assert _SDWidget.query.get(dead_id) is None


def test_execution_option_include_deleted_returns_all(app, sd_widgets):
    with app.app_context():
        names = sorted(
            w.name
            for w in _SDWidget.query.execution_options(include_deleted=True).all()
        )
        assert names == ["alive-1", "alive-2", "dead"]


def test_include_deleted_scope_disables_filter_in_block(app, sd_widgets):
    with app.test_request_context("/"):
        # Default still hides soft-deleted
        assert _SDWidget.query.count() == 2

        with include_deleted_scope():
            assert _SDWidget.query.count() == 3

        # Scope resets after the block
        assert _SDWidget.query.count() == 2


def test_filter_survives_after_restore(app, sd_widgets):
    _, _, dead_id = sd_widgets
    with app.app_context():
        restored = _SDWidget.query.execution_options(include_deleted=True).get(dead_id)
        restored.restore()
        db.session.commit()

        names = sorted(w.name for w in _SDWidget.query.all())
        assert names == ["alive-1", "alive-2", "dead"]
