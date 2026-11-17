"""Postgres-only guarantees of migration 5d2e8a7c3b19 (price changes, courtesies, notices, order-line courtesy).

The SQLite suite builds its schema from the models; this file runs the real migration on a throwaway
database and asserts each constraint BY NAME (`_rollback_on_named_integrity`), because a probe row that
breaks two rules would otherwise pass against a schema that lacks the one under test. Each probe below
breaks exactly one rule.
"""

import importlib
import itertools
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from business_app.models.order import OrderItem
from business_app.models.price_change import (
    COURTESY_SOURCES,
    PRICE_CHANGE_NOTICE_CHANNELS,
    PRICE_CHANGE_NOTICE_KINDS,
    PRICE_CHANGE_STATUSES,
    CourtesySource,
    PriceChange,
    PriceChangeItem,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
    PriceCourtesy,
)
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User
from tests.integration.reorder_reminder_builders import at, delivered_order, make_customer
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _index_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)
LATER = datetime(2026, 12, 1, 7, 0, tzinfo=UTC)
ANNOUNCE_ON, CHANGE_ON = date(2027, 1, 4), date(2027, 2, 1)
_phones = itertools.count(79000201)
_numbers = itertools.count(1)


def _customer(pg_db, name):
    return make_customer(pg_db, first_name=name, phone=f"+998{next(_phones)}")


@pytest.fixture
def world(pg_db):
    """An admin, a customer with a delivered order carrying a 4-unit line, a product and an APPLIED change."""
    admin, customer = _customer(pg_db, "Admin"), _customer(pg_db, "Kamola")
    category = ProductCategory(name="Water", description="w", is_active=True)
    pg_db.session.add(category)
    pg_db.session.flush()
    product = Product(
        name="Water 19L", description="d", category_id=category.id, size="19L", volume=19.0, volume_unit="L",
        base_price=Decimal("18000.00"), stock_quantity=100, min_stock_level=1, max_stock_level=500, is_active=True,
    )
    pg_db.session.add(product)
    pg_db.session.commit()
    order = delivered_order(pg_db, customer, at(1))
    line = OrderItem(
        order_id=order.id, product_id=product.id, quantity=4, unit_price=Decimal("20000.00"),
        total_price=Decimal("80000.00"),
    )
    applied = PriceChange(
        announce_on=ANNOUNCE_ON, change_on=CHANGE_ON, status=PriceChangeStatus.APPLIED, applied_at=NOW,
        created_by_id=admin.id,
    )
    pg_db.session.add_all([line, applied])
    pg_db.session.commit()
    return SimpleNamespace(
        admin_id=admin.id, customer_id=customer.id, product_id=product.id, order_id=order.id, line_id=line.id,
        change_id=applied.id,
    )


def _change(world, **columns):
    values = {"announce_on": ANNOUNCE_ON, "change_on": CHANGE_ON, "created_by_id": world.admin_id}
    return PriceChange(**{**values, **columns})


def _item(world, **columns):
    values = {"price_change_id": world.change_id, "product_id": world.product_id, "new_price": Decimal("20000.00"),
              "courtesy_cap": 4}
    return PriceChangeItem(**{**values, **columns})


def _courtesy(world, **columns):
    values = {"price_change_id": world.change_id, "user_id": world.customer_id, "source": CourtesySource.AUTOMATIC,
              "granted_at": NOW, "expires_at": LATER}
    return PriceCourtesy(**{**values, **columns})


def _notice(world, **columns):
    values = {"price_change_id": world.change_id, "user_id": world.customer_id, "kind": PriceChangeNoticeKind.REMINDER,
              "queued_at": NOW}
    return PriceChangeNotice(**{**values, **columns})


def _raw_violation(pg_db, table, values, constraint):
    """A row whose enum value the ORM would refuse before the database sees it: insert it as SQL."""
    columns = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    with pytest.raises(IntegrityError) as exc_info:
        pg_db.session.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({binds})"), values)
        pg_db.session.commit()
    assert exc_info.value.orig.diag.constraint_name == constraint, str(exc_info.value)
    pg_db.session.rollback()


def _change_values(world, **columns):
    values = {"announce_on": ANNOUNCE_ON, "change_on": CHANGE_ON, "reminder_lead_days": 7, "courtesy_days": 14,
              "status": "scheduled", "created_by_id": world.admin_id, "created_at": NOW, "updated_at": NOW}
    return {**values, **columns}


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_price_changes_status_matrix", {"status": PriceChangeStatus.APPLIED}),
        ("ck_price_changes_status_matrix", {"status": PriceChangeStatus.CANCELLED}),
        ("ck_price_changes_status_matrix", {"status": PriceChangeStatus.ANNOUNCED}),
        ("ck_price_changes_status_matrix", {"announcement_started_at": NOW}),
        ("ck_price_changes_dates", {"change_on": ANNOUNCE_ON}),
        ("ck_price_changes_reminder_lead_days", {"reminder_lead_days": 0}),
        ("ck_price_changes_courtesy_days", {"courtesy_days": 0}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_check_constraints_refuse_bad_changes(pg_db, world, constraint, columns):
    _rollback_on_named_integrity(pg_db, _change(world, **columns), constraint)
    assert PriceChange.query.count() == 1


def test_the_status_vocabulary_is_checked(pg_db, world):
    _raw_violation(pg_db, "price_changes", _change_values(world, status="bogus"), "ck_price_changes_status")


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_price_change_items_new_price", {"new_price": Decimal("0.00")}),
        ("ck_price_change_items_courtesy_cap", {"courtesy_cap": 0}),
    ],
)
def test_check_constraints_refuse_bad_items(pg_db, world, constraint, columns):
    _rollback_on_named_integrity(pg_db, _item(world, **columns), constraint)
    assert PriceChangeItem.query.count() == 0


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_price_courtesies_used", {"order_id": "order"}),
        ("ck_price_courtesies_used", {"used_at": NOW}),
        ("ck_price_courtesies_manager_reason", {"source": CourtesySource.MANAGER}),
    ],
)
def test_check_constraints_refuse_bad_courtesies(pg_db, world, constraint, columns):
    columns = {k: (world.order_id if v == "order" else v) for k, v in columns.items()}
    _rollback_on_named_integrity(pg_db, _courtesy(world, **columns), constraint)
    assert PriceCourtesy.query.count() == 0


def test_the_courtesy_source_vocabulary_is_checked(pg_db, world):
    values = {"price_change_id": world.change_id, "user_id": world.customer_id, "source": "bogus",
              "granted_at": NOW, "expires_at": LATER}
    _raw_violation(pg_db, "price_courtesies", values, "ck_price_courtesies_source")


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_price_change_notices_channel", {"channel": "bogus"}),
        ("ck_price_change_notices_none_unsent", {"channel": "none", "sent_at": NOW}),
        ("ck_price_change_notices_queued_unsent", {"channel": None, "sent_at": NOW}),
    ],
)
def test_check_constraints_refuse_bad_notices(pg_db, world, constraint, columns):
    _rollback_on_named_integrity(pg_db, _notice(world, **columns), constraint)
    assert PriceChangeNotice.query.count() == 0


def test_the_notice_kind_vocabulary_is_checked(pg_db, world):
    values = {"price_change_id": world.change_id, "user_id": world.customer_id, "kind": "bogus", "queued_at": NOW}
    _raw_violation(pg_db, "price_change_notices", values, "ck_price_change_notices_kind")


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_order_items_courtesy_pair", {"courtesy_quantity": 2}),
        ("ck_order_items_courtesy_pair", {"courtesy_unit_price": Decimal("18000.00")}),
        ("ck_order_items_courtesy_quantity", {"courtesy_quantity": 5, "courtesy_unit_price": Decimal("18000.00")}),
        ("ck_order_items_courtesy_quantity", {"courtesy_quantity": 0, "courtesy_unit_price": Decimal("18000.00")}),
    ],
)
def test_check_constraints_refuse_bad_order_lines(pg_db, world, constraint, columns):
    """The line carries 4 units."""
    line = pg_db.session.get(OrderItem, world.line_id)
    for name, value in columns.items():
        setattr(line, name, value)
    with pytest.raises(IntegrityError) as exc_info:
        pg_db.session.commit()
    assert exc_info.value.orig.diag.constraint_name == constraint, str(exc_info.value)
    pg_db.session.rollback()


def test_one_pending_change_at_a_time(pg_db, world):
    pg_db.session.add(_change(world))
    pg_db.session.commit()

    _rollback_on_named_integrity(pg_db, _change(world), "uq_price_changes_one_pending")
    _rollback_on_named_integrity(
        pg_db,
        _change(world, status=PriceChangeStatus.ANNOUNCED, announcement_started_at=NOW),
        "uq_price_changes_one_pending",
    )

    # Settled changes sit beside the pending one.
    pg_db.session.add_all(
        [
            _change(world, status=PriceChangeStatus.CANCELLED, cancelled_at=NOW),
            _change(world, status=PriceChangeStatus.APPLIED, applied_at=NOW),
        ]
    )
    pg_db.session.commit()
    assert PriceChange.query.count() == 4

    definition = _index_def(pg_db, "uq_price_changes_one_pending")
    assert definition is not None
    assert definition.startswith("CREATE UNIQUE INDEX uq_price_changes_one_pending")
    assert " WHERE " in definition
    assert set(re.findall(r"'([^']*)'", definition)) == {"scheduled", "announced"}


def test_unique_keys(pg_db, world):
    pg_db.session.add_all([_item(world), _courtesy(world), _notice(world)])
    pg_db.session.commit()

    _rollback_on_named_integrity(pg_db, _item(world), "uq_price_change_items_change_product")
    _rollback_on_named_integrity(pg_db, _courtesy(world), "uq_price_courtesies_change_user")
    _rollback_on_named_integrity(pg_db, _notice(world), "uq_price_change_notices_change_user_kind")
    # Another kind for the same person is a different notice.
    pg_db.session.add(_notice(world, kind=PriceChangeNoticeKind.ANNOUNCEMENT))
    pg_db.session.commit()


def test_every_documented_state_commits(pg_db, world):
    """The positive control for the probes above: one valid row per state of each matrix."""
    people = [_customer(pg_db, f"P{n}") for n in range(4)]
    orders = [delivered_order(pg_db, person, at(2)) for person in people]

    scheduled = _change(world)
    pg_db.session.add_all([scheduled, _change(world, status=PriceChangeStatus.CANCELLED, cancelled_at=NOW,
                                              cancelled_by_id=world.admin_id)])
    pg_db.session.commit()
    seen = {row.status for row in PriceChange.query.all()}
    # The one pending row moves on to ANNOUNCED.
    scheduled.status = PriceChangeStatus.ANNOUNCED
    scheduled.announcement_started_at = NOW
    pg_db.session.commit()
    assert seen | {row.status for row in PriceChange.query.all()} == set(PriceChangeStatus)

    pg_db.session.add_all(
        [
            _courtesy(world, user_id=people[0].id, source=CourtesySource.MANAGER, reason="phoned in",
                      granted_by_id=world.admin_id),
            _courtesy(world, user_id=people[1].id, source=CourtesySource.MANAGER, reason="phoned in",
                      order_id=orders[1].id, used_at=NOW),
            _courtesy(world, user_id=people[2].id),
            _courtesy(world, user_id=people[3].id, order_id=orders[3].id, used_at=NOW),
        ]
    )
    pg_db.session.add_all(
        [_notice(world, user_id=person.id, channel=channel, sent_at=NOW if channel in ("telegram", "email") else None)
         for person, channel in zip(people, (None, "telegram", "email", "none"))]
    )
    line = pg_db.session.get(OrderItem, world.line_id)
    line.courtesy_quantity, line.courtesy_unit_price = 2, Decimal("18000.00")
    pg_db.session.commit()

    assert PriceCourtesy.query.count() == 4
    assert PriceChangeNotice.query.count() == 4
    assert {row.channel for row in PriceChangeNotice.query.all()} == {None, *PRICE_CHANGE_NOTICE_CHANNELS}


def test_restrict_and_set_null(pg_db, world):
    # RESTRICT: a person a courtesy, a notice or a change points at cannot be deleted.
    creator, courtier, notified = (_customer(pg_db, name) for name in ("Creator", "Courtier", "Notified"))
    other_change = _change(world, created_by_id=creator.id)
    pg_db.session.add_all([other_change, _courtesy(world, user_id=courtier.id), _notice(world, user_id=notified.id)])
    pg_db.session.commit()
    for user, constraint in (
        (creator, "fk_price_changes_created_by_id"),
        (courtier, "fk_price_courtesies_user_id"),
        (notified, "fk_price_change_notices_user_id"),
    ):
        pg_db.session.delete(pg_db.session.get(User, user.id))
        with pytest.raises(IntegrityError) as exc_info:
            pg_db.session.commit()
        assert exc_info.value.orig.diag.constraint_name == constraint, str(exc_info.value)
        pg_db.session.rollback()

    # SET NULL: the granter and the canceller.
    granter, canceller = _customer(pg_db, "Granter"), _customer(pg_db, "Canceller")
    cancelled = _change(world, status=PriceChangeStatus.CANCELLED, cancelled_at=NOW, cancelled_by_id=canceller.id)
    granted = _courtesy(world, user_id=world.customer_id, granted_by_id=granter.id)
    pg_db.session.add_all([cancelled, granted])
    pg_db.session.commit()
    cancelled_id, granted_id = cancelled.id, granted.id
    pg_db.session.delete(pg_db.session.get(User, granter.id))
    pg_db.session.delete(pg_db.session.get(User, canceller.id))
    pg_db.session.commit()
    pg_db.session.expire_all()
    assert pg_db.session.get(PriceChange, cancelled_id).cancelled_by_id is None
    assert pg_db.session.get(PriceCourtesy, granted_id).granted_by_id is None


def test_items_go_with_their_change(pg_db, world):
    pg_db.session.add(_item(world))
    pg_db.session.commit()
    pg_db.session.execute(text("DELETE FROM price_changes WHERE id = :id"), {"id": world.change_id})
    pg_db.session.commit()
    assert PriceChangeItem.query.count() == 0


def test_frozen_vocabulary_matches_model(pg_db):
    migration = importlib.import_module("business_app.migrations.versions.20261116_1200_add_price_changes")
    assert migration.STATUSES == PRICE_CHANGE_STATUSES
    assert migration.SOURCES == COURTESY_SOURCES
    assert migration.KINDS == PRICE_CHANGE_NOTICE_KINDS
    assert migration.CHANNELS == PRICE_CHANGE_NOTICE_CHANNELS
    # And the live database carries exactly those literals.
    for table, name, values in [
        ("price_changes", "ck_price_changes_status", PRICE_CHANGE_STATUSES),
        ("price_courtesies", "ck_price_courtesies_source", COURTESY_SOURCES),
        ("price_change_notices", "ck_price_change_notices_kind", PRICE_CHANGE_NOTICE_KINDS),
        ("price_change_notices", "ck_price_change_notices_channel", PRICE_CHANGE_NOTICE_CHANNELS),
    ]:
        definition = _constraint_def(pg_db, table, name)
        assert definition is not None, f"{name} is missing on {table}"
        assert _quoted_literals(definition) == set(values)


def test_model_matches_migration(pg_db):
    """Scoped to the four new tables and the two new order_items columns, so unrelated drift never fails this.

    Alembic does not compare CHECK constraints (the tests above pin those) and compares an expression index
    as text, so `uq_price_changes_one_pending` is pinned through `_index_def` instead.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    tables = ("price_changes", "price_change_items", "price_courtesies", "price_change_notices")

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name in (*tables, "order_items")
        if obj.table.name == "order_items":
            return type_ == "column" and name in ("courtesy_quantity", "courtesy_unit_price")
        if type_ == "index" and name == "uq_price_changes_one_pending":
            return False
        return True

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []
