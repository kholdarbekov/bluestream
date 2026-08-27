"""Profile-edit sub-menu, name + birthday round-trips, phone routing (Deliverable C2-C5)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import keyboards as keyboards_module
from handlers import profile as profile_module
from keyboards import ProfileKeyboards
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, FakeAPIClientContext, make_context


def _resp(success=True, data=None, error=None, status_code=200):
    return SimpleNamespace(success=success, data=data or {}, error=error, status_code=status_code)


@pytest.mark.unit
class TestProfileEditMenuKeyboard:
    def test_profile_edit_menu_has_all_field_callbacks(self, monkeypatch):
        monkeypatch.setattr(keyboards_module.i18n, "get", lambda key, lang, **_: key)
        markup = ProfileKeyboards.profile_edit_menu("en")
        callbacks = {btn.callback_data for row in markup.inline_keyboard for btn in row}
        assert {
            "edit_profile_name",
            "edit_profile_birthday",
            "edit_profile_language",
            "edit_profile_phone",
            "menu_profile",
        } <= callbacks


@pytest.mark.unit
@pytest.mark.anyio
class TestEditProfileSubMenu:
    async def test_edit_profile_shows_field_submenu_and_disarms_only_its_own_prompts(
        self, monkeypatch
    ):
        """Opening the sub-menu abandons the name/birthday prompt it offers.

        It used to abandon EVERYTHING: `update_user_state(user_id, {})` also
        wiped a concern report armed by "Report an issue", silently, while that
        report's prompt and Cancel button were still on the customer's screen.
        So the clear is now targeted — the screen names the prompts it owns and
        `clear_awaiting_input` leaves any other flow armed.
        """
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(
            clear_awaiting_input=AsyncMock(return_value=False),
            arm_awaiting_input=AsyncMock(),
            disarm=AsyncMock(),
        )
        update = DummyUpdate(user_id=501)
        update.callback_query = DummyCallbackQuery(data="edit_profile")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.ProfileKeyboards, "profile_edit_menu", lambda _lang: "edit-menu-kbd")

        await handler.edit_profile(update, context)

        handler.user_repo.clear_awaiting_input.assert_awaited_once_with(
            501, "edit_profile_name", "edit_profile_birthday"
        )
        # Was: update_user_state.assert_not_awaited() — "this screen must not
        # blanket-wipe". `update_user_state` is no longer a call surface any
        # handler reaches directly; `arm_awaiting_input`/`disarm` are the two
        # methods that could now perform a raw whole-document write, so the
        # same intent ("must not disarm or arm anything beyond its own named
        # clear") is asserted against those.
        handler.user_repo.arm_awaiting_input.assert_not_awaited()
        handler.user_repo.disarm.assert_not_awaited()
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.profile.edit_menu_title:en",
            reply_markup="edit-menu-kbd",
        )
        update.callback_query.answer.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
class TestProfileNameEdit:
    async def test_edit_profile_name_prompt_sets_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(arm_awaiting_input=AsyncMock())
        update = DummyUpdate(user_id=601)
        update.callback_query = DummyCallbackQuery(data="edit_profile_name")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.edit_profile_name_prompt(update, context)

        # Was: update_user_state.assert_awaited_once_with(601, {"awaiting_input": "edit_profile_name"})
        # Same facts (user id, flow name, no companions), against the new method.
        handler.user_repo.arm_awaiting_input.assert_awaited_once_with(601, 'edit_profile_name')
        update.callback_query.edit_message_text.assert_awaited_once()
        update.callback_query.answer.assert_awaited_once()

    async def test_handle_profile_name_edit_splits_and_updates(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(
            update_user_state=AsyncMock(), clear_awaiting_input=AsyncMock(return_value=True)
        )
        update = DummyUpdate(user_id=602)
        context = make_context()
        captured = {}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                captured["token"] = token
                captured["payload"] = payload
                return _resp(success=True)

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())
        monkeypatch.setattr(profile_module.ProfileKeyboards, "profile_edit_menu", lambda _lang: "edit-menu-kbd")

        await handler.handle_profile_name_edit(update, context, "John Van Der Berg", {})

        assert captured["token"] == "jwt-token"
        assert captured["payload"] == {"first_name": "John", "last_name": "Van Der Berg"}
        # The completed flow disarms ITSELF; a flow armed elsewhere is not this
        # handler's to throw away.
        handler.user_repo.clear_awaiting_input.assert_awaited_once_with(602, "edit_profile_name")

    async def test_handle_profile_name_edit_rejects_too_short(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(arm_awaiting_input=AsyncMock(), disarm=AsyncMock())
        update = DummyUpdate(user_id=603)
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.handle_profile_name_edit(update, context, "J", {})

        update.message.reply_text.assert_awaited_once_with("telegram.name.too_short:en")
        # Was: update_user_state.assert_not_awaited() — a rejected edit must not
        # touch state. Repointed to the methods that could now do that.
        handler.user_repo.arm_awaiting_input.assert_not_awaited()
        handler.user_repo.disarm.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
class TestBirthdayTextEntry:
    """Birthday text-entry flow: prompt + completer (replaces the old button picker)."""

    async def test_edit_profile_birthday_start_sets_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(arm_awaiting_input=AsyncMock())
        update = DummyUpdate(user_id=701)
        update.callback_query = DummyCallbackQuery(data="edit_profile_birthday")
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module.MenuKeyboards, "cancel_button", lambda _lang: "cancel-kbd")

        await handler.edit_profile_birthday_start(update, context)

        # Was: update_user_state.assert_awaited_once_with(701, {"awaiting_input": "edit_profile_birthday"})
        # Same facts (user id, flow name, no companions), against the new method.
        handler.user_repo.arm_awaiting_input.assert_awaited_once_with(701, 'edit_profile_birthday')
        update.callback_query.edit_message_text.assert_awaited_once_with(
            text="telegram.profile.birthday_prompt:en",
            reply_markup="cancel-kbd",
        )
        update.callback_query.answer.assert_awaited_once()

    async def test_handle_profile_birthday_edit_valid_date_calls_backend(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(
            update_user_state=AsyncMock(), clear_awaiting_input=AsyncMock(return_value=True)
        )
        update = DummyUpdate(user_id=702)
        context = make_context()
        captured = {}

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                captured["token"] = token
                captured["payload"] = payload
                return _resp(success=True)

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())
        monkeypatch.setattr(profile_module.ProfileKeyboards, "profile_edit_menu", lambda _lang: "edit-menu-kbd")

        await handler.handle_profile_birthday_edit(update, context, "17-05-1990", {})

        # Must convert DD-MM-YYYY -> YYYY-MM-DD (ISO)
        assert captured["payload"] == {"date_of_birth": "1990-05-17"}
        assert captured["token"] == "jwt-token"
        handler.user_repo.clear_awaiting_input.assert_awaited_once_with(
            702, "edit_profile_birthday"
        )
        update.message.reply_text.assert_awaited_once_with(
            text="telegram.profile.birthday_updated:en",
            reply_markup="edit-menu-kbd",
        )

    async def test_handle_profile_birthday_edit_invalid_format_keeps_state(self, monkeypatch):
        """Slash-separated or non-date text -> error reply, state NOT cleared, no backend call."""
        handler = profile_module.ProfileHandlers()
        update_state_mock = AsyncMock()
        handler.user_repo = SimpleNamespace(update_user_state=update_state_mock)
        update = DummyUpdate(user_id=703)
        context = make_context()
        update_profile_called = []

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                update_profile_called.append(payload)
                return _resp(success=True)

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        await handler.handle_profile_birthday_edit(update, context, "1990/05/17", {})

        update.message.reply_text.assert_awaited_once_with(
            "telegram.profile.birthday_invalid_format:en"
        )
        update_state_mock.assert_not_awaited()
        assert update_profile_called == [], "backend must NOT be called on invalid format"

    async def test_handle_profile_birthday_edit_nonsense_text_keeps_state(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        update_state_mock = AsyncMock()
        handler.user_repo = SimpleNamespace(update_user_state=update_state_mock)
        update = DummyUpdate(user_id=704)
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.handle_profile_birthday_edit(update, context, "hello", {})

        update.message.reply_text.assert_awaited_once_with(
            "telegram.profile.birthday_invalid_format:en"
        )
        update_state_mock.assert_not_awaited()

    async def test_handle_profile_birthday_edit_impossible_date_keeps_state(self, monkeypatch):
        """31-02-1990 is an impossible date -> ValueError -> error, no backend call."""
        handler = profile_module.ProfileHandlers()
        update_state_mock = AsyncMock()
        handler.user_repo = SimpleNamespace(update_user_state=update_state_mock)
        update = DummyUpdate(user_id=705)
        context = make_context()

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")

        await handler.handle_profile_birthday_edit(update, context, "31-02-1990", {})

        update.message.reply_text.assert_awaited_once_with(
            "telegram.profile.birthday_invalid_format:en"
        )
        update_state_mock.assert_not_awaited()

    async def test_handle_profile_birthday_edit_backend_failure_keeps_state(self, monkeypatch):
        """Backend returns failure (e.g. too young) -> error reply, state NOT cleared."""
        handler = profile_module.ProfileHandlers()
        update_state_mock = AsyncMock()
        handler.user_repo = SimpleNamespace(update_user_state=update_state_mock)
        update = DummyUpdate(user_id=706)
        context = make_context()

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def update_user_profile(self, token, payload):
                return _resp(success=False, error="Age must be between 10 and 100")

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: f"{key}:{lang}")
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())

        await handler.handle_profile_birthday_edit(update, context, "17-05-2020", {})

        update.message.reply_text.assert_awaited_once_with(
            "telegram.profile.birthday_update_failed:en"
        )
        update_state_mock.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
class TestProfileMenuBirthdayDisplay:
    """profile_menu must render birthday as DD-MM-YYYY (not raw ISO YYYY-MM-DD)."""

    async def test_profile_menu_shows_birthday_as_dd_mm_yyyy(self, monkeypatch):
        handler = profile_module.ProfileHandlers()
        handler.user_repo = SimpleNamespace(
            update_user_state=AsyncMock(), clear_awaiting_input=AsyncMock(return_value=False)
        )
        update = DummyUpdate(user_id=801)
        update.callback_query = None  # message path
        context = make_context()
        sent_text = {}

        async def _reply_text(text, **kwargs):
            sent_text["text"] = text

        update.message.reply_text = _reply_text

        class _APIContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                return False

            async def get_user_profile(self, token):
                return _resp(success=True, data={"data": {
                    "first_name": "Test",
                    "last_name": "User",
                    "phone": "+998901234567",
                    "email": "test@test.com",
                    "date_of_birth": "1990-05-17",
                }})

        monkeypatch.setattr(profile_module.i18n, "get_user_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(profile_module.i18n, "get", lambda key, lang, **_: key)
        monkeypatch.setattr(profile_module, "get_auth_token", AsyncMock(return_value="jwt-token"))
        monkeypatch.setattr(profile_module, "api_client", _APIContext())
        monkeypatch.setattr(profile_module, "user_middleware", AsyncMock(return_value=SimpleNamespace(id=801)))
        monkeypatch.setattr(profile_module.ProfileKeyboards, "profile_menu", lambda _lang: "profile-kbd")

        await handler.profile_menu(update, context)

        assert "17-05-1990" in sent_text.get("text", ""), (
            f"Expected DD-MM-YYYY format in profile display, got: {sent_text.get('text')}"
        )
        assert "1990-05-17" not in sent_text.get("text", ""), (
            "Raw ISO YYYY-MM-DD must not appear in profile display"
        )


@pytest.mark.unit
def test_edit_profile_phone_routes_to_phone_verification_menu():
    """The Phone option in the edit sub-menu reuses the existing phone-verification
    submenu — it must be wired to phone_verification_menu, not a new handler."""
    import inspect
    import re
    import bot as bot_module

    src = inspect.getsource(bot_module)
    # The ^edit_profile_phone$ pattern must be handled by phone_verification_menu.
    match = re.search(
        r"CallbackQueryHandler\(\s*profile_handlers\.phone_verification_menu,\s*"
        r'pattern="\^edit_profile_phone\$"',
        src,
    )
    assert match, "edit_profile_phone must route to phone_verification_menu"
