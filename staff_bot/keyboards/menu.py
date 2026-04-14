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
        Unified `New Orders` row is shown whenever the user has any staff role;
        the handler routes by role. Delivery-specific and operator-specific
        rows follow, then shared rows.
        """
        if staff_roles is None:
            staff_roles = []

        is_driver = 'delivery_driver' in staff_roles
        is_operator = 'operator' in staff_roles

        keyboard = []

        # Unified New Orders button + role-specific companion
        if is_driver:
            keyboard.append([
                f"\U0001f4e6 {i18n.get('staff.menu.new_orders', language)}",
                f"\U0001f69a {i18n.get('staff.menu.active_deliveries', language)}",
            ])
            keyboard.append([
                f"\U0001f9ea {i18n.get('staff.menu.tryouts', language)}",
                f"\U0001f4b0 {i18n.get('staff.menu.cash', language)}",
            ])
        elif is_operator:
            # Operator-only users still need the New Orders entry point
            keyboard.append([
                f"\U0001f4e6 {i18n.get('staff.menu.new_orders', language)}",
                f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
            ])

        # Operator-specific rows (skipped for operator-only since New Orders row already
        # paired with Create Client above)
        if is_operator and is_driver:
            keyboard.append([
                f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
                f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
            ])
            keyboard.append([
                f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
            ])
        elif is_operator:
            keyboard.append([
                f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
                f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
            ])

        # Common items (all staff)
        keyboard.append([
            f"\U0001f464 {i18n.get('staff.menu.profile', language)}",
            f"\u2699\ufe0f {i18n.get('staff.menu.settings', language)}",
        ])
        keyboard.append([
            f"\u2753 {i18n.get('staff.menu.help', language)}",
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

        is_driver = 'delivery_driver' in staff_roles
        is_operator = 'operator' in staff_roles

        keyboard = []

        if is_driver:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.menu.new_orders', language)}",
                    callback_data="staff_new_orders_unified",
                ),
                InlineKeyboardButton(
                    f"\U0001f69a {i18n.get('staff.menu.active_deliveries', language)}",
                    callback_data="staff_active_deliveries",
                ),
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f9ea {i18n.get('staff.menu.tryouts', language)}",
                    callback_data="staff_tryouts_hub",
                ),
                InlineKeyboardButton(
                    f"\U0001f4b0 {i18n.get('staff.menu.cash', language)}",
                    callback_data="staff_cash_hub",
                ),
            ])
        elif is_operator:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.menu.new_orders', language)}",
                    callback_data="staff_new_orders_unified",
                ),
                InlineKeyboardButton(
                    f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
                    callback_data="staff_create_client",
                ),
            ])

        if is_operator and is_driver:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f464 {i18n.get('staff.menu.create_client', language)}",
                    callback_data="staff_create_client",
                ),
                InlineKeyboardButton(
                    f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
                    callback_data="staff_search_client",
                ),
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
                    callback_data="staff_create_order",
                ),
            ])
        elif is_operator:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.menu.create_order', language)}",
                    callback_data="staff_create_order",
                ),
                InlineKeyboardButton(
                    f"\U0001f50d {i18n.get('staff.menu.search_client', language)}",
                    callback_data="staff_search_client",
                ),
            ])

        # Common items
        keyboard.append([
            InlineKeyboardButton(
                f"\U0001f464 {i18n.get('staff.menu.profile', language)}",
                callback_data="staff_profile",
            ),
            InlineKeyboardButton(
                f"\u2699\ufe0f {i18n.get('staff.menu.settings', language)}",
                callback_data="staff_settings",
            ),
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"\u2753 {i18n.get('staff.menu.help', language)}",
                callback_data="staff_help",
            ),
        ])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def tryouts_hub(language: str) -> InlineKeyboardMarkup:
        """Sub-menu for try-out actions (delivery driver only)."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\u2795 {i18n.get('staff.tryouts.create', language)}",
                callback_data="staff_tryout_create",
            )],
            [InlineKeyboardButton(
                f"\U0001f9ea {i18n.get('staff.menu.tryout_tasks', language)}",
                callback_data="staff_tryout_tasks",
            )],
            [InlineKeyboardButton(
                f"\u267b\ufe0f {i18n.get('staff.menu.active_tryouts', language)}",
                callback_data="staff_tryout_active",
            )],
            [InlineKeyboardButton(
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main",
            )],
        ])

    @staticmethod
    def cash_hub(language: str) -> InlineKeyboardMarkup:
        """Sub-menu for cash-handling actions (delivery driver only)."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\U0001f9fe {i18n.get('staff.menu.cash_reconciliation', language)}",
                callback_data="staff_reconcile_session",
            )],
            [InlineKeyboardButton(
                f"\U0001f4b8 {i18n.get('staff.menu.collect_cod_debt', language)}",
                callback_data="staff_cod_collect_menu",
            )],
            [InlineKeyboardButton(
                f"\U0001f4e6 {i18n.get('staff.menu.bottle_collection', language)}",
                callback_data="staff_bottle_collect_menu",
            )],
            [InlineKeyboardButton(
                f"\U0001f4e6 {i18n.get('staff.menu.log_bottles_loaded', language)}",
                callback_data="staff_bottle_log_loaded",
            )],
            [InlineKeyboardButton(
                f"\u21a9\ufe0f {i18n.get('staff.menu.return_to_warehouse', language)}",
                callback_data="staff_bottle_return_warehouse",
            )],
            [InlineKeyboardButton(
                f"\U0001f4ca {i18n.get('staff.menu.my_bottle_accountability', language)}",
                callback_data="staff_bottle_my_accountability",
            )],
            [InlineKeyboardButton(
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main",
            )],
        ])

    @staticmethod
    def profile_hub(language: str, staff_roles: List[str] = None) -> InlineKeyboardMarkup:
        """Sub-menu rendered under the profile info block. Role-aware."""
        if staff_roles is None:
            staff_roles = []

        is_driver = 'delivery_driver' in staff_roles
        is_operator = 'operator' in staff_roles

        rows = []
        if is_driver:
            rows.append([InlineKeyboardButton(
                f"\U0001f4ca {i18n.get('staff.profile.view_stats', language)}",
                callback_data="staff_my_stats",
            )])
            rows.append([InlineKeyboardButton(
                f"\U0001f4cb {i18n.get('staff.profile.view_history', language)}",
                callback_data="staff_delivery_history",
            )])
        if is_operator:
            rows.append([InlineKeyboardButton(
                f"\U0001f4c4 {i18n.get('staff.profile.view_recent_orders', language)}",
                callback_data="staff_recent_orders",
            )])

        rows.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main",
        )])

        return InlineKeyboardMarkup(rows)
