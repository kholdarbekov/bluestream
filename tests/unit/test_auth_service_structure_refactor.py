"""Unit tests for profile/address/merge methods extracted to AuthService."""

from datetime import datetime, UTC, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.services.auth_service import AuthService
from shared.enums import PaymentMethod, SubscriptionFrequency, UserRole, UserStatus
from business_app.utils.exceptions import ValidationError, ConflictError
from business_app.utils.password_security import hash_password
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.integration.reorder_reminder_builders import seed_translations


@pytest.fixture
def auth_service(mock_redis):
    service = AuthService()
    service.redis_client = mock_redis
    return service


def test_update_user_profile_data_ignores_phone_changes(auth_service, db, sample_user):
    original_phone = sample_user.phone

    result = auth_service.update_user_profile_data(
        sample_user.id,
        {"first_name": "Updated", "phone": "+998991112233"},
    )
    db.session.refresh(sample_user)

    assert result["phone_update_attempted"] is True
    assert sample_user.first_name == "Updated"
    assert sample_user.phone == original_phone


def test_set_default_user_address_unsets_existing_default(auth_service, db, sample_user):
    first = auth_service.add_user_address(
        sample_user.id,
        {"title": "Home", "full_address": "A", "is_default": True},
    )
    second = auth_service.add_user_address(
        sample_user.id,
        {"title": "Work", "full_address": "B", "is_default": False},
    )

    updated_default = auth_service.set_default_user_address(sample_user.id, second.id)

    db.session.refresh(first)
    db.session.refresh(second)
    assert updated_default.id == second.id
    assert first.is_default is False
    assert second.is_default is True


def test_delete_user_address_blocks_default_when_other_addresses_exist(auth_service, db, sample_user):
    first = auth_service.add_user_address(
        sample_user.id,
        {"title": "Home", "full_address": "A", "is_default": True},
    )
    auth_service.add_user_address(
        sample_user.id,
        {"title": "Work", "full_address": "B", "is_default": False},
    )

    with pytest.raises(ValidationError):
        auth_service.delete_user_address(sample_user.id, first.id)


def test_delete_user_address_blocks_subscription_linked_address(auth_service, db, sample_user):
    auth_service.add_user_address(
        sample_user.id,
        {"title": "Home", "full_address": "A", "is_default": True},
    )
    linked = auth_service.add_user_address(
        sample_user.id,
        {"title": "Work", "full_address": "B", "is_default": False},
    )

    subscription = Subscription(
        subscription_number='SUB-AUTH-SERVICE-ADDR-1',
        user_id=sample_user.id,
        name='Auth service address lock',
        billing_cycle=SubscriptionFrequency.WEEKLY,
        billing_amount=Decimal('20000.00'),
        next_billing_date=datetime.now(UTC) + timedelta(days=7),
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=linked.id,
        start_date=datetime.now(UTC),
        payment_method=PaymentMethod.CARD,
    )
    db.session.add(subscription)
    db.session.commit()

    with pytest.raises(ValidationError):
        auth_service.delete_user_address(sample_user.id, linked.id)


def test_check_phone_availability_for_telegram_returns_masked_existing_user(auth_service, db, sample_user):
    # The caller signed up in the bot before its phone step, as registration always does first
    db.session.add(
        User(
            telegram_id="12345",
            email="telegram_12345@bot.internal",
            password_hash="telegram_user",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            registration_source="telegram",
        )
    )
    db.session.commit()

    result = auth_service.check_phone_availability_for_telegram(
        phone=sample_user.phone,
        telegram_id="12345",
    )

    assert result["available"] is False
    assert "existing_user_masked" in result
    assert result["existing_user_masked"]["name"].startswith("T")


def test_check_phone_availability_for_telegram_hides_existing_user_for_same_telegram_id(
    auth_service, db, sample_user
):
    sample_user.telegram_id = "12345"
    db.session.commit()

    result = auth_service.check_phone_availability_for_telegram(
        phone=sample_user.phone,
        telegram_id="12345",
    )

    assert result["available"] is True
    assert result["can_link"] is False
    assert result["existing_user_masked"] is None


def test_send_phone_link_otp_rejects_when_phone_already_linked(auth_service, db, sample_user):
    sample_user.telegram_id = "already-linked"
    telegram_user = User(
        telegram_id="777123000",
        email="telegram_777123000@bot.internal",
        phone=None,
        password_hash=hash_password("TempPassword123!"),
        first_name="Telegram",
        last_name="User",
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source="telegram",
    )
    db.session.add(telegram_user)
    db.session.commit()

    with pytest.raises(ConflictError):
        auth_service.send_phone_link_otp(sample_user.phone, "777123000")


def test_verify_phone_link_and_merge_accounts_returns_tokens(db, sample_user):
    telegram_user = User(
        telegram_id="555666777",
        email="telegram_555666777@bot.internal",
        phone=None,
        password_hash=hash_password("TempPassword123!"),
        first_name="Telegram",
        last_name="User",
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source="telegram",
    )
    db.session.add(telegram_user)
    db.session.commit()

    service = AuthService()
    service.redis_client = _InMemoryRedis()
    otp = _stage_link(service, sample_user, telegram_user)

    with (
        patch(
            "business_app.services.cross_platform_sync_service.cross_platform_sync_service.auto_link_accounts",
            return_value={"success": True},
        ),
        patch.object(
            service,
            "_generate_tokens",
            return_value={"access_token": "a", "refresh_token": "r"},
        ),
    ):
        result = service.verify_phone_link_and_merge_accounts("555666777", otp)

    assert result["linked"] is True
    assert result["tokens"]["access_token"] == "a"


class _InMemoryRedis:
    """Minimal Redis stand-in that honours get/setex/delete/ttl semantics so a
    test can observe whether a key was really consumed (a MagicMock can't)."""

    def __init__(self):
        self._v = {}
        self._ttl = {}

    @staticmethod
    def _b(v):
        if isinstance(v, bytes):
            return v
        return str(v).encode()

    def get(self, k):
        return self._v.get(k)

    def setex(self, k, ttl, v):
        self._v[k] = self._b(v)
        self._ttl[k] = int(ttl)
        return True

    def set(self, k, v):
        self._v[k] = self._b(v)
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._v:
                del self._v[k]
                n += 1
            self._ttl.pop(k, None)
        return n

    def ttl(self, k):
        return self._ttl.get(k, -2)

    def exists(self, k):
        return 1 if k in self._v else 0

    def incr(self, k):
        cur = int(self._v.get(k, b"0")) + 1
        self._v[k] = self._b(cur)
        return cur

    def expire(self, k, ttl):
        if k in self._v:
            self._ttl[k] = int(ttl)
        return True


def _stage_link(service, web_user, telegram_user):
    """Stage a link through the real send path; returns the code the web account's phone received."""
    sent = []

    def _sms_task(user_id, otp_code, phone_number=None):
        sent.append((phone_number, otp_code))

    with patch("business_app.services.auth_service.send_verification_sms_task", side_effect=_sms_task):
        service.send_phone_link_otp(web_user.phone, telegram_user.telegram_id)

    [(phone, otp)] = sent
    assert phone == web_user.phone
    return otp


def test_link_flow_preserves_otp_when_merge_fails(db, sample_user):
    """Prod incident 2026-06-23: the OTP was consumed by verify_phone BEFORE the
    merge ran, so a merge failure also burned the (correct) code — the
    customer's retry was then rejected as 'invalid OTP'. The valid code must
    survive a failed merge so a retry with it works."""
    telegram_user = User(
        telegram_id="909090909",
        email="telegram_909090909@bot.internal",
        phone=None,
        password_hash=hash_password("TempPassword123!"),
        first_name="Telegram",
        last_name="User",
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        registration_source="telegram",
    )
    db.session.add(telegram_user)
    db.session.commit()

    service = AuthService()
    service.redis_client = _InMemoryRedis()
    otp = _stage_link(service, sample_user, telegram_user)

    seed_translations(db, ["api.auth.accounts_linking_failed"])
    with patch(
        "business_app.services.cross_platform_sync_service.cross_platform_sync_service.auto_link_accounts",
        return_value={"success": False, "error": "transient merge failure"},
    ):
        with pytest.raises(ValidationError) as failed:
            service.verify_phone_link_and_merge_accounts(telegram_user.telegram_id, otp)
    # The merge's reason stays in the log; the customer gets the seeded message
    assert failed.value.message in BACKEND_TRANSLATIONS["api.auth.accounts_linking_failed"].values()

    with (
        patch(
            "business_app.services.cross_platform_sync_service.cross_platform_sync_service.auto_link_accounts",
            return_value={"success": True},
        ),
        patch.object(service, "_generate_tokens", return_value={"access_token": "a", "refresh_token": "r"}),
    ):
        result = service.verify_phone_link_and_merge_accounts(telegram_user.telegram_id, otp)

    assert result["linked"] is True
    assert result["user"].id == sample_user.id
