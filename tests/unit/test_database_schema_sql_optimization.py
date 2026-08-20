"""Schema regression tests for database optimization changes."""

from pathlib import Path

from sqlalchemy import inspect


def _index_names(inspector, table_name):
    return {index["name"] for index in inspector.get_indexes(table_name)}


def test_expected_indexes_and_constraints_exist(db):
    inspector = inspect(db.engine)

    payment_uniques = inspector.get_unique_constraints("payments")
    assert any(
        constraint["column_names"] == ["order_id"]
        for constraint in payment_uniques
    )

    assert "idx_loyalty_transactions_user_created" in _index_names(inspector, "loyalty_transactions")
    assert "idx_loyalty_points_program_tier_activity" in _index_names(inspector, "loyalty_points")
    # Renamed, not dropped: `idx_orders_delivery_slot_date` was composite over
    # (delivery_time_slot, delivery_date). The free-text slot column is gone
    # (migration c9e4a1f7b3d2), so the index it led on went with it and
    # `delivery_date` — the column the release sweep filters on — carries its
    # own index instead.
    assert "idx_orders_delivery_date" in _index_names(inspector, "orders")
    assert "idx_subscriptions_status_next_billing" in _index_names(inspector, "subscriptions")
    assert "idx_subscriptions_status_next_delivery" in _index_names(inspector, "subscriptions")
    assert "idx_campaign_usage_campaign_user" in _index_names(inspector, "campaign_usage")
    assert "idx_campaign_usage_order_id" in _index_names(inspector, "campaign_usage")
    assert "idx_products_active_category" in _index_names(inspector, "products")
    assert "idx_products_active_featured" in _index_names(inspector, "products")
    assert "idx_products_active_base_price" in _index_names(inspector, "products")
    assert "idx_products_slug" in _index_names(inspector, "products")


def test_campaign_usage_foreign_keys_exist(db):
    inspector = inspect(db.engine)
    foreign_keys = inspector.get_foreign_keys("campaign_usage")
    constrained_columns = {tuple(fk["constrained_columns"]) for fk in foreign_keys}

    assert ("campaign_id",) in constrained_columns
    assert ("user_id",) in constrained_columns
    assert ("order_id",) in constrained_columns


def test_latest_migration_contains_pg_trgm_search_index():
    versions_dir = Path(__file__).resolve().parents[2] / "business_app" / "migrations" / "versions"
    matching_migrations = sorted(
        path
        for path in versions_dir.glob("*.py")
        if "database_schema_sql" in path.name or "payment_canonical" in path.name or "optimization" in path.name
    )

    assert matching_migrations, "Expected generated optimization migration to exist"

    migration_text = matching_migrations[-1].read_text()
    assert "pg_trgm" in migration_text
    assert "idx_products_search_trgm" in migration_text
