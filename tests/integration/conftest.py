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

from tests.integration.fake_gateways import apply_test_provider_secrets


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


# --------------------------------------------------------------------------- #
# Shared payment-webhook fixtures (used by test_payment_matrix.py and
# test_click_crash_recovery.py). Kept here so both modules resolve them via
# fixture discovery instead of cross-module imports (pytest fixtures do not
# import across test modules).
# --------------------------------------------------------------------------- #

@pytest.fixture
def matrix_app(app):
    """Apply test provider secrets and return the same app fixture."""
    apply_test_provider_secrets(app)
    # Payme uses the *_with_billing variants for signature verification on
    # webhook receipts; mirror the primary key.
    app.config['PAYME_MERCHANT_ID_WITH_BILLING'] = app.config['PAYME_MERCHANT_ID']
    app.config['PAYME_SECRET_KEY_WITH_BILLING'] = app.config['PAYME_SECRET_KEY']
    return app


@pytest.fixture
def matrix_client(matrix_app):
    return matrix_app.test_client()


@pytest.fixture
def no_fiscalization(monkeypatch):
    """Stub out post-payment fiscalization triggers (TST-011 territory).

    ``_handle_successful_payment`` calls ``queue_click_fiscalization`` for
    Click + Card payments after a successful webhook. Fiscalization has its
    own retry/idempotency semantics that the OFD matrix (TST-011) covers.
    Patch at the PaymentService level so any service instance picks it up.
    """
    from business_app.services.payment_service import PaymentService
    monkeypatch.setattr(
        PaymentService,
        'queue_click_fiscalization',
        lambda self, payment_id: None,
        raising=True,
    )


@pytest.fixture
def sample_address(db, sample_user):
    """Default delivery address for sample_user.

    ARCH-006 enforces ``delivery_address_id IS NOT NULL`` on the
    PENDING → CONFIRMED transition. The shared ``sample_order`` fixture
    intentionally creates orders without an address (covers pre-CONFIRMED
    states), so this finding shadows it for tests that drive an order to
    paid state.
    """
    from business_app.models.user import UserAddress

    address = UserAddress(
        user_id=sample_user.id,
        title='Home',
        full_address='123 Test Street, Tashkent',
        latitude=41.2995,
        longitude=69.2401,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def order_with_address(db, sample_order, sample_address):
    """Attach the test address to ``sample_order`` so paid-state
    transitions clear the ARCH-006 guard."""
    sample_order.delivery_address_id = sample_address.id
    db.session.commit()
    return sample_order


@pytest.fixture
def two_line_order_with_one_short_pool(db):
    """Factory: make an order fiscally unfulfillable *after* its first line.

    Gives ``order`` two marking-code-requiring lines — product A, whose pool
    holds exactly the one code its line needs, and a fresh product B whose pool
    is empty. Any reservation attempt therefore covers A and is short on B,
    which is the shape that used to leave A's code durably RESERVED on webhook
    paths that answer a protocol code instead of re-raising
    (``handle_prepare`` → ``-9``, and the two bare-``except`` sites
    ``_restore_click_rail_after_offline_settlement`` /
    ``_accept_late_complete``, where the payment still ends COMPLETED).

    Returns ``(product_a, product_b)``.
    """
    from decimal import Decimal

    from business_app.models.order import OrderItem
    from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
    from shared.enums import MarkingCodeStatus

    def _apply(order, product_a):
        product_b = Product(
            name="Product B (empty pool)",
            category_id=product_a.category_id,
            size="19L",
            volume=19.0,
            volume_unit="L",
            base_price=Decimal("15000.00"),
            stock_quantity=0,
            is_active=True,
        )
        db.session.add(product_b)
        db.session.flush()

        # The planner walks lines in `(product_id, id)` order, so B must sort
        # AFTER A or the shortfall raises before A's line is ever planned and
        # every test built on this fixture silently pins nothing.
        assert product_a.id < product_b.id, (
            "B is created second precisely so it plans second; do not reorder"
        )

        db.session.add_all(
            [
                ProductFiscalProfile(
                    product_id=product_a.id,
                    fiscalization_enabled=True,
                    requires_marking_codes=True,
                    spic="SPIC-A",
                ),
                ProductFiscalProfile(
                    product_id=product_b.id,
                    fiscalization_enabled=True,
                    requires_marking_codes=True,
                    spic="SPIC-B",
                ),
                ProductMarkingCode(
                    product_id=product_a.id,
                    code=f"A-CODE-{product_a.id}",
                    status=MarkingCodeStatus.AVAILABLE,
                ),
                OrderItem(
                    order_id=order.id,
                    product_id=product_a.id,
                    quantity=1,
                    unit_price=Decimal("15000.00"),
                    total_price=Decimal("15000.00"),
                ),
                OrderItem(
                    order_id=order.id,
                    product_id=product_b.id,
                    quantity=1,
                    unit_price=Decimal("15000.00"),
                    total_price=Decimal("15000.00"),
                ),
            ]
        )
        db.session.commit()
        return product_a, product_b

    return _apply
