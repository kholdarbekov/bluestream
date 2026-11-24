"""Postgres-only guarantees of the ten-litre-trip migration: the enum value, the two line tables, their keys."""

import itertools
from importlib import import_module

import pytest

from business_app.models.bottle import (
    DriverBottleSession,
    DriverBottleSessionUnit,
    DriverBottleTransfer,
    DriverBottleTransferUnit,
)
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User
from tests.integration.test_sales_agent_pg_constraints import _rollback_on_named_integrity

pytestmark = pytest.mark.integration

_phones = itertools.count(900000801)


def _user(pg_db):
    user = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name="Davron")
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _product(pg_db):
    category = ProductCategory(name=f"Water {next(_phones)}", is_active=True)
    pg_db.session.add(category)
    pg_db.session.flush()
    product = Product(name="10 Litr suv", category_id=category.id, size="10L", base_price=1000)
    pg_db.session.add(product)
    pg_db.session.commit()
    return product


def _trip(pg_db, driver):
    trip = DriverBottleSession(driver_user_id=driver.id)
    pg_db.session.add(trip)
    pg_db.session.commit()
    return trip


def test_the_migration_chains_on_the_driver_expense_head():
    migration = import_module("business_app.migrations.versions.20261119_1200_ten_litre_trip_units")
    assert migration.down_revision == "3d8f5a1c7e92"


def test_the_migration_adds_rejected_to_the_native_enum(pg_db):
    values = [
        row[0]
        for row in pg_db.session.execute(
            pg_db.text("SELECT unnest(enum_range(NULL::driver_bottle_transfer_status))::text")
        )
    ]
    assert "rejected" in values


def test_the_product_setting_defaults_to_false_in_the_database(pg_db):
    product = _product(pg_db)
    assert pg_db.session.execute(
        pg_db.text("SELECT counted_on_bottle_trip FROM products WHERE id = :i"), {"i": product.id}
    ).scalar() is False


def test_one_line_per_trip_and_product(pg_db):
    driver, product = _user(pg_db), _product(pg_db)
    trip = _trip(pg_db, driver)
    pg_db.session.add(DriverBottleSessionUnit(session_id=trip.id, product_id=product.id, loaded=3))
    pg_db.session.commit()
    _rollback_on_named_integrity(
        pg_db, DriverBottleSessionUnit(session_id=trip.id, product_id=product.id), "uq_dbsu_session_product"
    )


@pytest.mark.parametrize("column", ["loaded", "delivered", "transferred_in", "transferred_out", "returned"])
def test_a_negative_line_counter_is_refused(pg_db, column):
    driver, product = _user(pg_db), _product(pg_db)
    trip = _trip(pg_db, driver)
    line = DriverBottleSessionUnit(session_id=trip.id, product_id=product.id, **{column: -1})
    _rollback_on_named_integrity(pg_db, line, "ck_dbsu_counts_non_negative")


def test_a_line_defaults_to_zero_counters_and_a_server_side_created_at(pg_db):
    driver, product = _user(pg_db), _product(pg_db)
    trip = _trip(pg_db, driver)
    pg_db.session.execute(
        pg_db.text("INSERT INTO driver_bottle_session_units (session_id, product_id) VALUES (:s, :p)"),
        {"s": trip.id, "p": product.id},
    )
    pg_db.session.commit()
    line = DriverBottleSessionUnit.query.filter_by(session_id=trip.id).one()
    assert (line.loaded, line.delivered, line.transferred_in, line.transferred_out) == (0, 0, 0, 0)
    assert line.returned is None and line.discrepancy is None and line.created_at is not None


def _transfer(pg_db):
    sender, receiver = _user(pg_db), _user(pg_db)
    trip = _trip(pg_db, sender)
    transfer = DriverBottleTransfer(
        sender_session_id=trip.id, sender_driver_id=sender.id, receiver_driver_id=receiver.id, declared_quantity=0
    )
    pg_db.session.add(transfer)
    pg_db.session.commit()
    return transfer


def test_one_transfer_line_per_product_and_never_negative(pg_db):
    product = _product(pg_db)
    transfer = _transfer(pg_db)
    pg_db.session.add(DriverBottleTransferUnit(transfer_id=transfer.id, product_id=product.id, declared=6))
    pg_db.session.commit()
    _rollback_on_named_integrity(
        pg_db,
        DriverBottleTransferUnit(transfer_id=transfer.id, product_id=product.id, declared=1),
        "uq_dbtu_transfer_product",
    )
    for column in ("declared", "confirmed", "resolved"):
        other = _product(pg_db)
        values = {"declared": 1, column: -1}
        _rollback_on_named_integrity(
            pg_db,
            DriverBottleTransferUnit(transfer_id=transfer.id, product_id=other.id, **values),
            "ck_dbtu_counts_non_negative",
        )


def test_a_transfer_can_carry_rejected_at_and_the_rejected_status(pg_db):
    from datetime import UTC, datetime

    from shared.enums import DriverBottleTransferStatus

    transfer = _transfer(pg_db)
    transfer.status = DriverBottleTransferStatus.REJECTED
    transfer.rejected_at = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    pg_db.session.commit()
    pg_db.session.expire_all()
    assert DriverBottleTransfer.query.get(transfer.id).to_dict()["status"] == "rejected"
    assert DriverBottleTransfer.query.get(transfer.id).to_dict()["rejected_at"].startswith("2026-11-19T07:00")


def test_the_model_matches_the_migration(pg_db):
    """The migrated database against the models, scoped to what this migration added: the two line tables (columns,
    types, server defaults, both foreign keys, the unique key and the four indexes), `products.counted_on_bottle_trip`
    and `driver_bottle_transfers.rejected_at`, so unrelated drift never fails this. Alembic does not compare CHECK
    constraints; the tests above pin those by behaviour. A model edited without a migration (or the other way round)
    fails here, though every behaviour test stays green."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    tables = ("driver_bottle_session_units", "driver_bottle_transfer_units")
    added_columns = {"products": ("counted_on_bottle_trip",), "driver_bottle_transfers": ("rejected_at",)}

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name in (*tables, *added_columns)
        if obj.table.name in added_columns:
            return type_ == "column" and name in added_columns[obj.table.name]
        return True

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []
