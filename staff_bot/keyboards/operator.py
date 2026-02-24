"""
Operator-related keyboards for Staff Bot
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Dict

from i18n import i18n


class OperatorKeyboards:
    """Keyboards for operator flows"""

    @staticmethod
    def user_found(language: str, user_id: int) -> InlineKeyboardMarkup:
        """Actions for a found client user"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"\U0001f4e6 {i18n.get('staff.operator.create_order_for', language)}",
                    callback_data=f"staff_op_order_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"\U0001f4cd {i18n.get('staff.operator.manage_addresses', language)}",
                    callback_data=f"staff_op_addresses_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                    callback_data="staff_back_to_main"
                )
            ]
        ])

    @staticmethod
    def user_not_found(language: str) -> InlineKeyboardMarkup:
        """Options when user is not found"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\U0001f464 {i18n.get('staff.operator.create_user', language)}",
                callback_data="staff_op_create_user"
            )],
            [InlineKeyboardButton(
                f"\U0001f50d {i18n.get('staff.operator.search_again', language)}",
                callback_data="staff_search_client"
            )],
            [InlineKeyboardButton(
                f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main"
            )]
        ])

    @staticmethod
    def product_list(language: str, products: List[Dict]) -> InlineKeyboardMarkup:
        """Product selection for order creation"""
        keyboard = []
        for product in products:
            name = product.get('name') or i18n.get('staff.common.not_available', language)
            price = product.get('price', 0)
            keyboard.append([InlineKeyboardButton(
                f"{name} - {price:,.0f} {i18n.get('staff.currency.uzs', language)}",
                callback_data=f"staff_op_product_{product['id']}"
            )])

        keyboard.append([InlineKeyboardButton(
            f"\u2705 {i18n.get('staff.operator.done_selecting', language)}",
            callback_data="staff_op_products_done"
        )])
        keyboard.append([InlineKeyboardButton(
            f"\u274c {i18n.get('staff.cancel', language)}",
            callback_data="staff_back_to_main"
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def quantity_selection(language: str, product_id: int) -> InlineKeyboardMarkup:
        """Quantity selection buttons"""
        keyboard = []
        row = []
        for qty in [1, 2, 3, 4, 5]:
            row.append(InlineKeyboardButton(
                str(qty), callback_data=f"staff_op_qty_{product_id}_{qty}"
            ))
        keyboard.append(row)

        row2 = []
        for qty in [6, 8, 10, 15, 20]:
            row2.append(InlineKeyboardButton(
                str(qty), callback_data=f"staff_op_qty_{product_id}_{qty}"
            ))
        keyboard.append(row2)

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def address_list(language: str, addresses: List[Dict], user_id: int) -> InlineKeyboardMarkup:
        """Address selection for order"""
        keyboard = []
        for addr in addresses:
            label = addr.get('label') or addr.get('address_line_1') or i18n.get('staff.operator.address', language)
            keyboard.append([InlineKeyboardButton(
                f"\U0001f4cd {label}",
                callback_data=f"staff_op_addr_{addr['id']}"
            )])

        keyboard.append([InlineKeyboardButton(
            f"\u2795 {i18n.get('staff.operator.add_address', language)}",
            callback_data=f"staff_op_add_addr_{user_id}"
        )])
        keyboard.append([InlineKeyboardButton(
            f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def payment_methods(language: str) -> InlineKeyboardMarkup:
        """Payment method selection"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\U0001f4b5 {i18n.get('staff.operator.payment_cash', language)}",
                callback_data="staff_op_pay_cash"
            )],
            [InlineKeyboardButton(
                f"\U0001f4b3 {i18n.get('staff.operator.payment_payme', language)}",
                callback_data="staff_op_pay_payme"
            )],
            [InlineKeyboardButton(
                f"\U0001f4b3 {i18n.get('staff.operator.payment_click', language)}",
                callback_data="staff_op_pay_click"
            )]
        ])

    @staticmethod
    def order_confirm(language: str) -> InlineKeyboardMarkup:
        """Order confirmation"""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"\u2705 {i18n.get('staff.operator.confirm_order', language)}",
                callback_data="staff_op_confirm_order"
            )],
            [InlineKeyboardButton(
                f"\u274c {i18n.get('staff.cancel', language)}",
                callback_data="staff_back_to_main"
            )]
        ])
