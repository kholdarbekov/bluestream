"""TST-004: Alembic migration upgrade → downgrade → upgrade roundtrip.

Catches the classic rollout-day surprises:
  - ``downgrade()`` drops a column or table that a later (still-applied)
    migration refers to.
  - ``upgrade()`` assumes data that an earlier migration neglected to backfill.
  - ``downgrade()`` is a no-op or raises, so a real rollback would fail.

The test runs against an ephemeral Postgres database created on the configured
``DATABASE_URL`` server. SQLite is rejected because production runs on
Postgres and several migrations use Postgres-only constructs (e.g. JSONB,
``ENCODE(DIGEST(...))``).
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError


REQUIRES_PG_REASON = (
    "TST-004 migration roundtrip requires PostgreSQL. "
    "Set DATABASE_URL to a Postgres URL with permission to CREATE/DROP databases."
)


def _resolve_database_url() -> str:
    """Pick the most reliable Postgres DSN for the current environment.

    Local dev sometimes has a stale ``DATABASE_URL`` in ``.env`` whose
    password no longer matches the running ``postgres`` container's
    ``POSTGRES_PASSWORD``. When ``POSTGRES_USER``/``PASSWORD``/``DB`` are all
    set, build the DSN from them so we always agree with whatever credentials
    the postgres service actually started with.
    """
    user = os.environ.get('POSTGRES_USER')
    password = os.environ.get('POSTGRES_PASSWORD')
    database = os.environ.get('POSTGRES_DB')
    if user and password and database:
        host = os.environ.get('POSTGRES_HOST', 'postgres')
        port = os.environ.get('POSTGRES_PORT', '5432')
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return os.environ.get('DATABASE_URL', '')


def _admin_engine_for(database_url: str):
    """Engine pointed at the ``postgres`` maintenance DB for CREATE/DROP DATABASE."""
    admin_url = make_url(database_url).set(database='postgres')
    # ``str(URL)`` redacts the password as ``***`` — pass the URL object
    # directly so SQLAlchemy keeps the real credential.
    return create_engine(admin_url, isolation_level='AUTOCOMMIT')


@pytest.fixture
def ephemeral_pg_database():
    """Create a transient Postgres database, yield its URL, drop on teardown."""
    base_url = _resolve_database_url()
    if not base_url.startswith(('postgresql://', 'postgresql+', 'postgres://')):
        pytest.skip(REQUIRES_PG_REASON)

    admin_engine = _admin_engine_for(base_url)
    db_name = f"migr_rt_{uuid.uuid4().hex[:12]}"
    quoted = f'"{db_name}"'

    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE {quoted}'))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres unreachable for migration roundtrip: {exc.orig}")

    # render_as_string(hide_password=False) keeps the real credential;
    # ``str(URL)`` would redact it to ``***``.
    target_url = make_url(base_url).set(database=db_name).render_as_string(
        hide_password=False
    )
    try:
        yield target_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {'db': db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS {quoted}'))
        admin_engine.dispose()


@pytest.mark.integration
def test_migrations_upgrade_downgrade_upgrade(ephemeral_pg_database):
    """``alembic upgrade head → downgrade base → upgrade head`` must succeed."""
    from business_app import create_app
    from flask_migrate import downgrade, upgrade

    app = create_app({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': ephemeral_pg_database,
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SECRET_KEY': 'test-secret-key-for-migration-roundtrip-32-chars',
        'JWT_SECRET_KEY': 'test-jwt-secret-key-for-migration-roundtrip',
        'CELERY_ALWAYS_EAGER': True,
    })

    with app.app_context():
        upgrade(revision='head')
        downgrade(revision='base')
        upgrade(revision='head')
