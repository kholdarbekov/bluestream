"""Shared Postgres-backed end-to-end harness for integration tests.

The default test suite runs on SQLite in-memory, which silently IGNORES
migration-only ``CHECK`` constraints (and other Postgres-only DDL). That gap is
exactly why several production CHECK-constraint violations (ARCH-006:
``ck_payments_cash_completed_requires_collector``,
``ck_deliveries_person_required_after_assigned``) and enum-value mismatches
sailed through 4,000+ green tests.

``pg_app`` / ``pg_db`` give a real, fully-migrated Postgres database per test so
the genuine constraints fire exactly as they do in production. Use these for
constraint / invariant enforcement tests; keep the fast SQLite fixtures for the
bulk of behavioural edge cases.

Tests are skipped (not failed) when no Postgres DSN is reachable, so the suite
still runs anywhere.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError


REQUIRES_PG_REASON = (
    "Postgres-backed integration test requires a Postgres DATABASE_URL "
    "with permission to CREATE/DROP databases."
)


def _resolve_database_url() -> str:
    """Prefer explicit POSTGRES_* env (matches the running container creds)."""
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    database = os.environ.get("POSTGRES_DB")
    if user and password and database:
        host = os.environ.get("POSTGRES_HOST", "postgres")
        port = os.environ.get("POSTGRES_PORT", "5432")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return os.environ.get("DATABASE_URL", "")


def _admin_engine_for(database_url: str):
    admin_url = make_url(database_url).set(database="postgres")
    return create_engine(admin_url, isolation_level="AUTOCOMMIT")


@pytest.fixture
def ephemeral_pg_database():
    """Create a transient Postgres database, yield its URL, drop on teardown."""
    base_url = _resolve_database_url()
    if not base_url.startswith(("postgresql://", "postgresql+", "postgres://")):
        pytest.skip(REQUIRES_PG_REASON)

    admin_engine = _admin_engine_for(base_url)
    db_name = f"e2e_{uuid.uuid4().hex[:12]}"
    quoted = f'"{db_name}"'

    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres unreachable for integration test: {exc.orig}")

    target_url = make_url(base_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        yield target_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
        admin_engine.dispose()


@pytest.fixture
def pg_app(ephemeral_pg_database):
    """A Flask app bound to a fresh, fully-migrated Postgres DB.

    Real CHECK constraints / enum types are present (unlike the SQLite suite),
    so invariant violations raise exactly as they would in production. Pushes an
    app context for the test body.
    """
    from business_app import create_app
    from flask_migrate import upgrade

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": ephemeral_pg_database,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test-secret-key-for-pg-e2e-32-characters",
            "JWT_SECRET_KEY": "test-jwt-secret-key-for-pg-e2e",
            "CELERY_ALWAYS_EAGER": True,
            "WTF_CSRF_ENABLED": False,
        }
    )

    with app.app_context():
        upgrade(revision="head")
        yield app


@pytest.fixture
def pg_db(pg_app):
    """The SQLAlchemy ``db`` bound to the migrated Postgres app (within context)."""
    from business_app import db

    return db
