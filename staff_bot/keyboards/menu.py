"""
Main menu keyboards for Staff Bot - Role-aware keyboard generation
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from typing import List

from staff_bot.i18n import i18n


class MenuKeyboards:
    """Role-aware menu keyboard builder"""

    @staticmethod
    def main_menu(language: str, staff_roles: List[str] = None) -> ReplyKeyboardMarkup:
        """
        Build reply keyboard main menu based on staff roles.
        Delivery drivers see delivery options, operators see operator options,
        dual-role users see both.
        """
        if staff_roles is None:
            staff_roles = []

        keyboard = []

        # Delivery driver menu items
        if 'delivery_driver' in staff_roles:
            keyboard.append([
                f"\U0001f195 {i18n.get('staff.menu.new_orders', language)}",
                f"\U0001f69a {i18n.get('staff.menu.active_deliveries', language)}"
            ])
            keyboard.append([
                f"\u2795 {i18n.get('staff.menu.create_tryout_now', language)}",
                f"\U0001f9ea {i18n.get('staff.menu.tryout_tasks', language)}",
            ])
            keyboard.append([
                f"\u267b\ufe0f {i18n.get('staff.menu.active_tryouts', language)}",
                f"\U0001f4cb {i18n.get('staff.menu.delivery_history', language)}",
                f"\U0001f4ca {i18n.get('staff.menu.my_stats', language)}"
            ])
            keyboard.append([
                f"\U0001f9fe {i18n.get('staff.menu.cash_reconciliation', language)}",
                f"\U0001f4b8 {i18n.get('staff.menu.collect_cod_debt', language)}",
            ])

        # Operator menu items
        if 'operator' in staff_roles:
            keyboard.append([
                f"\U0001f195 {i18n.get('staff.menu.new_orders_view', language)}",
                f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
            ])
            keyboard.append([
                f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
                f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
            ])
            keyboard.append([
                f"\U0001f4c4 {i18n.get('staff.menu.recent_orders', language)}"
            ])

        # Common items (all staff)
        keyboard.append([
            f"\U0001f464 {i18n.get('staff.menu.profile', language)}",
            f"\u2699\ufe0f {i18n.get('staff.menu.settings', language)}"
        ])
        keyboard.append([
            f"\u2753 {i18n.get('staff.menu.help', language)}"
        ])

        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def main_menu_inline(language: str, staff_roles: List[str] = None) -> InlineKeyboardMarkup:
        """
        Build inline keyboard main menu based on staff roles.
        Used when editing messages.
        """
        if staff_roles is None:
            staff_roles = []

        keyboard = []

        # Delivery driver items
        if 'delivery_driver' in staff_roles:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f195 {i18n.get('staff.menu.new_orders', language)}",
                    callback_data="staff_new_orders"
                ),
                InlineKeyboardButton(
                    f"\U0001f69a {i18n.get('staff.menu.active_deliveries', language)}",
                    callback_data="staff_active_deliveries"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\u2795 {i18n.get('staff.menu.create_tryout_now', language)}",
                    callback_data="staff_tryout_create"
                ),
                InlineKeyboardButton(
                    f"\U0001f9ea {i18n.get('staff.menu.tryout_tasks', language)}",
                    callback_data="staff_tryout_tasks"
                ),
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\u267b\ufe0f {i18n.get('staff.menu.active_tryouts', language)}",
                    callback_data="staff_tryout_active"
                ),
                InlineKeyboardButton(
                    f"\U0001f4cb {i18n.get('staff.menu.delivery_history', language)}",
                    callback_data="staff_delivery_history"
                ),
                InlineKeyboardButton(
                    f"\U0001f4ca {i18n.get('staff.menu.my_stats', language)}",
                    callback_data="staff_my_stats"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f9fe {i18n.get('staff.menu.cash_reconciliation', language)}",
                    callback_data="staff_reconcile_session"
                ),
                InlineKeyboardButton(
                    f"\U0001f4b8 {i18n.get('staff.menu.collect_cod_debt', language)}",
                    callback_data="staff_cod_collect_menu"
                ),
            ])

        # Operator items
        if 'operator' in staff_roles:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f195 {i18n.get('staff.menu.new_orders_view', language)}",
                    callback_data="staff_op_new_orders"
                ),
                InlineKeyboardButton(
                    f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
                    callback_data="staff_create_client"
                ),
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
                    callback_data="staff_create_order"
                ),
                InlineKeyboardButton(
                    f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
                    callback_data="staff_search_client"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4c4 {i18n.get('staff.menu.recent_orders', language)}",
                    callback_data="staff_recent_orders"
                )
            ])

        # Common items
        keyboard.append([
            InlineKeyboardButton(
                f"\U0001f464 {i18n.get('staff.menu.profile', language)}",
                callback_data="staff_profile"
            ),
            InlineKeyboardButton(
                f"\u2699\ufe0f {i18n.get('staff.menu.settings', language)}",
                callback_data="staff_settings"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"\u2753 {i18n.get('staff.menu.help', language)}",
                callback_data="staff_help"
            )
        ])

        return InlineKeyboardMarkup(keyboard)
