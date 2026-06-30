"""Registration / phone-collection flow tests for the telegram bot.

Covers the P1 bot-side fixes:
- Task 5: response-nesting fix in account linking (merged tokens cached, masked
  phone shown instead of leaking the full number).
- Task 6: stranded NULL-phone user re-prompted for phone on /start.
- Task 7: transient API error during phone check re-prompts and does NOT write
  the phone or end registration.
- Task 8 (already implemented elsewhere): invalid contact number re-prompts.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ConversationHandler

from handlers import profile as profile_module
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


@pytest.mark.unit
@pytest.mark.anyio
class TestLinkAccountResponseNesting:
    """Task 5: backend wraps payloads as {success, message, data:{...}}."""

    async def test_link_account_otp_caches_merged_tokens_and_uses_nested_user(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1301)
        update.message.text = "123456"
        context = make_context()
        context.user_data["pending_link_phone"] = "+998901112233"

        token_manager = SimpleNamespace(store_tokens=AsyncMock())
        context.bot_data["token_manager"] = token_manager

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def link_phone_verify(self, *_args, **_kwargs):
                return _resp(
                    success=True,
                    data={
                        "data": {
                            "tokens": {
                                "access_token": "acc",
                                "refresh_token": "ref",
                                "expires_in": 7200,
                            },
                            "user": {"first_name": "Jamshid"},
                        }
                    },
                )

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}:{kw}")
        monkeypatch.setattr(profile_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.link_account_otp(update, context)

        assert state == ConversationHandler.END
        # Merged-account tokens must be cached (previously never were).
        token_manager.store_tokens.assert_awaited_once_with(1301, "acc", "ref", 7200)
        # Success message must read the nested user's first_name.
        success_call = update.message.reply_text.await_args
        assert "Jamshid" in success_call.args[0]
        assert "pending_link_phone" not in context.user_data

    async def test_link_account_confirm_shows_masked_phone_from_nested_payload(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1302)
        update.callback_query = DummyCallbackQuery(data="link_yes")
        update.effective_chat = SimpleNamespace(id=1302)
        context = make_context()
        context.user_data["pending_link_phone"] = "+998901112233"

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def link_phone_send_otp(self, *_args, **_kwargs):
                return _resp(
                    success=True,
                    data={"data": {"phone_masked": "+998***2233"}},
                )

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}:{kw}")
        monkeypatch.setattr(profile_module.otp_rate_limiter, "allow_otp_request", AsyncMock(return_value=True))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.link_account_confirm(update, context)

        assert state == profile_module.LINK_ACCOUNT_OTP
        prompt_call = update.callback_query.edit_message_text.await_args
        # Masked phone must be used; the full number must never appear.
        assert "+998***2233" in prompt_call.args[0]
        assert "+998901112233" not in prompt_call.args[0]


@pytest.mark.unit
@pytest.mark.anyio
class TestStrandedUserReprompt:
    """Task 6: existing row with no phone resumes phone collection."""

    async def test_existing_user_without_phone_routes_to_phone_collection(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1401)
        context = make_context()
        cleanup_mock = AsyncMock(return_value=True)

        # Existing row, phone never captured.
        user_repo = SimpleNamespace(
            get_user_by_telegram_id=AsyncMock(return_value={"id": 1, "phone": None, "preferred_language": "uz"})
        )
        monkeypatch.setattr(profile_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")
        monkeypatch.setattr(profile_module, "maybe_remove_stale_reply_keyboard", cleanup_mock)

        state = await handler.start_registration_new(update, context)

        assert state == profile_module.PHONE
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.registration.share_contact_prompt:uz",
            reply_markup="phone-kbd",
        )

    async def test_existing_user_with_phone_shows_main_menu(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1402)
        context = make_context()
        cleanup_mock = AsyncMock(return_value=True)

        user_repo = SimpleNamespace(
            get_user_by_telegram_id=AsyncMock(return_value={"id": 1, "phone": "+998901112233"})
        )
        monkeypatch.setattr(profile_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(profile_module, "maybe_remove_stale_reply_keyboard", cleanup_mock)

        state = await handler.start_registration_new(update, context)

        assert state == ConversationHandler.END
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.welcome:en",
            reply_markup="menu-kbd",
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestPhoneReceivedApiError:
    """Task 7: transient API error must re-prompt, not write phone / end."""

    async def test_api_error_reprompts_and_does_not_write_phone(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(set_user_phone_verified=AsyncMock())
        update = DummyUpdate(user_id=1501)
        update.message.contact = SimpleNamespace(user_id=1501, phone_number="+998901112233")
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def check_phone_availability(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda phone: phone)
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.phone_received(update, context)

        assert state == profile_module.PHONE
        handler.user_repo.set_user_phone_verified.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "telegram.phone.verify_unavailable_now:en",
            reply_markup="phone-kbd",
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestPhoneReceivedContactValidation:
    """Task 8 (already implemented): invalid contact number re-prompts."""

    async def test_invalid_contact_number_reprompts_without_calling_api(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(set_user_phone_verified=AsyncMock())
        update = DummyUpdate(user_id=1601)
        update.message.contact = SimpleNamespace(user_id=1601, phone_number="not-a-phone")
        context = make_context()

        api_called = {"hit": False}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def check_phone_availability(self, *_args, **_kwargs):
                api_called["hit"] = True
                return _resp(success=True, data={"data": {"available": True}})

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        # Invalid number normalizes to None.
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda phone: None)
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.phone_received(update, context)

        assert state == profile_module.PHONE
        assert api_called["hit"] is False
        handler.user_repo.set_user_phone_verified.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "telegram.phone.invalid_format:en",
            reply_markup="phone-kbd",
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestRegistrationOtpInsideConversation:
    """Task 12: registration OTP capture moved INSIDE the conversation.

    The text-phone path now transitions to REGISTER_OTP (instead of ending the
    conversation and relying on the global awaiting_otp catch-all), and a new
    in-conversation handler verifies the code via the registration endpoint
    (verify_phone_otp), NOT the account-merge endpoint (link_phone_verify).
    """

    async def test_register_otp_state_constant_is_additive(self):
        # REGISTER_OTP must exist and be the LAST appended state so no existing
        # constant's integer value changes.
        assert hasattr(profile_module, "REGISTER_OTP")
        assert profile_module.REGISTER_OTP == profile_module.LINK_ACCOUNT_OTP + 1

    async def test_phone_text_available_sends_otp_and_returns_register_otp_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1701)
        update.message.text = "+998901112233"
        update.update_id = 555
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def check_phone_availability(self, *_args, **_kwargs):
                return _resp(success=True, data={"data": {"available": True}})

            async def send_phone_verification(self, *_args, **_kwargs):
                return _resp(success=True, data={"data": {"phone_masked": "+998***2233"}})

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}:{kw}")
        monkeypatch.setattr(profile_module, "validate_phone_number", AsyncMock(return_value=True))
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda phone: phone)
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="user-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.phone_text_received(update, context)

        # Now stays inside the conversation instead of ending it.
        assert state == profile_module.REGISTER_OTP
        # Registration path must NOT set the global awaiting_otp flag anymore.
        assert "awaiting_otp" not in context.user_data
        assert "otp_prompted_update_id" not in context.user_data
        # Pending phone is stashed for the in-conversation OTP handler.
        assert context.user_data["pending_phone_verification"] == "+998901112233"

    async def test_register_otp_received_valid_calls_verify_phone_otp_not_link(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1702)
        update.message.text = "123456"
        context = make_context()
        context.user_data["pending_phone_verification"] = "+998901112233"

        calls = {"verify_phone_otp": 0, "link_phone_verify": 0}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def verify_phone_otp(self, *_args, **_kwargs):
                calls["verify_phone_otp"] += 1
                return _resp(success=True, data={"data": {}})

            async def link_phone_verify(self, *_args, **_kwargs):
                calls["link_phone_verify"] += 1
                return _resp(success=True)

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "main_menu_for", AsyncMock(return_value="menu-kbd"))
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="user-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.register_otp_received(update, context)

        assert state == ConversationHandler.END
        # Correct registration endpoint used; account-merge endpoint never touched.
        assert calls["verify_phone_otp"] == 1
        assert calls["link_phone_verify"] == 0
        # Pending phone cleared and a main-menu keyboard shown on success.
        assert "pending_phone_verification" not in context.user_data
        success_call = update.message.reply_text.await_args
        assert success_call.kwargs.get("reply_markup") == "menu-kbd"

    async def test_register_otp_received_invalid_format_reprompts_without_verify(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1703)
        update.message.text = "12ab"
        context = make_context()
        context.user_data["pending_phone_verification"] = "+998901112233"

        verify_called = {"hit": False}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def verify_phone_otp(self, *_args, **_kwargs):
                verify_called["hit"] = True
                return _resp(success=True)

            async def link_phone_verify(self, *_args, **_kwargs):
                verify_called["hit"] = True
                return _resp(success=True)

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="user-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.register_otp_received(update, context)

        assert state == profile_module.REGISTER_OTP
        assert verify_called["hit"] is False
        # Pending phone preserved so the user can retry.
        assert context.user_data["pending_phone_verification"] == "+998901112233"

    async def test_register_otp_received_failed_verify_reprompts(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1704)
        update.message.text = "123456"
        context = make_context()
        context.user_data["pending_phone_verification"] = "+998901112233"

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def verify_phone_otp(self, *_args, **_kwargs):
                return _resp(success=False, error="invalid code")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="user-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.register_otp_received(update, context)

        # Generic failure re-prompts so the user can re-enter the code.
        assert state == profile_module.REGISTER_OTP

    async def test_register_otp_received_expired_verify_ends(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1705)
        update.message.text = "123456"
        context = make_context()
        context.user_data["pending_phone_verification"] = "+998901112233"

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def verify_phone_otp(self, *_args, **_kwargs):
                return _resp(success=False, error="OTP expired")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **kw: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="user-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.register_otp_received(update, context)

        assert state == ConversationHandler.END
