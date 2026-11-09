"""Postgres-only guarantees of migration `add_eta_model_fits_and_promised_date` (honest-ETA Phase 2, Task 4, R11, R13).

The SQLite suite builds its schema from the models; this file runs the real migration on a throwaway database. It pins
what the weekly fit's one table and the promised-day column are in production: the columns and their types, the unique
data window, the index the engine reads the latest fit through, that a promise written the Phase 1 way still inserts,
and that the downgrade takes away exactly what the upgrade added. Each constraint is asserted BY NAME, as in
test_delivery_promise_pg_constraints.py.
"""

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from business_app.models.delivery import Delivery
from business_app.models.delivery_promise import MODEL_ENGINE_S1, MODEL_GOOGLE_TAP, DeliveryPromise
from business_app.models.eta_model_fit import EtaModelFit
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.eta_plan.params import current_params
from business_app.services.eta_plan.windows import lo_hi
from shared.enums import DeliveryStatus
from tests.integration.conftest import transient_pg_database
from tests.integration.test_migrations_roundtrip import _migration_app, _revision, _schema_columns
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _index_def,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

FITS = "eta_model_fits"
# 05:00 on a Monday in Tashkent is 00:00Z that day: the end of a fit's data window and the start of that Monday's plan
# day.
MONDAY = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
FITTED = MONDAY + timedelta(minutes=30)
NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # Wednesday 11:00 in Tashkent
_phones = itertools.count(900000401)

# The `params` JSON the weekly job writes (the plan's Ruling P4): a thin bucket keeps its n and no values.
PARAMS = {
    "cycle_points": [[0.1, 13.5], [0.8, 16.0], [2.3, 24.0], [4.6, 32.0], [7.7, 44.0], [13.2, 67.0]],
    "depot_line": [11.0, 1.4],
    "window_buckets": {
        "0|started": {"lo": None, "hi": None, "n": 12},
        "1-2|started": {"lo": -33, "hi": 77, "n": 48},
        "1-2|not_started": {"lo": -50, "hi": 140, "n": 41},
    },
    "monitoring": {"by_hour_band": {"11-13": 0.97}, "by_bottles_band": {"0-5": 1.01}},
}


def _fit(**columns) -> EtaModelFit:
    values = {
        "fitted_at": FITTED,
        "data_from": MONDAY - timedelta(days=28),
        "data_to": MONDAY,
        "params": PARAMS,
        "rows_used": {"cycle": 412, "depot": 31, "window": 640},
        "kept_previous": {"depot": "thin: 9 pairs"},
    }
    return EtaModelFit(**{**values, **columns})


def _columns(pg_db, table):
    rows = pg_db.session.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table ORDER BY ordinal_position"
        ),
        {"table": table},
    )
    return [tuple(row) for row in rows]


def test_the_table_holds_exactly_the_documented_columns(pg_db):
    """The migration's own columns, types and nullability: the fit's window and results are all required."""
    assert _columns(pg_db, FITS) == [
        ("id", "integer", "NO"),
        ("fitted_at", "timestamp with time zone", "NO"),
        ("data_from", "timestamp with time zone", "NO"),
        ("data_to", "timestamp with time zone", "NO"),
        ("params", "json", "NO"),
        ("rows_used", "json", "NO"),
        ("kept_previous", "json", "NO"),
    ]


def test_the_promised_day_is_an_optional_date_on_the_promise_table(pg_db):
    columns = {name: (data_type, nullable) for name, data_type, nullable in _columns(pg_db, "delivery_promises")}

    assert columns["promised_date"] == ("date", "YES")


def test_fits_are_unique_per_data_window(pg_db):
    """The weekly job is idempotent per window: a second row for the same `data_to` is refused by name."""
    pg_db.session.add(_fit())
    pg_db.session.commit()

    _rollback_on_named_integrity(pg_db, _fit(fitted_at=FITTED + timedelta(hours=1)), "uq_eta_model_fits_data_to")

    # The next week's window is another row, and the refused one left nothing behind.
    pg_db.session.add(_fit(fitted_at=FITTED + timedelta(days=7), data_to=MONDAY + timedelta(days=7)))
    pg_db.session.commit()
    assert EtaModelFit.query.count() == 2


def test_the_unique_window_and_the_fitted_at_index_are_live_on_their_columns(pg_db):
    assert _constraint_def(pg_db, FITS, "uq_eta_model_fits_data_to") == "UNIQUE (data_to)"
    assert _index_def(pg_db, "ix_eta_model_fits_fitted_at") == (
        "CREATE INDEX ix_eta_model_fits_fitted_at ON public.eta_model_fits USING btree (fitted_at)"
    )


def test_the_migrated_table_matches_the_model(pg_db):
    """Scoped to the one new table, so unrelated drift elsewhere in the schema never fails this. The promise table's
    new column is compared by test_delivery_promise_pg_constraints.py::test_the_migrated_table_matches_the_model."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name == FITS
        return obj.table.name == FITS

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []


def test_a_fit_reads_back_as_the_parameters_the_engine_plans_with(pg_db):
    """The real column types end to end: JSON nulls stay null, floats stay floats, and `fitted_at <= now` compares
    time zones the way production does."""
    older = _fit(fitted_at=FITTED - timedelta(days=7), data_to=MONDAY - timedelta(days=7))
    newer = _fit()
    later = _fit(fitted_at=NOW + timedelta(days=5), data_to=MONDAY + timedelta(days=7))  # not yet written at NOW
    pg_db.session.add_all([older, newer, later])
    pg_db.session.commit()

    params = current_params(NOW)

    assert params.source == f"fit:{newer.id}"
    assert params.cycle_points == tuple(tuple(point) for point in PARAMS["cycle_points"])
    assert params.depot_line == (11.0, 1.4)
    assert params.window_buckets["0|started"] == {"lo": None, "hi": None, "n": 12}
    # A thin bucket borrows, a fitted one is read as stored.
    assert lo_hi(params, 0, True) == (-33, 77, False)
    assert lo_hi(params, 2, False) == (-50, 140, False)
    # Before the first fit was written there is none: the seeds.
    assert current_params(older.fitted_at - timedelta(seconds=1)).source == "seed"


@pytest.fixture
def delivery(pg_db):
    """A delivery that is waiting in the pool, committed."""
    customer = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name="Kamola")
    pg_db.session.add(customer)
    pg_db.session.commit()
    order = Order(user_id=customer.id, subtotal=Decimal("15000.00"), total_amount=Decimal("15000.00"))
    pg_db.session.add(order)
    pg_db.session.commit()
    row = Delivery(
        order_id=order.id,
        status=DeliveryStatus.PENDING,
        scheduled_date=NOW,
        scheduled_time_slot="09:00-12:00",
    )
    pg_db.session.add(row)
    pg_db.session.commit()
    return row


def test_a_phase_one_row_inserts_without_a_promised_date(pg_db, delivery):
    """Phase 1 writers know nothing of the new column. A promise written the old way, through the model and through the
    bare column list of migration 9e7056f6124a, commits and reads back NULL; an engine row can carry the date."""
    pg_db.session.add(
        DeliveryPromise(
            delivery_id=delivery.id, stage="on_the_way", model_version=MODEL_GOOGLE_TAP, computed_at=NOW, shown=True
        )
    )
    pg_db.session.commit()
    pg_db.session.execute(
        text(
            "INSERT INTO delivery_promises (delivery_id, computed_at, stage, model_version, shown) "
            "VALUES (:delivery_id, :computed_at, 'on_the_way', 'osrm_shadow_v1', false)"
        ),
        {"delivery_id": delivery.id, "computed_at": NOW},
    )
    pg_db.session.add(
        DeliveryPromise(
            delivery_id=delivery.id,
            stage="waiting",
            model_version=MODEL_ENGINE_S1,
            computed_at=NOW,
            day_kind="date",
            promised_date=date(2026, 11, 9),
        )
    )
    pg_db.session.commit()

    stored = {row.model_version: row.promised_date for row in DeliveryPromise.query.filter_by(delivery_id=delivery.id)}

    assert stored == {MODEL_GOOGLE_TAP: None, "osrm_shadow_v1": None, MODEL_ENGINE_S1: date(2026, 11, 9)}


FITS_REVISION = "533fdcecfeff"
PROMISES_REVISION = "9e7056f6124a"  # the parent: delivery_promises as Phase 1 shipped it, and no eta_model_fits
# The columns of delivery_promises at the parent revision (migration 9e7056f6124a).
PHASE_ONE_PROMISE_COLUMNS = {
    "id",
    "delivery_id",
    "delivery_status_history_id",
    "computed_at",
    "stage",
    "expected_at",
    "window_start",
    "window_end",
    "day_kind",
    "stops_ahead",
    "day_started",
    "drivers_working",
    "model_version",
    "source",
    "shown",
}


@pytest.fixture
def empty_pg_database():
    """A transient, EMPTY Postgres database: the test below replays the migrations itself."""
    with transient_pg_database("eta_fits_rt") as url:
        yield url


def test_the_downgrade_removes_exactly_what_the_upgrade_added(empty_pg_database):
    """The parent revision has neither the table nor the column; this one has both, and nothing else of the promise
    table moved; the second upgrade brings them back. (The base roundtrip only proves the downgrade runs, not that it
    took the right objects with it.)"""
    from alembic import command

    from business_app import db

    def state():
        with db.engine.connect() as conn:
            return _schema_columns(conn, FITS), _schema_columns(conn, "delivery_promises")

    app = _migration_app(empty_pg_database)
    with app.app_context():
        config = app.extensions["migrate"].migrate.get_config()
        command.upgrade(config, PROMISES_REVISION)
        assert state() == (set(), PHASE_ONE_PROMISE_COLUMNS)

        command.upgrade(config, FITS_REVISION)
        fit_columns, promise_columns = state()
        assert fit_columns == {"id", "fitted_at", "data_from", "data_to", "params", "rows_used", "kept_previous"}
        assert promise_columns == PHASE_ONE_PROMISE_COLUMNS | {"promised_date"}

        command.downgrade(config, PROMISES_REVISION)
        assert state() == (set(), PHASE_ONE_PROMISE_COLUMNS)
        assert _revision(db.engine) == PROMISES_REVISION

        command.upgrade(config, FITS_REVISION)
        assert state() == (fit_columns, promise_columns)
