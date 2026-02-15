"""
Common/shared keyboard components for Staff Bot
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from typing import List

from i18n import i18n


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
