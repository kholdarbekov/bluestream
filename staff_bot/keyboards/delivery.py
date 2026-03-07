"""
Delivery-related keyboards for Staff Bot
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Dict

from i18n import i18n
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS, FAILED_DELIVERY_REASONS


class DeliveryKeyboards:
    """Keyboards for delivery person flows"""

    @staticmethod
    def order_pool_item(language: str, delivery_id: int) -> InlineKeyboardMarkup:
        """View/Accept buttons for an order in the pool"""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"\U0001f440 {i18n.get('staff.delivery.view_details', language)}",
                callback_data=f"staff_view_order_{delivery_id}"
            ),
            InlineKeyboardButton(
                f"\u2705 {i18n.get('staff.delivery.accept', language)}",
                callback_data=f"staff_accept_order_{delivery_id}"
            )
        ]])

    @staticmethod
    def accept_confirm(language: str, delivery_id: int) -> InlineKeyboardMarkup:
        """Confirm order acceptance"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"\u2705 {i18n.get('staff.confirm', language)}",
                    callback_data=f"staff_confirm_accept_{delivery_id}"
                ),
                InlineKeyboardButton(
                    f"\u274c {i18n.get('staff.cancel', language)}",
                    callback_data="staff_new_orders"
                )
            ]
        ])

    @staticmethod
    def order_detail_actions(
        language: str,
        delivery_id: int,
        order_id: int = None,
        can_mark_preparing: bool = False,
        back_callback: str = "staff_new_orders",
    ) -> InlineKeyboardMarkup:
        """Actions for pool order details."""
        keyboard = [[InlineKeyboardButton(
            f"\u2705 {i18n.get('staff.delivery.accept', language)}",
            callback_data=f"staff_accept_order_{delivery_id}"
        )]]

        if can_mark_preparing and order_id:
            keyboard.append([InlineKeyboardButton(
                f"\U0001f6e0\ufe0f {i18n.get('staff.delivery.mark_preparing', language)}",
                callback_data=f"staff_mark_preparing_{order_id}"
            )])

        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data=back_callback
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def active_delivery_actions(
        language: str, delivery_id: int, current_status: str
    ) -> InlineKeyboardMarkup:
        """Action buttons for an active delivery based on current status"""
        keyboard = []

        # Status transition buttons
        allowed_next = DELIVERY_STATUS_TRANSITIONS.get(current_status, [])
        for next_status in allowed_next:
            if next_status == 'failed':
                emoji = "\u274c"
            else:
                emoji = {
                    'picked_up': '\U0001f4e6',
                    'in_transit': '\U0001f69a',
                    'arrived': '\U0001f4cd',
                    'delivered': '\u2705',
                }.get(next_status, '\u27a1\ufe0f')

            keyboard.append([InlineKeyboardButton(
                f"{emoji} {i18n.get(f'staff.delivery.status.{next_status}', language)}",
                callback_data=f"staff_status_{delivery_id}_{next_status}"
            )])

        # Navigate button (opens Yandex Maps route)
        keyboard.append([InlineKeyboardButton(
            f"\U0001f4cd {i18n.get('staff.delivery.navigate', language)}",
            callback_data=f"staff_navigate_{delivery_id}"
        )])

        # Back button
        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data="staff_active_deliveries"
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def failed_reasons(language: str, delivery_id: int) -> InlineKeyboardMarkup:
        """Failed delivery reason selection"""
        keyboard = []
        for reason in FAILED_DELIVERY_REASONS:
            keyboard.append([InlineKeyboardButton(
                i18n.get(f'staff.delivery.reason.{reason}', language),
                callback_data=f"staff_failed_reason_{delivery_id}_{reason}"
            )])

        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data=f"staff_view_active_{delivery_id}"
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def cash_collection_options(
        language: str, delivery_id: int, amount: float
    ) -> InlineKeyboardMarkup:
        """Explicit delivery-completion cash collection options."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\u2705 {i18n.get('staff.delivery.confirm_cash', language, amount=f'{amount:,.0f}')}",
                callback_data=f"staff_cash_full_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"\u270f\ufe0f {i18n.get('staff.delivery.edit_cash', language)}",
                callback_data=f"staff_cash_partial_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"\u274c {i18n.get('staff.delivery.no_cash_collected', language)}",
                callback_data=f"staff_cash_none_{delivery_id}"
            )],
        ])

    @staticmethod
    def reconciliation_actions(language: str, can_submit: bool = True) -> InlineKeyboardMarkup:
        """Actions for the driver's reconciliation session view."""
        keyboard = []
        if can_submit:
            keyboard.append([InlineKeyboardButton(
                f"\U0001f4b5 {i18n.get('staff.delivery.submit_reconciliation', language)}",
                callback_data="staff_reconcile_submit"
            )])
        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def cod_customer_result(language: str, customer_id: int) -> InlineKeyboardMarkup:
        """View COD debt details for a searched customer."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\U0001f4dc {i18n.get('staff.delivery.view_cod_statement', language)}",
                callback_data=f"staff_cod_customer_{customer_id}"
            )],
        ])

    @staticmethod
    def cod_statement_actions(
        language: str,
        customer_id: int,
        *,
        can_collect: bool = True,
    ) -> InlineKeyboardMarkup:
        """Actions available from a customer's COD debt statement."""
        keyboard = []
        if can_collect:
            keyboard.append([InlineKeyboardButton(
                f"\U0001f4b8 {i18n.get('staff.delivery.collect_full_cod', language)}",
                callback_data=f"staff_cod_collect_full_{customer_id}"
            )])
            keyboard.append([InlineKeyboardButton(
                f"\u270f\ufe0f {i18n.get('staff.delivery.collect_custom_cod', language)}",
                callback_data=f"staff_cod_collect_custom_{customer_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)
