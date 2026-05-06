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
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data=callback_data
            )
        ]])

    @staticmethod
    def flow_cancel(language: str) -> InlineKeyboardMarkup:
        """Cancel button for free-text input prompts that drive a `pending_*_flow`.

        The bot's catch-all text router (`_handle_text_message`) eats every
        text update while any of `pending_delivery_cash_flow`,
        `pending_reconciliation_flow`, `pending_cod_collection_flow`,
        `pending_bottle_collection_flow`, or `tryout_pickup_task_id` is set \u2014
        even reply-keyboard taps, because those send text. Without an inline
        Cancel the user has no way back except typing a value the parser
        accepts. Pair this button with the `staff_flow_cancel` global handler
        registered in `bot.py`, which clears every flow flag and returns the
        user to the cash hub.
        """
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"\u274c {i18n.get('staff.cancel', language)}",
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
                f"\u2705 {i18n.get('staff.confirm', language)}",
                callback_data=confirm_data
            ),
            InlineKeyboardButton(
                f"\u274c {i18n.get('staff.cancel', language)}",
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
                "\u25c0\ufe0f",
                callback_data=f"{callback_prefix}_page_{current_page - 1}"
            ))

        buttons.append(InlineKeyboardButton(
            f"{current_page}/{total_pages}",
            callback_data="noop"
        ))

        if current_page < total_pages:
            buttons.append(InlineKeyboardButton(
                "\u25b6\ufe0f",
                callback_data=f"{callback_prefix}_page_{current_page + 1}"
            ))

        return buttons

    @staticmethod
    def location_request(language: str, button_text: str) -> ReplyKeyboardMarkup:
        """Reply keyboard with Telegram location request button."""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(button_text, request_location=True)],
                [KeyboardButton(i18n.get('staff.cancel', language))],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
