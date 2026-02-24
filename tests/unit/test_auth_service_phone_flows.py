"""Unit tests for AuthService phone-registration and reset edge cases."""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from business_app.models.user import User
from business_app.services.auth_service import AuthService
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password


class InMemoryRedis:
    """Small stateful Redis double for OTP and lockout flows."""

    def __init__(self):
        self._store = {}
        self._ttl = {}

    def setex(self, key, ttl, value):
        if isinstance(value, bytes):
            encoded = value
        else:
            encoded = str(value).encode()
        self._store[key] = encoded
        self._ttl[key] = int(ttl)
        return True

    def get(self, key):
        return self._store.get(key)

    def ttl(self, key):
        if key in self._store:
            return self._ttl.get(key, -1)
        return -2

    def exists(self, key):
        return 1 if key in self._store else 0

    def incr(self, key):
        current = int(self._store.get(key, b"0").decode())
        current += 1
        self._store[key] = str(current).encode()
        return current

    def expire(self, key, ttl):
        if key in self._store:
            self._ttl[key] = int(ttl)
            return True
        return False

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self._store:
                deleted += 1
                self._store.pop(key, None)
                self._ttl.pop(key, None)
        return deleted


@pytest.fixture
def stateful_redis():
    return InMemoryRedis()


@pytest.fixture
def auth_service_with_stateful_redis(app, stateful_redis):
    with app.app_context():
        service = AuthService()
        service.redis_client = stateful_redis
        return service


def _phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


@pytest.mark.unit
@pytest.mark.auth
class TestAuthServicePhoneFlows:
    def test_initiate_phone_registration_stores_otp_and_queues_sms(
        self, app, db, auth_service_with_stateful_redis, stateful_redis
    ):
        phone = "+998901112233"

        with (
            app.app_context(),
            patch("business_app.tasks.notification_tasks.send_registration_otp_task.delay") as mock_delay,
        ):
            result = auth_service_with_stateful_redis.initiate_phone_registration(phone, language="uz")

        phone_hash = _phone_hash(phone)
        otp_key = f"phone_reg_otp:{phone_hash}"
        mapping_key = f"phone_reg_mapping:{phone_hash}"
        cooldown_key = f"phone_otp_cooldown:{phone_hash}"

        assert result["phone_masked"] == "+998***2233"
        assert result["expires_in"] == auth_service_with_stateful_redis.PHONE_OTP_EXPIRY
        assert result["resend_available_in"] == auth_service_with_stateful_redis.PHONE_OTP_RESEND_COOLDOWN
        assert stateful_redis.get(otp_key) is not None
        assert stateful_redis.get(mapping_key) == phone.encode()
        assert stateful_redis.ttl(cooldown_key) == auth_service_with_stateful_redis.PHONE_OTP_RESEND_COOLDOWN
        mock_delay.assert_called_once()

    def test_initiate_phone_registration_rejects_existing_phone(
        self, app, db, auth_service_with_stateful_redis
    ):
        phone = "+998901110000"
        existing = User(
            email="existing-phone@example.com",
            phone=phone,
            password_hash=hash_password("StrongPass123!"),
            first_name="Existing",
            registration_source="web",
        )
        db.session.add(existing)
        db.session.commit()

        with app.app_context():
            with pytest.raises(ConflictError):
                auth_service_with_stateful_redis.initiate_phone_registration(phone, language="uz")

    def test_initiate_phone_registration_enforces_cooldown(
        self, app, db, auth_service_with_stateful_redis, stateful_redis
    ):
        phone = "+998901114455"
        phone_hash = _phone_hash(phone)
        stateful_redis.setex(f"phone_otp_cooldown:{phone_hash}", 45, "1")

        with app.app_context():
            with pytest.raises(ValidationError) as exc:
                auth_service_with_stateful_redis.initiate_phone_registration(phone, language="uz")

        assert exc.value.error_code == "RESEND_COOLDOWN"

    def test_complete_phone_registration_invalid_otp_increments_attempts(
        self, app, auth_service_with_stateful_redis, stateful_redis
    ):
        phone = "+998901115566"
        phone_hash = _phone_hash(phone)
        stateful_redis.setex(f"phone_reg_otp:{phone_hash}", 180, "654321")

        with app.app_context():
            with pytest.raises(ValidationError) as exc:
                auth_service_with_stateful_redis.complete_phone_registration(
                    phone=phone,
                    otp_code="111111",
                    first_name="Phone",
                    last_name="User",
                    password="StrongPass123!",
                )

        assert exc.value.error_code == "INVALID_OTP"
        assert stateful_redis.get(f"phone_otp_attempts:{phone_hash}") == b"1"

    def test_complete_phone_registration_locks_after_max_attempts(
        self, app, auth_service_with_stateful_redis, stateful_redis
    ):
        phone = "+998901116677"
        phone_hash = _phone_hash(phone)
        stateful_redis.setex(f"phone_reg_otp:{phone_hash}", 180, "123456")
        stateful_redis.setex(f"phone_otp_attempts:{phone_hash}", 600, "4")

        with app.app_context():
            with pytest.raises(ValidationError) as exc:
                auth_service_with_stateful_redis.complete_phone_registration(
                    phone=phone,
                    otp_code="000000",
                    first_name="Lockout",
                    last_name="User",
                    password="StrongPass123!",
                )

        assert exc.value.error_code == "OTP_MAX_ATTEMPTS"
        assert stateful_redis.exists(f"phone_otp_lockout:{phone_hash}") == 1
        assert stateful_redis.get(f"phone_reg_otp:{phone_hash}") is None

    def test_complete_phone_registration_success_creates_user_and_cleans_keys(
        self, app, db, auth_service_with_stateful_redis, stateful_redis
    ):
        phone = "+998901117788"
        phone_hash = _phone_hash(phone)
        stateful_redis.setex(f"phone_reg_otp:{phone_hash}", 180, "123123")
        stateful_redis.setex(f"phone_reg_lang:{phone_hash}", 180, "ru")
        stateful_redis.setex(f"phone_reg_mapping:{phone_hash}", 180, phone)
        stateful_redis.setex(f"phone_otp_cooldown:{phone_hash}", 60, "1")

        with (
            app.app_context(),
            app.test_request_context("/api/v1/auth/phone/complete", method="POST"),
            patch.object(
                auth_service_with_stateful_redis,
                "_generate_tokens",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ),
            patch.object(auth_service_with_stateful_redis, "_create_user_session"),
            patch("business_app.tasks.notification_tasks.send_welcome_sms_task.delay") as mock_welcome_delay,
        ):
            user, tokens = auth_service_with_stateful_redis.complete_phone_registration(
                phone=phone,
                otp_code="123123",
                first_name="Phone",
                last_name="Registered",
                password="StrongPass123!",
            )

        persisted_user = User.query.get(user.id)
        assert tokens["access_token"] == "access"
        assert persisted_user is not None
        assert persisted_user.phone == phone
        assert persisted_user.registration_method == "phone"
        assert persisted_user.is_verified is True
        assert persisted_user.phone_verified_at is not None
        assert stateful_redis.get(f"phone_reg_mapping:{phone_hash}") is None
        assert stateful_redis.get(f"phone_reg_lang:{phone_hash}") is None
        assert stateful_redis.get(f"phone_otp_cooldown:{phone_hash}") is None
        mock_welcome_delay.assert_called_once_with(user.id)

    def test_request_password_reset_for_telegram_user_with_verified_phone_uses_sms(
        self, app, db, auth_service_with_stateful_redis
    ):
        user = User(
            email="telegram_777@bot.internal",
            phone="+998901118899",
            password_hash=hash_password("StrongPass123!"),
            first_name="Telegram",
            registration_source="telegram",
            phone_verified_at=datetime.now(timezone.utc),
        )
        db.session.add(user)
        db.session.commit()

        with (
            app.app_context(),
            patch.object(auth_service_with_stateful_redis, "_send_phone_password_reset") as mock_sms_reset,
        ):
            result = auth_service_with_stateful_redis.request_password_reset(user.email)

        assert result is True
        mock_sms_reset.assert_called_once()
        assert mock_sms_reset.call_args[0][0].id == user.id

    def test_request_password_reset_for_telegram_user_without_verified_phone_noop(
        self, app, db, auth_service_with_stateful_redis
    ):
        user = User(
            email="telegram_888@bot.internal",
            phone="+998901119900",
            password_hash=hash_password("StrongPass123!"),
            first_name="Telegram",
            registration_source="telegram",
            phone_verified_at=None,
        )
        db.session.add(user)
        db.session.commit()

        with (
            app.app_context(),
            patch.object(auth_service_with_stateful_redis, "_send_phone_password_reset") as mock_sms_reset,
            patch.object(auth_service_with_stateful_redis, "_generate_verification_token") as mock_token,
        ):
            result = auth_service_with_stateful_redis.request_password_reset(user.email)

        assert result is True
        mock_sms_reset.assert_not_called()
        mock_token.assert_not_called()

    def test_send_verification_sms_updates_phone_when_allowed(
        self, app, db, auth_service_with_stateful_redis, sample_user, stateful_redis
    ):
        new_phone = "+998909991122"

        with (
            app.app_context(),
            patch("business_app.services.auth_service.send_verification_sms_task") as mock_send_sms_task,
        ):
            success = auth_service_with_stateful_redis.send_verification_sms(
                user_id=sample_user.id,
                phone=new_phone,
                update_phone=True,
            )

        db.session.refresh(sample_user)
        assert success is True
        assert sample_user.phone == new_phone
        assert stateful_redis.get(f"sms_verification:{sample_user.id}") is not None
        mock_send_sms_task.assert_called_once()

    def test_send_verification_sms_does_not_update_phone_when_forbidden(
        self, app, db, auth_service_with_stateful_redis, sample_user
    ):
        original_phone = sample_user.phone
        alternate_phone = "+998907770011"

        with (
            app.app_context(),
            patch("business_app.services.auth_service.send_verification_sms_task"),
        ):
            success = auth_service_with_stateful_redis.send_verification_sms(
                user_id=sample_user.id,
                phone=alternate_phone,
                update_phone=False,
            )

        db.session.refresh(sample_user)
        assert success is True
        assert sample_user.phone == original_phone
