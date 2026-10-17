"""The pre-existing writers of `is_bot_active` now go through TelegramReachabilityService (spec
2026-10-09 §7.2 d), driven through the endpoints and the merge the product really uses."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from business_app.models.reorder_reminder import ReorderReminderEvent
from business_app.models.user import User
from business_app.services.cross_platform_sync_service import cross_platform_sync_service
from business_app.services.token_service import TokenService
from business_app.utils.password_security import hash_password
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import UserRole, UserStatus, UserType

pytestmark = pytest.mark.integration

FIXED_BLOCK = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)


def _signed_login(app, client, body):
    raw = json.dumps(body).encode()
    signature = hmac.new(app.config["BOT_WEBHOOK_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/auth/telegram-login",
        data=raw,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature},
    )


def _customer(db, *, phone, email, source, telegram_id=None, is_bot_active=False, blocked_at=None, created_at=None):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="Kamola",
        last_name="Y",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source=source,
        telegram_id=telegram_id,
        is_bot_active=is_bot_active,
        telegram_blocked_at=blocked_at,
        is_verified=True,
        created_at=created_at or datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _state(db, user_id):
    db.session.expire_all()
    user = db.session.get(User, user_id)
    blocked_at = ensure_utc(user.telegram_blocked_at) if user.telegram_blocked_at is not None else None
    return user.telegram_id, user.is_bot_active, blocked_at


def _event_kinds(user_id):
    return [
        (row.kind, row.detail)
        for row in ReorderReminderEvent.query.filter_by(user_id=user_id).order_by(ReorderReminderEvent.id)
    ]


def test_registering_through_the_bot_makes_the_customer_reachable(client, db):
    response = client.post(
        "/api/v1/auth/telegram-register",
        json={"telegram_id": 778899201, "first_name": "Kamola", "language_code": "uz"},
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    user = User.query.filter_by(telegram_id="778899201").one()
    assert _state(db, user.id) == ("778899201", True, None)
    assert _event_kinds(user.id) == []


def test_a_bot_login_unblocks_a_customer_the_backend_held_as_blocked(app, client, db):
    """The bot logs a customer in when they use it, so the login is evidence they unblocked it."""
    blocked_at = datetime.now(UTC) - timedelta(hours=1)
    user = _customer(
        db,
        phone="+998901110801",
        email="kamola801@example.com",
        source="telegram",
        telegram_id="810000801",
        blocked_at=blocked_at,
    )

    response = _signed_login(app, client, {"telegram_id": 810000801, "first_name": "Kamola"})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _state(db, user.id) == ("810000801", True, None)
    [event] = ReorderReminderEvent.query.filter_by(user_id=user.id).all()
    assert (event.kind, event.detail) == ("unblocked", "telegram_login")
    assert datetime.fromisoformat(event.payload["at"]) > blocked_at


def test_a_first_bot_login_creates_a_reachable_customer(app, client, db):
    """`authenticate_telegram_user`, new-user branch: the login creates the account, and a customer
    who just used the bot has a working chat. Without the service call the column default
    (`is_bot_active` False) would leave every first-time bot user "not connected"."""
    assert User.query.filter_by(telegram_id="810000701").first() is None

    response = _signed_login(app, client, {"telegram_id": 810000701, "first_name": "Kamola", "username": "kamola701"})

    assert response.status_code == 200, response.get_data(as_text=True)
    user = User.query.filter_by(telegram_id="810000701").one()
    assert _state(db, user.id) == ("810000701", True, None)
    assert _event_kinds(user.id) == []


def test_linking_telegram_from_the_website_makes_the_customer_reachable(client, db):
    """`/auth/verify-telegram-auth/<code>`: the site issued a code, the bot redeemed it."""
    user = _customer(
        db,
        phone="+998901110901",
        email="kamola901@example.com",
        source="web",
        blocked_at=datetime.now(UTC) - timedelta(days=3),
    )
    token_service = TokenService()
    token_service._ensure_redis_connection()
    if not token_service.redis_available:
        pytest.skip("verify-telegram-auth reads its code from Redis")
    token_service.redis_client.setex(
        "telegram_auth:K4M0LA77",
        300,
        json.dumps({"user_id": user.id, "email": user.email, "created_at": datetime.now(UTC).isoformat(), "type": "telegram_auth"}),
    )

    response = client.post(
        "/api/v1/auth/verify-telegram-auth/K4M0LA77",
        json={"telegram_id": "810000901", "telegram_username": "kamola_test"},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _state(db, user.id) == ("810000901", True, None)
    assert _event_kinds(user.id) == [("unblocked", "link")]


def test_merging_a_telegram_account_into_a_web_account_moves_its_block(db):
    web = _customer(db, phone="+998901110911", email="web911@example.com", source="web")
    telegram = _customer(
        db,
        phone="+998901110912",
        email="telegram_810000912@bluestream.local",
        source="telegram",
        telegram_id="810000912",
        blocked_at=FIXED_BLOCK,
    )

    result = cross_platform_sync_service.auto_link_accounts(primary_user=web, secondary_user=telegram, link_type="merge")

    assert result["success"] is True, result.get("error")
    assert _state(db, web.id) == ("810000912", False, FIXED_BLOCK)
    assert ReorderReminderEvent.query.count() == 0


@pytest.mark.parametrize(
    "is_bot_active, blocked_at",
    [(True, None), (False, FIXED_BLOCK)],
    ids=["reachable-chat", "blocked-chat"],
)
def test_a_same_platform_merge_copies_both_fields(db, is_bot_active, blocked_at):
    older = _customer(
        db,
        phone="+998901110921",
        email="older921@example.com",
        source="web",
        created_at=datetime.now(UTC) - timedelta(days=30),
    )
    newer = _customer(
        db,
        phone="+998901110922",
        email="newer922@example.com",
        source="web",
        telegram_id="810000922",
        is_bot_active=is_bot_active,
        blocked_at=blocked_at,
    )

    result = cross_platform_sync_service.auto_link_accounts(primary_user=older, secondary_user=newer, link_type="merge")

    assert result["success"] is True, result.get("error")
    assert _state(db, older.id) == ("810000922", is_bot_active, blocked_at)
    assert ReorderReminderEvent.query.count() == 0
