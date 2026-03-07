"""Regression checks for notification preferences uniqueness migration."""

from pathlib import Path

from sqlalchemy import inspect


def test_notification_preferences_unique_constraint_exists(db):
    inspector = inspect(db.engine)
    unique_constraints = inspector.get_unique_constraints('notification_preferences')

    assert any(
        constraint.get('column_names') == ['user_id', 'notification_type', 'channel']
        for constraint in unique_constraints
    )


def test_delivery_telegram_notification_migration_dedupes_before_constraint():
    versions_dir = Path(__file__).resolve().parents[2] / 'business_app' / 'migrations' / 'versions'
    matching_migrations = sorted(
        path
        for path in versions_dir.glob('*.py')
        if 'delivery_telegram_notification' in path.name
    )

    assert matching_migrations, 'Expected generated delivery telegram notification migration to exist'

    migration_text = matching_migrations[-1].read_text()
    assert 'ROW_NUMBER() OVER' in migration_text
    assert 'PARTITION BY user_id, notification_type, channel' in migration_text
    assert 'uq_notification_preferences_user_type_channel' in migration_text
