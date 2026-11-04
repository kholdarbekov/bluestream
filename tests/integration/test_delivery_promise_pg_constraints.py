"""Postgres-only guarantees of migration 9e7056f6124a (delivery_promises, honest-ETA Phase 1, spec §4.10).

The SQLite suite builds its schema from the models; this file runs the real migration on a throwaway
database and asserts each constraint BY NAME (`_rollback_on_named_integrity`), because a probe row that
breaks two rules would otherwise pass against a schema that lacks the one under test. Postgres fires
CHECKs in name order, which is why each probe below breaks exactly one rule.
"""

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import (
    MODEL_GOOGLE_TAP,
    MODEL_OSRM_SHADOW,
    PROMISE_DAY_KINDS,
    PROMISE_STAGES,
    DeliveryPromise,
)
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.order_deletion_service import OrderDeletionService
from shared.enums import DeliveryStatus
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _index_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

TABLE = "delivery_promises"
NOW = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
START = NOW + timedelta(minutes=30)
END = NOW + timedelta(minutes=90)
MISSING = 10**8  # an id no row in this throwaway database can hold
_phones = itertools.count(900000301)


def _user(pg_db, first_name):
    user = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name=first_name)
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


@pytest.fixture
def tap(pg_db):
    """A delivery that is on its way, and the history row of the tap that put it there; both committed."""
    customer, driver = _user(pg_db, "Kamola"), _user(pg_db, "Sardor")
    order = Order(user_id=customer.id, subtotal=Decimal("15000.00"), total_amount=Decimal("15000.00"))
    pg_db.session.add(order)
    pg_db.session.commit()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=NOW,
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(delivery)
    pg_db.session.commit()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.PICKED_UP,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_by=driver.id,
    )
    pg_db.session.add(history)
    pg_db.session.commit()
    return SimpleNamespace(order_number=order.order_number, delivery_id=delivery.id, history_id=history.id)


def _promise(tap, **columns):
    values = {"delivery_id": tap.delivery_id, "stage": "on_the_way", "model_version": MODEL_GOOGLE_TAP}
    return DeliveryPromise(**{**values, **columns})


def test_each_constraint_is_enforced_by_name(pg_db, tap):
    """Every rule the migration names, provoked by a row that breaks exactly that one."""
    pg_db.session.add(_promise(tap, delivery_status_history_id=tap.history_id))
    pg_db.session.commit()

    probes = [
        ("ck_delivery_promises_stage", _promise(tap, stage="paused")),
        ("ck_delivery_promises_day_kind", _promise(tap, day_kind="someday")),
        ("ck_delivery_promises_window_order", _promise(tap, window_start=END, window_end=START)),
        ("uq_delivery_promises_history_model", _promise(tap, delivery_status_history_id=tap.history_id)),
        ("fk_delivery_promises_delivery_id", _promise(tap, delivery_id=MISSING)),
        ("fk_delivery_promises_delivery_status_history_id", _promise(tap, delivery_status_history_id=MISSING)),
    ]
    for constraint, row in probes:
        _rollback_on_named_integrity(pg_db, row, constraint)

    # Refused means refused: only the committed row is there.
    assert DeliveryPromise.query.count() == 1


@pytest.mark.parametrize(
    "name, values",
    [("ck_delivery_promises_stage", PROMISE_STAGES), ("ck_delivery_promises_day_kind", PROMISE_DAY_KINDS)],
)
def test_the_migrations_frozen_vocabulary_matches_the_model(pg_db, name, values):
    definition = _constraint_def(pg_db, TABLE, name)
    assert definition is not None, f"{name} is missing on {TABLE}"
    assert _quoted_literals(definition) == set(values)


def test_the_indexes_are_live_on_their_columns(pg_db):
    """The unique index is PARTIAL: Alembic does not compare an index's predicate, so this pins it."""
    unique = _index_def(pg_db, "uq_delivery_promises_history_model")
    assert unique is not None
    assert unique.startswith("CREATE UNIQUE INDEX uq_delivery_promises_history_model ON public.delivery_promises")
    assert "(delivery_status_history_id, model_version)" in unique
    assert unique.endswith("WHERE (delivery_status_history_id IS NOT NULL)"), unique

    assert _index_def(pg_db, "ix_delivery_promises_delivery_id_computed_at") == (
        "CREATE INDEX ix_delivery_promises_delivery_id_computed_at ON public.delivery_promises "
        "USING btree (delivery_id, computed_at)"
    )


def test_every_documented_shape_commits(pg_db, tap):
    """The positive control for the probes above, and the migration's boundaries: every stage, every day
    kind and none, a window open on either side or a single instant, one tap holding a shown row and its
    shadow, and any number of rows that belong to no tap."""
    rows = [
        *(_promise(tap, stage=stage) for stage in PROMISE_STAGES),
        *(_promise(tap, day_kind=day_kind) for day_kind in (*PROMISE_DAY_KINDS, None)),
        _promise(tap, window_start=START, window_end=START),
        _promise(tap, window_start=START),
        _promise(tap, window_end=END),
        _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_GOOGLE_TAP, shown=True),
        _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_OSRM_SHADOW),
    ]
    pg_db.session.add_all(rows)
    pg_db.session.commit()

    assert DeliveryPromise.query.count() == len(rows)
    of_the_tap = DeliveryPromise.query.filter_by(delivery_status_history_id=tap.history_id).all()
    assert sorted((row.model_version, row.shown) for row in of_the_tap) == [
        (MODEL_GOOGLE_TAP, True),
        (MODEL_OSRM_SHADOW, False),
    ]


def test_hard_deleting_an_order_takes_its_promises_with_it(pg_db, tap):
    """Both foreign keys are plain, so the order-deletion walk has to delete the promises BEFORE the
    delivery and the status row they reference. Nothing else deletes either."""
    pg_db.session.add_all(
        [
            _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_GOOGLE_TAP, shown=True),
            _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_OSRM_SHADOW),
            _promise(tap),
        ]
    )
    pg_db.session.commit()

    result = OrderDeletionService().delete_order_by_number(tap.order_number, apply_changes=True)

    assert result["applied"] is True
    assert result["deleted_rows_by_table"][TABLE] == 3
    assert DeliveryPromise.query.count() == 0


def test_the_migrated_table_matches_the_model(pg_db):
    """Nothing else compares models to migrations (the SQLite suite is built from the models).

    Scoped to the one new table, so unrelated drift elsewhere in the schema never fails this. Alembic
    does not compare CHECK constraints or an index's predicate: the tests above pin those.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name == TABLE
        return obj.table.name == TABLE

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []
