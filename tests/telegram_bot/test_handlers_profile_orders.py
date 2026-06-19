"""Handler tests for profile and orders telegram bot flows."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest
from telegram.ext import ConversationHandler

from handlers import orders as orders_module
from handlers import profile as profile_module
from handlers.profile import ADDRESS_LOCATION, PHONE_VERIFY_NAME, PHONE_VERIFY_PHONE
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


@pytest.mark.unit
@pytest.mark.anyio
class TestProfileHandlerFlows:
    def test_capture_referral_arg_stores_code_from_deep_link(self):
        # t.me/<bot>?start=ref_CODE arrives as context.args == ["ref_CODE"].
        ctx = SimpleNamespace(args=["ref_REFMM8UQU"], user_data={})
        profile_module.ProfileHandlers._capture_referral_arg(ctx)
        assert ctx.user_data["referral_code"] == "REFMM8UQU"

    def test_capture_referral_arg_ignores_non_referral_args(self):
        for args in (["somepromo"], [], ["ref_"]):
            ctx = SimpleNamespace(args=args, user_data={})
            profile_module.ProfileHandlers._capture_referral_arg(ctx)
            assert "referral_code" not in ctx.user_data

    async def test_add_phone_number_starts_conversation(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="add_phone_number")
        context = make_context()

        monkeypatch.setattr(profile_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")

        state = await handler.add_phone_number(update, context)

        assert state == PHONE_VERIFY_PHONE
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            "telegram.phone.send_code_prompt:en",
            parse_mode="Markdown",
            reply_markup="phone-kbd",
        )

    async def test_start_registration_new_existing_user_cleans_stale_reply_keyboard(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=707)
        context = make_context()
        cleanup_mock = AsyncMock(return_value=True)

        user_repo = SimpleNamespace(
            get_user_by_telegram_id=AsyncMock(return_value={"id": 1, "phone": "+998901112233"})
        )
        monkeypatch.setattr(profile_module, "BotUserRepository", lambda _db: user_repo)
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")
        monkeypatch.setattr(profile_module, "maybe_remove_stale_reply_keyboard", cleanup_mock)

        state = await handler.start_registration_new(update, context)

        assert state == ConversationHandler.END
        cleanup_mock.assert_awaited_once_with(update, context)
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.welcome:en",
            reply_markup="menu-kbd",
        )

    async def test_phone_verify_contact_rejects_other_users_contact(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=777)
        update.message.contact = SimpleNamespace(user_id=555, phone_number="+998901112233")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")

        state = await handler.phone_verify_contact_received(update, context)

        assert state == PHONE_VERIFY_PHONE
        update.message.reply_text.assert_awaited_once_with(
            "telegram.phone.share_own_phone:en",
            reply_markup="phone-kbd",
        )

    async def test_phone_verify_contact_accepts_and_moves_to_name_step(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(set_user_phone_verified=AsyncMock())
        update = DummyUpdate(user_id=777)
        update.message.contact = SimpleNamespace(user_id=777, phone_number="+998901112233")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda p: p)
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            profile_module,
            "api_client",
            FakeAPIClientContext(update_user_profile=_resp(success=True)),
        )

        state = await handler.phone_verify_contact_received(update, context)

        assert state == PHONE_VERIFY_NAME
        assert context.user_data["pending_phone"] == "+998901112233"
        handler.user_repo.set_user_phone_verified.assert_awaited_once_with(777, "+998901112233")
        assert update.message.reply_text.await_count == 2

    async def test_phone_verify_text_accepts_valid_phone_removes_keyboard_and_moves_to_name_step(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(set_user_phone=AsyncMock())
        update = DummyUpdate(user_id=778)
        update.message.text = "+998901112233"
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "validate_phone_number", AsyncMock(return_value=True))
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda p: p)
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            profile_module,
            "api_client",
            FakeAPIClientContext(update_user_profile=_resp(success=True)),
        )

        state = await handler.phone_verify_text_received(update, context)

        assert state == PHONE_VERIFY_NAME
        assert context.user_data["pending_phone"] == "+998901112233"
        handler.user_repo.set_user_phone.assert_awaited_once_with(778, "+998901112233")
        assert update.message.reply_text.await_count == 2
        first_call = update.message.reply_text.await_args_list[0]
        second_call = update.message.reply_text.await_args_list[1]
        assert first_call.args == ("telegram.phone.phone_accepted:en",)
        assert isinstance(first_call.kwargs["reply_markup"], profile_module.ReplyKeyboardRemove)
        assert second_call.args == ("telegram.enter_name:en",)

    async def test_phone_verify_text_rejects_invalid_phone_and_keeps_phone_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=779)
        update.message.text = "not-a-phone"
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "validate_phone_number", AsyncMock(return_value=False))
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")

        state = await handler.phone_verify_text_received(update, context)

        assert state == PHONE_VERIFY_PHONE
        update.message.reply_text.assert_awaited_once_with(
            "telegram.phone.invalid_format:en",
            reply_markup="phone-kbd",
        )

    async def test_phone_verify_name_validates_input(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=888)
        update.message.text = "1"
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        state = await handler.phone_verify_name_received(update, context)

        assert state == PHONE_VERIFY_NAME
        update.message.reply_text.assert_awaited_once_with("telegram.name.too_short:en")

    async def test_phone_verify_name_success_updates_profile_and_ends(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=999)
        update.message.text = "John Doe"
        context = make_context()
        context.user_data["pending_phone"] = "+998900000000"

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            profile_module,
            "api_client",
            FakeAPIClientContext(update_user_profile=_resp(success=True)),
        )
        monkeypatch.setattr(profile_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        state = await handler.phone_verify_name_received(update, context)

        assert state == ConversationHandler.END
        assert "pending_phone" not in context.user_data
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.profile_updated:en",
            reply_markup="menu-kbd",
        )

    async def test_phone_received_available_removes_phone_keyboard_before_main_menu(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(set_user_phone_verified=AsyncMock())
        update = DummyUpdate(user_id=1101)
        update.message.contact = SimpleNamespace(user_id=1101, phone_number="+998901112233")
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def check_phone_availability(self, *_args, **_kwargs):
                return _resp(success=True, data={"data": {"available": True}})

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda phone: phone)
        monkeypatch.setattr(profile_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.phone_received(update, context)

        assert state == ConversationHandler.END
        handler.user_repo.set_user_phone_verified.assert_awaited_once_with(1101, "+998901112233")
        assert update.message.reply_text.await_count == 2
        first_call = update.message.reply_text.await_args_list[0]
        second_call = update.message.reply_text.await_args_list[1]
        assert first_call.args == ("telegram.phone.phone_accepted:en",)
        assert isinstance(first_call.kwargs["reply_markup"], profile_module.ReplyKeyboardRemove)
        assert second_call.kwargs == {
            "text": "telegram.registration_complete:en",
            "reply_markup": "menu-kbd",
        }

    async def test_phone_received_linkable_removes_phone_keyboard_before_link_prompt(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1102)
        update.message.contact = SimpleNamespace(user_id=1102, phone_number="+998901112233")
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def check_phone_availability(self, *_args, **_kwargs):
                return _resp(
                    success=True,
                    data={
                        "data": {
                            "available": False,
                            "can_link": True,
                            "existing_user_masked": {"name": "J***"},
                        }
                    },
                )

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "normalize_phone_number", lambda phone: phone)
        monkeypatch.setattr(profile_module.KeyboardBuilder, "build_inline_keyboard", lambda _buttons: "inline-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.phone_received(update, context)

        assert state == profile_module.LINK_ACCOUNT_CONFIRM
        assert context.user_data["pending_link_phone"] == "+998901112233"
        assert update.message.reply_text.await_count == 2
        first_call = update.message.reply_text.await_args_list[0]
        second_call = update.message.reply_text.await_args_list[1]
        assert first_call.args == ("telegram.phone.phone_accepted:en",)
        assert isinstance(first_call.kwargs["reply_markup"], profile_module.ReplyKeyboardRemove)
        assert second_call.kwargs == {
            "text": "telegram.phone.already_registered_link_prompt:en",
            "reply_markup": "inline-kbd",
        }

    async def test_cancel_phone_verification_clears_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=101)
        context = make_context()
        context.user_data["pending_phone"] = "+998"

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        state = await handler.cancel_phone_verification(update, context)

        assert state == ConversationHandler.END
        assert "pending_phone" not in context.user_data
        assert update.message.reply_text.await_count == 2
        first_call = update.message.reply_text.await_args_list[0]
        second_call = update.message.reply_text.await_args_list[1]
        assert first_call.kwargs["text"] == "telegram.action_cancelled_short:en"
        assert isinstance(first_call.kwargs["reply_markup"], profile_module.ReplyKeyboardRemove)
        assert second_call.kwargs == {
            "text": "telegram.action_cancelled:en",
            "reply_markup": "menu-kbd",
        }

    async def test_cancel_registration_removes_reply_keyboard_before_main_menu(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1010)
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        state = await handler.cancel_registration(update, context)

        assert state == ConversationHandler.END
        assert update.message.reply_text.await_count == 2
        first_call = update.message.reply_text.await_args_list[0]
        second_call = update.message.reply_text.await_args_list[1]
        assert first_call.kwargs["text"] == "telegram.action_cancelled_short:en"
        assert isinstance(first_call.kwargs["reply_markup"], profile_module.ReplyKeyboardRemove)
        assert second_call.kwargs == {
            "text": "telegram.action_cancelled:en",
            "reply_markup": "menu-kbd",
        }

    async def test_link_account_confirm_link_yes_api_failure_sends_clean_retry_prompt(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1201)
        update.callback_query = DummyCallbackQuery(data="link_yes")
        update.effective_chat = SimpleNamespace(id=1201)
        context = make_context()
        context.user_data["pending_link_phone"] = "+998901112233"

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def link_phone_send_otp(self, *_args, **_kwargs):
                return _resp(success=False, error="sms down")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.otp_rate_limiter, "allow_otp_request", AsyncMock(return_value=True))
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.link_account_confirm(update, context)

        assert state == profile_module.PHONE
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once_with(
            "telegram.phone.verification_code_send_failed_retry_or_different:en",
            reply_markup=None,
        )
        context.bot.send_message.assert_awaited_once_with(
            chat_id=1201,
            text="telegram.phone.share_phone_using_button:en",
            reply_markup="phone-kbd",
        )

    async def test_link_account_confirm_link_yes_api_exception_sends_clean_retry_prompt(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=1202)
        update.callback_query = DummyCallbackQuery(data="link_yes")
        update.effective_chat = SimpleNamespace(id=1202)
        context = make_context()
        context.user_data["pending_link_phone"] = "+998901112233"

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def link_phone_send_otp(self, *_args, **_kwargs):
                raise RuntimeError("network")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.otp_rate_limiter, "allow_otp_request", AsyncMock(return_value=True))
        monkeypatch.setattr(profile_module.ProfileKeyboards, "phone_request", lambda _lang: "phone-kbd")
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        state = await handler.link_account_confirm(update, context)

        assert state == profile_module.PHONE
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once_with(
            "telegram.phone.verification_code_send_failed_generic:en",
            reply_markup=None,
        )
        context.bot.send_message.assert_awaited_once_with(
            chat_id=1202,
            text="telegram.phone.share_phone_using_button:en",
            reply_markup="phone-kbd",
        )

    async def test_notification_settings_renders_toggle_screen(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=303)
        update.callback_query = DummyCallbackQuery(data="notification_settings")
        context = make_context()

        monkeypatch.setattr(profile_module, "user_middleware", AsyncMock(return_value={"id": 303}))
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(
            profile_module.ProfileKeyboards,
            "notification_settings",
            lambda language, delivery_telegram_status_updates_enabled: (
                f"kbd:{language}:{delivery_telegram_status_updates_enabled}"
            ),
        )
        monkeypatch.setattr(
            profile_module,
            "api_client",
            FakeAPIClientContext(
                get_notification_preferences=_resp(
                    success=True,
                    data={
                        "data": {
                            "preferences": {
                                "delivery_telegram_status_updates_enabled": False,
                            }
                        }
                    },
                )
            ),
        )

        await handler.notification_settings(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        call_kwargs = update.callback_query.edit_message_text.call_args.kwargs
        assert "telegram.notifications.current_status_disabled:en" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] == "kbd:en:False"
        update.callback_query.answer.assert_awaited_once()

    async def test_toggle_delivery_telegram_status_notifications_updates_preferences_and_refreshes(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=304)
        update.callback_query = DummyCallbackQuery(data="toggle_delivery_telegram_status_off")
        context = make_context()
        calls = []

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_notification_preferences(self, user_token, payload):
                calls.append((user_token, payload))
                return _resp(
                    success=True,
                    data={
                        "data": {
                            "preferences": {
                                "delivery_telegram_status_updates_enabled": payload[
                                    "delivery_telegram_status_updates_enabled"
                                ]
                            }
                        }
                    },
                )

        monkeypatch.setattr(profile_module, "user_middleware", AsyncMock(return_value={"id": 304}))
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(
            profile_module.ProfileKeyboards,
            "notification_settings",
            lambda language, delivery_telegram_status_updates_enabled: (
                f"kbd:{language}:{delivery_telegram_status_updates_enabled}"
            ),
        )
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        await handler.toggle_delivery_telegram_status_notifications(update, context)

        assert calls == [
            ("jwt-token", {"delivery_telegram_status_updates_enabled": False})
        ]
        update.callback_query.edit_message_text.assert_awaited_once()
        call_kwargs = update.callback_query.edit_message_text.call_args.kwargs
        assert "telegram.notifications.current_status_disabled:en" in call_kwargs["text"]
        assert call_kwargs["reply_markup"] == "kbd:en:False"
        update.callback_query.answer.assert_awaited_once_with(
            "telegram.notifications.update_success:en"
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestProfileAddressHandlerFlows:
    async def test_add_address_continues_when_delete_message_fails(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate(user_id=202)
        update.callback_query = DummyCallbackQuery(data="add_new_address")
        update.callback_query.delete_message = AsyncMock(
            side_effect=BadRequest("Message can't be deleted for everyone")
        )
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "location_request_with_skip", lambda _lang: "loc-kbd")

        state = await handler.add_address(update, context)

        assert state == ADDRESS_LOCATION
        handler.user_repo.update_user_state.assert_awaited_once_with(202, {})
        assert context.user_data["conversation_state"] == "address_location"
        assert context.user_data["temp_address_data"] == {}
        update.callback_query.delete_message.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            text="telegram.address.location_prompt_enhanced:en",
            reply_markup="loc-kbd",
            parse_mode="Markdown",
        )

    async def test_add_address_uses_bot_send_when_callback_message_missing(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate(user_id=203)
        update.callback_query = DummyCallbackQuery(data="add_new_address")
        update.callback_query.message = None
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "location_request_with_skip", lambda _lang: "loc-kbd")

        state = await handler.add_address(update, context)

        assert state == ADDRESS_LOCATION
        update.callback_query.answer.assert_awaited_once()
        context.bot.send_message.assert_awaited_once_with(
            chat_id=203,
            text="telegram.address.location_prompt_enhanced:en",
            reply_markup="loc-kbd",
            parse_mode="Markdown",
        )

    async def test_add_address_skips_delete_for_stale_callback_message(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate(user_id=204)
        update.callback_query = DummyCallbackQuery(data="add_new_address")
        update.callback_query.message.date = datetime.now(timezone.utc) - timedelta(hours=49)
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "location_request_with_skip", lambda _lang: "loc-kbd")

        state = await handler.add_address(update, context)

        assert state == ADDRESS_LOCATION
        update.callback_query.delete_message.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.message.reply_text.assert_awaited_once_with(
            text="telegram.address.location_prompt_enhanced:en",
            reply_markup="loc-kbd",
            parse_mode="Markdown",
        )

    async def test_add_address_from_checkout_marks_checkout_origin(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(update_user_state=AsyncMock())
        update = DummyUpdate(user_id=205)
        update.callback_query = DummyCallbackQuery(data="add_new_address_checkout")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "location_request_with_skip", lambda _lang: "loc-kbd")

        state = await handler.add_address(update, context)

        assert state == ADDRESS_LOCATION
        assert context.user_data["address_flow_origin"] == "checkout"

    async def test_save_address_final_from_checkout_resumes_checkout(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update = DummyUpdate(user_id=206)
        update.callback_query = DummyCallbackQuery(data="addr_title_home")
        context = make_context()
        context.user_data["temp_address_data"] = {
            "title": "Home",
            "full_address": "Sample address",
            "city": "Tashkent",
            "latitude": 41.3,
            "longitude": 69.2,
        }
        context.user_data["address_flow_origin"] = "checkout"

        saved_payloads = []

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def add_user_address(self, token, payload):
                saved_payloads.append((token, payload))
                return _resp(success=True)

        resume_checkout = AsyncMock()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())
        monkeypatch.setattr(orders_module.order_handlers, "checkout_handler", resume_checkout)

        state = await handler.save_address_final(update, context, is_callback=True)

        assert state == ConversationHandler.END
        assert saved_payloads and saved_payloads[0][0] == "jwt-token"
        resume_checkout.assert_awaited_once()
        resumed_update = resume_checkout.await_args.args[0]
        assert resumed_update.callback_query is None
        assert resumed_update.message is update.callback_query.message
        assert "temp_address_data" not in context.user_data
        assert "address_flow_origin" not in context.user_data
        update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
class TestOrderHandlerFlows:
    async def test_orders_menu_returns_when_user_missing(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        context = make_context()
        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value=None))

        result = await handler.orders_menu(update, context)
        assert result is None
        assert update.message.reply_text.await_count == 0

    async def test_orders_menu_handles_auth_error(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler._handle_auth_error = AsyncMock()
        update = DummyUpdate()
        context = make_context()

        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value=None))
        monkeypatch.setattr(orders_module, "api_client", FakeAPIClientContext())

        await handler.orders_menu(update, context)
        handler._handle_auth_error.assert_awaited_once_with(update, "en")

    async def test_orders_menu_no_orders_callback_path(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_orders")
        context = make_context()

        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(get_user_orders=_resp(success=True, data={"data": {"orders": []}})),
        )
        monkeypatch.setattr(orders_module.MenuKeyboards, "main_menu", lambda _lang: "menu-kbd")

        await handler.orders_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.no_orders:en",
            reply_markup="menu-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_orders_menu_with_orders_shows_list(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="menu_orders")
        context = make_context()
        orders_payload = {"data": {"orders": [{"id": 1}, {"id": 2}]}}

        monkeypatch.setattr(orders_module, "user_middleware", AsyncMock(return_value={"id": 1}))
        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(get_user_orders=_resp(success=True, data=orders_payload)),
        )
        monkeypatch.setattr(orders_module.OrderKeyboards, "order_list", lambda _orders, _lang: "orders-kbd")

        await handler.orders_menu(update, context)

        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.your_orders:en\n\n",
            reply_markup="orders-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_cancel_order_confirmation_prompt(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cancel_order_123")
        context = make_context()

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(orders_module.MenuKeyboards, "yes_no_buttons", lambda *_args, **_kwargs: "yes-no-kbd")

        await handler.cancel_order(update, context)

        assert context.user_data["cancelling_order_id"] == 123
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.orders.cancel_confirm:en",
            reply_markup="yes-no-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_cancel_checkout_clears_selection_and_returns_main_menu(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cancel_order")
        context = make_context()
        context.user_data.update(
            {
                "selected_address_id": 11,
                "selected_payment_method": "cash",
                "keep_me": "value",
            }
        )

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(orders_module.MenuKeyboards, "main_menu", lambda _lang: "main-menu-kbd")

        await handler.cancel_checkout(update, context)

        assert "selected_address_id" not in context.user_data
        assert "selected_payment_method" not in context.user_data
        assert context.user_data["keep_me"] == "value"
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.action_cancelled:en",
            reply_markup="main-menu-kbd",
        )
        update.callback_query.answer.assert_awaited_once_with("telegram.action_cancelled_short:en")

    async def test_cancel_order_confirm_yes_success(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.orders_menu = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cancel_order_confirm_yes")
        context = make_context()
        context.user_data["cancelling_order_id"] = 456

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(orders_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(orders_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(
            orders_module,
            "api_client",
            FakeAPIClientContext(cancel_order=_resp(success=True)),
        )

        await handler.cancel_order_confirm_yes(update, context)

        assert "cancelling_order_id" not in context.user_data
        update.callback_query.answer.assert_awaited_once_with("telegram.orders.cancel_success:en")
        handler.orders_menu.assert_awaited_once_with(update, context)

    async def test_cancel_order_confirm_no_returns_to_order_details(self, monkeypatch):
        handler = orders_module.OrderHandlers()
        handler.order_details = AsyncMock()
        update = DummyUpdate()
        update.callback_query = DummyCallbackQuery(data="cancel_order_confirm_no")
        context = make_context()
        context.user_data["cancelling_order_id"] = 789

        monkeypatch.setattr(orders_module.i18n, "get_user_language", AsyncMock(return_value="en"))

        await handler.cancel_order_confirm_no(update, context)

        assert "cancelling_order_id" not in context.user_data
        assert update.callback_query.data == "order_789"
        handler.order_details.assert_awaited_once_with(update, context)
