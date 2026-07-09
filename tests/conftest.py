"""
Test configuration and fixtures for BlueStream test suite
"""
import pytest
import os
import sys
import redis
from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import Mock, MagicMock
import asyncio
from typing import Dict, Any, Optional
from urllib.parse import urlparse
import requests

# Set required environment variables for testing
os.environ.setdefault('DB_PASSWORD', 'test_password')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-32-chars-long')
os.environ.setdefault('JWT_SECRET_KEY', 'test-jwt-secret-key-for-testing')
# Bot↔backend HMAC secrets are now required at startup (no SECRET_KEY/JWT
# fallback — see config validation in telegram_bot/config.py and
# staff_bot/config.py). Tests that import bot modules would fail collection
# without these, so seed distinct test values here. Each is a separate
# trust boundary in production; using different strings in tests guards
# against any accidental cross-domain reuse creeping back in.
os.environ.setdefault('BOT_WEBHOOK_SECRET', 'test-bot-webhook-secret')
os.environ.setdefault('WEBHOOK_SECRET', 'test-staff-webhook-secret')
# Force testing mode even when docker-compose service env defaults to production.
os.environ['FLASK_ENV'] = 'testing'
os.environ['TESTING'] = 'true'

# Hermetic containment (2026-07-08 staff-bot broadcast incident): @shared_task
# resolves through Celery's *current app*. In a pytest process that never
# imports business_app.tasks.celery_app, that is Celery's DEFAULT app, which
# reads CELERY_BROKER_URL straight from the environment — under the docker
# test runner that was .env's live broker (redis DB 0), so test-triggered
# .delay() calls became real tasks: the dev celery_worker executed them
# against the dev DB and the staff bot broadcast "new order" Telegram messages
# to every delivery person, once per test run. Pin every broker and
# outbound-webhook knob to an inert value BEFORE any business_app / bot module
# can snapshot it at import time. Unroutable hosts use RFC 2606 `.invalid` so
# an unmocked call fails fast instead of reaching the live compose services.
os.environ['CELERY_BROKER_URL'] = 'memory://'
os.environ['CELERY_RESULT_BACKEND'] = 'cache+memory://'
os.environ['STAFF_BOT_WEBHOOK_URL'] = 'http://staff-bot-must-be-mocked.invalid'
os.environ['BOT_WEBHOOK_URL'] = 'http://telegram-bot-must-be-mocked.invalid'
os.environ['BUSINESS_APP_URL'] = 'http://api-must-be-mocked.invalid'


def _force_nonzero_redis_db(url: str) -> str:
    """Rewrite a redis URL so it can never point at DB 0 (the live broker).

    The autouse ``reset_redis_state`` fixture calls ``flushdb()`` on whatever
    ``REDIS_URL`` resolves to; ``.env``'s value is the compose stack's DB 0,
    so a pytest run without the runner script's DB-15 override would wipe the
    live Celery broker and app cache. URLs without an explicit DB segment, or
    with DB 0, are forced to DB 15.
    """
    scheme_split = url.split('://', 1)
    if len(scheme_split) != 2:
        return url
    scheme, rest = scheme_split
    host_part, sep, db = rest.rpartition('/')
    if sep and db.isdigit() and int(db) != 0:
        return url
    if not sep:
        host_part = rest
    return f"{scheme}://{host_part}/15"


os.environ['REDIS_URL'] = _force_nonzero_redis_db(os.environ.get('REDIS_URL', 'redis://redis:6379/15'))

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from business_app import create_app
from business_app.models.user import User
from business_app.models.product import Product, ProductCategory
from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import UserRole, UserType, OrderStatus, PaymentStatus, PaymentMethod
from business_app.utils.password_security import hash_password


def _per_worker_redis_url(base_url: str) -> str:
    """Allocate a unique Redis DB per pytest-xdist worker (TST-006).

    With ``-n auto``, multiple workers run in parallel against the same Redis
    instance. The autouse ``reset_redis_state`` fixture calls ``flushdb()``,
    which would wipe other workers' setup state if every worker shared one DB.
    Map ``gwN`` to ``DB (15 - N mod 15)`` so workers stay isolated within
    Redis's default 16-DB range while never touching DB 0 — that's the live
    broker/cache the compose stack runs on (collisions only start at 15+
    workers, far above the runner's ``-n 4``). Falls back to ``base_url`` for
    the master process or unrecognised worker names.
    """
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'master')
    if not worker.startswith('gw'):
        return base_url
    try:
        worker_num = int(worker[2:])
    except ValueError:
        return base_url
    db_index = 15 - (worker_num % 15)
    scheme_split = base_url.split('://', 1)
    if len(scheme_split) != 2:
        return base_url
    scheme, rest = scheme_split
    host_part = rest.rsplit('/', 1)[0] if '/' in rest else rest
    return f"{scheme}://{host_part}/{db_index}"


@pytest.fixture(scope='session')
def app():
    """Create Flask app for testing"""
    base_redis_url = os.environ.get('REDIS_URL', 'redis://redis:6379/15')
    test_config = {
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test-secret-key',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'JWT_SECRET_KEY': 'test-jwt-secret',
        'REDIS_URL': _per_worker_redis_url(base_redis_url),
        'CELERY_ALWAYS_EAGER': True,  # Run tasks synchronously in tests
    }

    app = create_app(test_config)

    with app.app_context():
        yield app


@pytest.fixture(scope='session')
def client(app):
    """Create test client"""
    return app.test_client()


@pytest.fixture(scope='session')
def runner(app):
    """Create test CLI runner"""
    return app.test_cli_runner()


@pytest.fixture(scope='function')
def db(app):
    """Create database tables for each test"""
    from business_app import db as _db

    with app.app_context():
        _db.create_all()
        yield _db
        _db.session.remove()
        _db.drop_all()


class _QueryCounter:
    """Context manager returned by the `count_queries` fixture.

    Usage:
        def test_admin_orders_no_n_plus_one(client, count_queries, ...):
            with count_queries() as counter:
                client.get('/api/admin/orders?page=1&per_page=50')
            assert counter.count <= 15  # N+1 guard (ARCH-009)
    """

    def __init__(self):
        self.count = 0
        self.statements: list = []
        self._engine = None
        self._listener = None

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1
        self.statements.append(statement)

    def __enter__(self):
        from sqlalchemy import event
        from business_app import db as _db

        self._engine = _db.engine
        self._listener = self._on_execute
        event.listen(self._engine, 'before_cursor_execute', self._listener)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        from sqlalchemy import event

        if self._engine is not None and self._listener is not None:
            event.remove(self._engine, 'before_cursor_execute', self._listener)
        return False


@pytest.fixture
def count_queries():
    """Factory that yields a SQL query counter (ARCH-009 N+1 regression guard)."""
    return _QueryCounter


@pytest.fixture(autouse=True)
def reset_redis_state(app):
    """Keep Redis-backed counters/sessions isolated between tests.

    TST-005: flush is the **pre-test invariant** — every test starts against
    an empty DB regardless of what the previous test left behind. The
    post-yield flush is belt-and-suspenders for the next test if this one
    crashed mid-fixture. Ordering must not matter.
    """
    redis_url = app.config.get('REDIS_URL')

    def _safe_flush():
        try:
            redis.from_url(redis_url).flushdb()
        except Exception:
            # Tests should continue even if Redis is unavailable.
            pass

    _safe_flush()
    yield
    _safe_flush()


@pytest.fixture(autouse=True)
def block_external_side_effects(monkeypatch):
    """
    Prevent any real-world side effects during tests.

    Guards against:
    - Outbound HTTP requests (email/SMS/telegram providers)
    - Celery task queue publishing via `.delay()`/`.apply_async()`
    - Real notification delivery paths
    """
    from celery.app.task import Task
    from business_app.services.notification_service import NotificationService

    def _blocked_http_request(_self, method, url, *args, **kwargs):
        parsed = urlparse(str(url))
        host = (parsed.hostname or "").lower()
        raise RuntimeError(
            f"Outbound HTTP blocked during tests: {method} {url} (host={host or 'unknown'})"
        )

    def _mock_async_publish(_self, *args, **kwargs):
        result = Mock()
        result.id = "mock-task-id"
        result.status = "MOCKED"
        return result

    def _mock_send_notification(_self, user_id, notification_type, channels=None,
                                template_data=None, priority='normal'):
        if channels:
            response = {}
            for channel in channels:
                channel_name = channel.value if hasattr(channel, 'value') else str(channel)
                response[channel_name] = {'success': True, 'mocked': True}
            return response
        return {'mocked': {'success': True, 'mocked': True}}

    def _mock_send_sms_to_phone(_self, phone, notification_type, template_key,
                                template_data, language='uz'):
        return {
            'success': True,
            'mocked': True,
            'phone': phone,
            'template_key': template_key,
            'language': language
        }

    monkeypatch.setattr(requests.sessions.Session, 'request', _blocked_http_request, raising=True)
    monkeypatch.setattr(Task, 'delay', _mock_async_publish, raising=True)
    monkeypatch.setattr(Task, 'apply_async', _mock_async_publish, raising=True)
    monkeypatch.setattr(NotificationService, 'send_notification', _mock_send_notification, raising=True)
    monkeypatch.setattr(NotificationService, 'send_sms_to_phone', _mock_send_sms_to_phone, raising=True)


@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    mock = MagicMock()
    mock.get.return_value = None
    mock.set.return_value = True
    mock.setex.return_value = True
    mock.incr.return_value = 1
    mock.expire.return_value = True
    mock.delete.return_value = 1
    mock.ttl.return_value = 300
    mock.exists.return_value = 0
    return mock


@pytest.fixture
def sample_user(db):
    """Create a sample user for testing"""
    user = User(
        email='test@example.com',
        phone='+998901234567',
        password_hash=hash_password('TestPassword123!'),
        first_name='Test',
        last_name='User',
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def admin_user(db):
    """Create an admin user for testing"""
    user = User(
        email='admin@example.com',
        phone='+998901234568',
        password_hash=hash_password('AdminPassword123!'),
        first_name='Admin',
        last_name='User',
        user_type=UserType.STAFF,
        role=UserRole.ADMIN,
        is_verified=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def delivery_driver(db):
    """Create a delivery driver for testing"""
    user = User(
        email='driver@example.com',
        phone='+998901234569',
        password_hash=hash_password('DriverPassword123!'),
        first_name='Delivery',
        last_name='Driver',
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def sample_category(db):
    """Create a sample product category for testing."""
    category = ProductCategory(
        name='Water',
        description='Water products',
        is_active=True
    )
    db.session.add(category)
    db.session.commit()
    return category


@pytest.fixture
def sample_product(db, sample_category):
    """Create a sample product for testing"""
    product = Product(
        name='Pure Water 19L',
        description='Premium pure water in 19L bottle',
        category_id=sample_category.id,
        size='19L',
        volume=19.0,
        volume_unit='L',
        base_price=Decimal('15000.00'),
        stock_quantity=100,
        min_stock_level=10,
        max_stock_level=500,
        is_active=True,
        created_at=datetime.now(UTC)
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def sample_order(db, sample_user, sample_product):
    """Create a sample order for testing"""
    order = Order(
        user_id=sample_user.id,
        order_number='ORD-TEST-001',
        status=OrderStatus.PENDING,
        subtotal=Decimal('15000.00'),
        delivery_fee=Decimal('3000.00'),
        discount_amount=Decimal('0.00'),
        loyalty_discount=Decimal('0.00'),
        total_amount=Decimal('18000.00'),
        delivery_notes='Test order',
        created_at=datetime.now(UTC)
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.fixture
def sample_payment(db, sample_order):
    """Create a sample payment for testing"""
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CARD,
        amount=sample_order.total_amount,
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id='test_payment_123',
        provider_transaction_id='test_tx_456',
        created_at=datetime.now(UTC)
    )
    db.session.add(payment)
    db.session.commit()
    return payment


@pytest.fixture
def auth_token(app, sample_user):
    """Create a valid JWT access token for a regular user."""
    from flask_jwt_extended import create_access_token

    with app.app_context():
        return create_access_token(identity=str(sample_user.id))


@pytest.fixture
def admin_token(app, admin_user):
    """Create a valid JWT access token for an admin user."""
    from flask_jwt_extended import create_access_token

    with app.app_context():
        return create_access_token(identity=str(admin_user.id))


@pytest.fixture
def auth_headers(auth_token):
    """Create authentication headers for API testing"""
    return {
        'Authorization': f'Bearer {auth_token}',
        'Content-Type': 'application/json'
    }


@pytest.fixture
def admin_auth_headers(admin_token):
    """Create admin authentication headers for API testing"""
    return {
        'Authorization': f'Bearer {admin_token}',
        'Content-Type': 'application/json'
    }


# Mock services for testing
@pytest.fixture
def mock_payment_service():
    """Mock payment service"""
    mock = MagicMock()
    mock.process_payment.return_value = {
        'success': True,
        'payment_id': 'test_payment_123',
        'transaction_id': 'test_tx_456',
        'status': 'completed'
    }
    mock.refund_payment.return_value = {
        'success': True,
        'refund_id': 'test_refund_123',
        'status': 'refunded'
    }
    return mock


@pytest.fixture
def mock_inventory_service():
    """Mock inventory service"""
    mock = MagicMock()
    mock.check_availability.return_value = True
    mock.reserve_stock.return_value = True
    mock.release_stock.return_value = True
    mock.update_stock.return_value = True
    return mock


@pytest.fixture
def mock_delivery_service():
    """Mock delivery service"""
    mock = MagicMock()
    mock.calculate_delivery_fee.return_value = Decimal('3000.00')
    mock.estimate_delivery_time.return_value = '2 hours'
    mock.get_available_time_slots.return_value = [
        {'id': 1, 'name': 'Morning', 'time_range': '9:00-12:00'},
        {'id': 2, 'name': 'Afternoon', 'time_range': '14:00-17:00'}
    ]
    return mock


@pytest.fixture
def mock_notification_service():
    """Mock notification service"""
    mock = MagicMock()
    mock.send_notification.return_value = True
    mock.send_email.return_value = True
    mock.send_sms.return_value = True
    return mock


# Test data fixtures
@pytest.fixture
def valid_order_data():
    """Valid order data for testing"""
    return {
        'items': [
            {
                'product_id': 1,
                'quantity': 2,
                'unit_price': '15000.00'
            }
        ],
        'delivery_address': {
            'address_line1': '123 Test Street',
            'city': 'Tashkent',
            'latitude': 41.2995,
            'longitude': 69.2401
        },
        'delivery_time_slot_id': 1,
        'notes': 'Test order notes',
        'payment_method': 'card'
    }


@pytest.fixture
def valid_payment_data():
    """Valid payment data for testing"""
    return {
        'amount': '18000.00',
        'currency': 'UZS',
        'payment_method': 'card',
        'card_token': 'test_card_token_123',
        'return_url': 'https://example.com/payment/return',
        'callback_url': 'https://example.com/payment/callback'
    }


@pytest.fixture
def valid_user_data():
    """Valid user registration data for testing"""
    return {
        'email': 'newuser@example.com',
        'phone': '+998901234570',
        'password': 'SecureP@ssw0rd123',
        'first_name': 'New',
        'last_name': 'User',
        'language': 'en'
    }


# Security testing fixtures
@pytest.fixture
def malicious_payloads():
    """Common malicious payloads for security testing"""
    return {
        'sql_injection': [
            "'; DROP TABLE users; --",
            "1' OR '1'='1",
            "admin'--",
            "' UNION SELECT * FROM users--"
        ],
        'xss': [
            "<script>alert('xss')</script>",
            "javascript:alert('xss')",
            "<img src=x onerror=alert('xss')>",
            "';alert('xss');//"
        ],
        'path_traversal': [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32\\config\\sam",
            "%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "....//....//....//etc//passwd"
        ],
        'command_injection': [
            "; ls -la",
            "| cat /etc/passwd",
            "&& rm -rf /",
            "`whoami`"
        ]
    }


# Performance testing fixtures
@pytest.fixture
def performance_test_data():
    """Data for performance testing"""
    return {
        'concurrent_users': 100,
        'requests_per_user': 10,
        'max_response_time': 2.0,  # seconds
        'target_throughput': 1000,  # requests per minute
    }


# Async testing support
@pytest.fixture(scope='session')
def event_loop():
    """Create an event loop for async tests"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Database transaction fixtures
@pytest.fixture
def db_transaction(db):
    """Provide database transaction that can be rolled back"""
    connection = db.engine.connect()
    transaction = connection.begin()

    # Configure session to use this connection
    db.session.configure(bind=connection)

    yield db

    # Rollback transaction
    transaction.rollback()
    connection.close()
    db.session.remove()


# Test categories
@pytest.fixture
def test_categories():
    """Define test categories for organization"""
    return {
        'critical': ['payment', 'order', 'auth', 'security'],
        'high': ['inventory', 'delivery', 'loyalty'],
        'medium': ['notification', 'subscription', 'reporting'],
        'low': ['ui', 'logging', 'caching']
    }
