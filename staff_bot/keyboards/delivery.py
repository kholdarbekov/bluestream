"""
Delivery-related keyboards for Staff Bot
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Dict

from staff_bot.i18n import i18n
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS, FAILED_DELIVERY_REASONS


class DeliveryKeyboards:
    """Keyboards for delivery person flows"""

    @staticmethod
    def order_pool_item(language: str, delivery_id: int) -> InlineKeyboardMarkup:
        """View/Accept buttons for an order in the pool"""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"👀 {i18n.get('staff.delivery.view_details', language)}",
                callback_data=f"staff_view_order_{delivery_id}"
            ),
            InlineKeyboardButton(
                f"✅ {i18n.get('staff.delivery.accept', language)}",
                callback_data=f"staff_accept_order_{delivery_id}"
            )
        ]])

    @staticmethod
    def accept_confirm(language: str, delivery_id: int) -> InlineKeyboardMarkup:
        """Confirm order acceptance"""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.confirm', language)}",
                    callback_data=f"staff_confirm_accept_{delivery_id}"
                ),
                InlineKeyboardButton(
                    f"❌ {i18n.get('staff.cancel', language)}",
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
            f"✅ {i18n.get('staff.delivery.accept', language)}",
            callback_data=f"staff_accept_order_{delivery_id}"
        )]]

        if can_mark_preparing and order_id:
            keyboard.append([InlineKeyboardButton(
                f"🛠️ {i18n.get('staff.delivery.mark_preparing', language)}",
                callback_data=f"staff_mark_preparing_{order_id}"
            )])

        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data=back_callback
        )])

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def active_list_top_actions(
        language: str, *, show_share_location: bool
    ) -> InlineKeyboardMarkup:
        """Top-of-list actions for the My active deliveries hub.

        Includes the manual "Optimize routes" button and (when the driver's
        live location is missing/stale) a "Share location" button that
        triggers the reply keyboard prompt.
        """
        keyboard = [[
            InlineKeyboardButton(
                f"\U0001f504 {i18n.get('staff.delivery.optimize_routes_button', language)}",
                callback_data="staff_optimize_routes",
            )
        ]]
        if show_share_location:
            keyboard.append([
                InlineKeyboardButton(
                    f"\U0001f4cd {i18n.get('staff.delivery.share_location_button', language)}",
                    callback_data="staff_share_location_prompt",
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data="staff_back_to_main",
            )
        ])
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
            f"📍 {i18n.get('staff.delivery.navigate', language)}",
            callback_data=f"staff_navigate_{delivery_id}"
        )])

        # Back button
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
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
            f"⬅️ {i18n.get('staff.back', language)}",
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
                f"✅ {i18n.get('staff.delivery.confirm_cash', language, amount=f'{amount:,.0f}')}",
                callback_data=f"staff_cash_full_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"✏️ {i18n.get('staff.delivery.edit_cash', language)}",
                callback_data=f"staff_cash_partial_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"❌ {i18n.get('staff.delivery.no_cash_collected', language)}",
                callback_data=f"staff_cash_none_{delivery_id}"
            )],
        ])

    @staticmethod
    def reconciliation_actions(
        language: str,
        can_submit: bool = True,
        can_handoff: bool = True,
    ) -> InlineKeyboardMarkup:
        """Actions for the driver's reconciliation session view."""
        keyboard = []
        if can_submit:
            keyboard.append([InlineKeyboardButton(
                f"💵 {i18n.get('staff.delivery.submit_reconciliation', language)}",
                callback_data="staff_reconcile_submit"
            )])
        if can_handoff:
            keyboard.append([InlineKeyboardButton(
                f"📦 {i18n.get('staff.delivery.checkpoint_handoff', language)}",
                callback_data="staff_reconcile_transfer"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def cod_customer_result(language: str, customer_id: int) -> InlineKeyboardMarkup:
        """View COD debt details for a searched customer."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📜 {i18n.get('staff.delivery.view_cod_statement', language)}",
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
                f"💸 {i18n.get('staff.delivery.collect_full_cod', language)}",
                callback_data=f"staff_cod_collect_full_{customer_id}"
            )])
            keyboard.append([InlineKeyboardButton(
                f"✏️ {i18n.get('staff.delivery.collect_custom_cod', language)}",
                callback_data=f"staff_cod_collect_custom_{customer_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    # ------------------------------------------------------------------
    # Bottle return keyboards
    # ------------------------------------------------------------------

    @staticmethod
    def bottle_return_options(language: str, delivery_id: int, expected_bottles: int) -> InlineKeyboardMarkup:
        """Options for bottle return during delivery completion."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ {i18n.get('staff.delivery.bottles_all_returned', language, count=expected_bottles)}",
                callback_data=f"staff_bottles_full_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"✏️ {i18n.get('staff.delivery.bottles_enter_count', language)}",
                callback_data=f"staff_bottles_custom_{delivery_id}"
            )],
            [InlineKeyboardButton(
                f"❌ {i18n.get('staff.delivery.bottles_none_returned', language)}",
                callback_data=f"staff_bottles_none_{delivery_id}"
            )],
        ])

    @staticmethod
    def bottle_customer_result(language: str, customer_id: int) -> InlineKeyboardMarkup:
        """View bottle balance for a searched customer."""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📊 {i18n.get('staff.delivery.view_bottle_balance', language)}",
                callback_data=f"staff_bottle_customer_{customer_id}"
            )
        ]])

    @staticmethod
    def bottle_address_selection(
        language: str, customer_id: int, addresses: list
    ) -> InlineKeyboardMarkup:
        """Select address for standalone bottle collection."""
        keyboard = []
        for addr in addresses:
            addr_id = addr.get('address_id')
            title = addr.get('address_title') or addr.get('full_address', '')[:30]
            balance = addr.get('balance', 0)
            keyboard.append([InlineKeyboardButton(
                f"📍 {title} ({int(balance)})",
                callback_data=f"staff_bottle_addr_{customer_id}_{addr_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def bottle_statement_actions(
        language: str, customer_id: int, address_id: int, *, can_collect: bool = True
    ) -> InlineKeyboardMarkup:
        """Actions on a customer's bottle balance at an address."""
        keyboard = []
        if can_collect:
            keyboard.append([InlineKeyboardButton(
                f"📦 {i18n.get('staff.delivery.collect_bottles', language)}",
                callback_data=f"staff_bottle_collect_{customer_id}_{address_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⚠️ {i18n.get('staff.delivery.issue_bottle_fine', language)}",
            callback_data=f"staff_bottle_fine_{customer_id}_{address_id}"
        )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    # ------------------------------------------------------------------
    # Session & Transfer keyboards
    # ------------------------------------------------------------------

    @staticmethod
    def bottle_session_menu(language: str) -> InlineKeyboardMarkup:
        """Main bottle session action menu."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📦 {i18n.get('staff.menu.log_bottles_loaded', language)}",
                callback_data="staff_bottle_session_load"
            )],
            [InlineKeyboardButton(
                f"🏢 {i18n.get('staff.menu.return_to_warehouse', language)}",
                callback_data="staff_bottle_session_return"
            )],
            [InlineKeyboardButton(
                f"🔄 {i18n.get('staff.menu.transfer_bottles_to_driver', language)}",
                callback_data="staff_bottle_transfer_start"
            )],
            [InlineKeyboardButton(
                f"📥 {i18n.get('staff.menu.incoming_transfers', language)}",
                callback_data="staff_bottle_transfers_pending"
            )],
            [InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data="staff_cash_hub"
            )],
        ])

    @staticmethod
    def driver_select_for_transfer(language: str, drivers: List[Dict]) -> InlineKeyboardMarkup:
        """Select a driver to transfer bottles to."""
        keyboard = []
        for driver in drivers[:10]:  # cap at 10 to avoid overflow
            driver_id = driver.get('id') or driver.get('user_id')
            name = driver.get('name') or driver.get('full_name', 'Driver')
            keyboard.append([InlineKeyboardButton(
                f"👤 {name}",
                callback_data=f"staff_transfer_driver_{driver_id}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_cash_hub"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def pending_transfer_list(language: str, transfers: List[Dict]) -> InlineKeyboardMarkup:
        """Confirm / enter different count for each pending transfer."""
        keyboard = []
        for t in transfers[:5]:
            transfer_id = t.get('id')
            qty = t.get('declared_quantity', 0)
            sender = (t.get('sender_name') or 'Driver')[:15]
            keyboard.append([
                InlineKeyboardButton(
                    f"✅ Confirm {qty} from {sender}",
                    callback_data=f"staff_transfer_confirm_{transfer_id}_{qty}"
                ),
                InlineKeyboardButton(
                    "✏️ Different count",
                    callback_data=f"staff_transfer_custom_{transfer_id}"
                ),
            ])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_cash_hub"
        )])
        return InlineKeyboardMarkup(keyboard)
