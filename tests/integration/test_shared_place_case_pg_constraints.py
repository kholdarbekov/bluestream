"""Postgres-only guarantees of migration e3b7c1d9a5f4 (shared_place_cases, address_groups.notes).

The SQLite suite builds its schema from the models; this file runs the real migration on a throwaway
database and asserts each constraint BY NAME (`_rollback_on_named_integrity`), because a probe row that
breaks two rules would otherwise pass against a schema that lacks the one under test. Each probe below
breaks exactly one rule.
"""

import importlib
import itertools
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.customer_link import AddressGroup
from business_app.models.shared_place_case import (
    SHARED_PLACE_CASE_STATUSES,
    SHARED_PLACE_DRIVER_ANSWERS,
    SHARED_PLACE_EMPTY_REASONS,
    SHARED_PLACE_RESOLUTION_KINDS,
    SharedPlaceCase,
)
from business_app.models.user import User, UserAddress
from tests.integration.reorder_reminder_builders import delivered_order, make_customer, at
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _index_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

TABLE = "shared_place_cases"
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)
_phones = itertools.count(79000101)


def _customer(pg_db, name):
    return make_customer(pg_db, first_name=name, phone=f"+998{next(_phones)}")


@pytest.fixture
def world(pg_db):
    """A customer with a delivered order at a home address, and a driver; all committed."""
    customer, driver = _customer(pg_db, "Kamola"), _customer(pg_db, "Sardor")
    order = delivered_order(pg_db, customer, at(1))
    return SimpleNamespace(
        order_id=order.id, address_id=order.delivery_address_id, driver_id=driver.id, customer_id=customer.id
    )


def _case(world, **columns):
    values = {
        "order_id": world.order_id,
        "address_id": world.address_id,
        "returned_qty": 2,
        "place_balance_before": Decimal("0.00"),
        "candidates": [],
        "status": "awaiting_driver",
    }
    return SharedPlaceCase(**{**values, **columns})


def _other_order(pg_db, world):
    return delivered_order(pg_db, pg_db.session.get(User, world.customer_id), at(2))


@pytest.mark.parametrize(
    "constraint, columns",
    [
        ("ck_shared_place_cases_status", {"status": "bogus"}),
        ("ck_shared_place_cases_driver_answer", {"driver_answer": "maybe", "answered_at": NOW}),
        ("ck_shared_place_cases_resolution_kind", {"resolution_kind": "bogus"}),
        ("ck_shared_place_cases_empty_reason", {"empty_reason": "bogus"}),
        ("ck_shared_place_cases_answered", {"driver_answer": "none"}),
        ("ck_shared_place_cases_picked", {"driver_answer": "picked", "answered_at": NOW}),
        ("ck_shared_place_cases_decided", {"status": "resolved", "resolution_kind": "same_home"}),
        ("ck_shared_place_cases_resolved_kind", {"status": "resolved", "decided_at": NOW}),
        ("ck_shared_place_cases_closed_kind", {"status": "closed", "decided_at": NOW, "resolution_kind": "same_home"}),
        ("ck_shared_place_cases_returned_qty", {"returned_qty": 0}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_check_constraints_refuse_bad_states(pg_db, world, constraint, columns):
    _rollback_on_named_integrity(pg_db, _case(world, **columns), constraint)
    assert SharedPlaceCase.query.count() == 0


def test_every_documented_state_commits(pg_db, world):
    """The positive control for the probes above: one valid row per state of the matrix."""
    other_orders = [_other_order(pg_db, world) for _ in range(4)]
    rows = [
        _case(world),
        _case(
            SimpleNamespace(**{**vars(world), "order_id": other_orders[0].id}),
            status="awaiting_admin",
            driver_answer="picked",
            picked_index=0,
            answered_at=NOW,
            driver_note="their cousin",
            empty_reason=None,
        ),
        _case(
            SimpleNamespace(**{**vars(world), "order_id": other_orders[1].id}),
            status="resolved",
            resolution_kind="shared_place",
            resolution_payload={"kind": "shared_place"},
            decided_at=NOW,
            empty_reason="no_pin",
        ),
        # A close keeps its kind NULL but stores the payload (the replay compares against it).
        _case(
            SimpleNamespace(**{**vars(world), "order_id": other_orders[2].id}),
            status="closed",
            resolution_payload={"kind": "close"},
            decided_at=NOW,
        ),
    ]
    pg_db.session.add_all(rows)
    pg_db.session.commit()
    assert SharedPlaceCase.query.count() == 4


def test_one_case_per_order(pg_db, world):
    pg_db.session.add(_case(world))
    pg_db.session.commit()
    _rollback_on_named_integrity(pg_db, _case(world), "uq_shared_place_cases_order_id")
    assert SharedPlaceCase.query.count() == 1


def test_deleting_the_driver_or_the_decider_nulls_the_reference(pg_db, world):
    admin = _customer(pg_db, "Admin")
    case = _case(world, asked_driver_id=world.driver_id, decided_by_user_id=admin.id, status="closed", decided_at=NOW)
    pg_db.session.add(case)
    pg_db.session.commit()
    case_id = case.id

    pg_db.session.delete(pg_db.session.get(User, world.driver_id))
    pg_db.session.delete(admin)
    pg_db.session.commit()
    pg_db.session.expire_all()

    case = pg_db.session.get(SharedPlaceCase, case_id)
    assert (case.asked_driver_id, case.decided_by_user_id) == (None, None)


def test_deleting_the_resulting_group_nulls_the_reference(pg_db, world):
    group = AddressGroup(label="office")
    pg_db.session.add(group)
    pg_db.session.commit()
    case = _case(world, resulting_group_id=group.id)
    pg_db.session.add(case)
    pg_db.session.commit()

    pg_db.session.delete(group)
    pg_db.session.commit()
    pg_db.session.expire_all()

    assert pg_db.session.get(SharedPlaceCase, case.id).resulting_group_id is None


def test_the_order_and_address_foreign_keys_are_plain(pg_db, world):
    pg_db.session.add(_case(world))
    pg_db.session.commit()
    with pytest.raises(IntegrityError):
        pg_db.session.delete(pg_db.session.get(UserAddress, world.address_id))
        pg_db.session.commit()
    pg_db.session.rollback()


def test_frozen_vocabulary_matches_model(pg_db):
    migration = importlib.import_module("business_app.migrations.versions.20261111_1200_add_shared_place_cases")
    assert migration.STATUSES == SHARED_PLACE_CASE_STATUSES
    assert migration.DRIVER_ANSWERS == SHARED_PLACE_DRIVER_ANSWERS
    assert migration.RESOLUTION_KINDS == SHARED_PLACE_RESOLUTION_KINDS
    assert migration.EMPTY_REASONS == SHARED_PLACE_EMPTY_REASONS
    # And the live database carries exactly those literals.
    for name, values in [
        ("ck_shared_place_cases_status", SHARED_PLACE_CASE_STATUSES),
        ("ck_shared_place_cases_driver_answer", SHARED_PLACE_DRIVER_ANSWERS),
        ("ck_shared_place_cases_resolution_kind", SHARED_PLACE_RESOLUTION_KINDS),
        ("ck_shared_place_cases_empty_reason", SHARED_PLACE_EMPTY_REASONS),
    ]:
        definition = _constraint_def(pg_db, TABLE, name)
        assert definition is not None, f"{name} is missing on {TABLE}"
        assert _quoted_literals(definition) == set(values)


def test_the_indexes_are_live_on_their_columns(pg_db):
    assert "(status, created_at)" in _index_def(pg_db, "ix_shared_place_cases_status_created_at")
    assert "(delivery_id)" in _index_def(pg_db, "ix_shared_place_cases_delivery_id")
    assert "(asked_driver_id)" in _index_def(pg_db, "ix_shared_place_cases_asked_driver_id")


def test_model_matches_migration(pg_db):
    """Nothing else compares models to migrations (the SQLite suite is built from the models).

    Scoped to the new table and the one new column, so unrelated drift elsewhere never fails this.
    Alembic does not compare CHECK constraints: the tests above pin those.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name in (TABLE, "address_groups")
        if obj.table.name == TABLE:
            return True
        return type_ == "column" and name == "notes"

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []
