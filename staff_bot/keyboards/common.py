"""
Common/shared keyboard components for Staff Bot
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from typing import List

from staff_bot.i18n import i18n


class CommonKeyboards:
    """Common keyboards used across handlers"""

    @staticmethod
    def back_button(language: str, callback_data: str = "staff_back_to_main") -> InlineKeyboardMarkup:
        """Single back button"""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data=callback_data
            )
        ]])

    @staticmethod
    def flow_cancel(language: str) -> InlineKeyboardMarkup:
        """Cancel button for free-text input prompts that drive a `pending_*_flow`.

        The bot's catch-all text router (`_handle_text_message`) eats every
        text update while any of `pending_delivery_cash_flow`,
        `pending_reconciliation_flow`, `pending_cod_collection_flow`,
        `pending_bottle_collection_flow`, or `tryout_pickup_task_id` is set —
        even reply-keyboard taps, because those send text. Without an inline
        Cancel the user has no way back except typing a value the parser
        accepts. Pair this button with the `staff_flow_cancel` global handler
        registered in `bot.py`, which clears every flow flag and returns the
        user to the cash hub.
        """
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data="staff_flow_cancel",
            )
        ]])

    @staticmethod
    def confirm_cancel(
        language: str,
        confirm_data: str,
        cancel_data: str = "staff_back_to_main"
    ) -> InlineKeyboardMarkup:
        """Confirm / Cancel buttons"""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ {i18n.get('staff.confirm', language)}",
                callback_data=confirm_data
            ),
            InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data=cancel_data
            )
        ]])

    @staticmethod
    def yes_no(language: str, yes_data: str, no_data: str) -> InlineKeyboardMarkup:
        """Yes / No buttons"""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                i18n.get('staff.yes', language),
                callback_data=yes_data
            ),
            InlineKeyboardButton(
                i18n.get('staff.no', language),
                callback_data=no_data
            )
        ]])

    @staticmethod
    def pagination(
        language: str, current_page: int, total_pages: int,
        callback_prefix: str
    ) -> List[InlineKeyboardButton]:
        """Pagination buttons row"""
        buttons = []
        if current_page > 1:
            buttons.append(InlineKeyboardButton(
                "◀️",
                callback_data=f"{callback_prefix}_page_{current_page - 1}"
            ))

        buttons.append(InlineKeyboardButton(
            f"{current_page}/{total_pages}",
            callback_data="noop"
        ))

        if current_page < total_pages:
            buttons.append(InlineKeyboardButton(
                "▶️",
                callback_data=f"{callback_prefix}_page_{current_page + 1}"
            ))

        return buttons

    @staticmethod
    def location_request(
        language: str, button_text: str, include_cancel: bool = True
    ) -> ReplyKeyboardMarkup:
        """Reply keyboard with Telegram location request button.

        WHY THIS KEYBOARD EXISTS AT ALL: `request_location` is a field of
        `KeyboardButton` and of nothing else -- MTProto is explicit,
        "Available only in private chats, in reply keyboards"
        (core.telegram.org/constructor/keyboardButtonRequestGeoLocation), and
        `InlineKeyboardButton` has had no location field since Bot API 2.0 in
        2016. Telegram's own "Share your location?" dialog is triggered BY this
        button. So an inline button -- like Optimize on the route card -- can
        never ask for a location itself; the bot has to draw this prompt to
        reach that dialog. The extra tap is structural, not a design choice.

        `include_cancel` (2026-08-14, driver feedback): the delivery paths pass
        False. The driver's complaint was the "unwanted layer of step (two
        buttons: one to share location and other is cancel)", and the second
        button was pure cost there -- on the delivery paths Cancel has NO
        handler: the tap falls through to `_handle_text_message`, matches no
        menu label and no flow flag, and is dropped. It looked like an escape
        and was not one. The driver's real exit is the route card's own inline
        buttons, which the reply keyboard does not cover.

        The default stays True because `staff_bot/handlers/tryouts.py` is the
        one caller whose Cancel is real: `receive_create_address` compares the
        text against `staff.cancel` and aborts the conversation, and it is the
        only way out of address entry. Defaulting to False would silently strip
        that exit.

        `one_time_keyboard=True` is deliberate and must NOT be dropped to match
        `MenuKeyboards.main_menu`'s `is_persistent=True`. These are two
        different kinds of keyboard: the main menu is the driver's permanent
        control surface and has to stay up, while this is a TRANSIENT PROMPT
        that answers one question and should get out of the way once answered.
        """
        rows = [[KeyboardButton(button_text, request_location=True)]]
        if include_cancel:
            rows.append([KeyboardButton(i18n.get('staff.cancel', language))])
        return ReplyKeyboardMarkup(
            rows,
            resize_keyboard=True,
            one_time_keyboard=True,
        )
