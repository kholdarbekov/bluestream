"""Inline keyboards for try-out task workflows in the staff bot."""

from decimal import Decimal
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.i18n import i18n
from staff_bot.utils.formatters import format_quantity


class TryoutKeyboards:
    """Keyboard helpers for try-out workflows."""

    @staticmethod
    def product_list(language: str, products: list, selected_quantities: Optional[dict] = None) -> InlineKeyboardMarkup:
        selected_quantities = selected_quantities or {}
        keyboard = []
        for product in products:
            suffix = " ♻️" if product.get('tracks_returnable_bottles') else ""
            selected = selected_quantities.get(int(product['id']), 0)
            selected_suffix = f" · x{selected}" if selected else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{product.get('name')}{suffix}{selected_suffix}",
                    callback_data=f"staff_tryout_product_{product['id']}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                f"✅ {i18n.get('staff.tryout.done_selecting', language)}",
                callback_data="staff_tryout_products_done"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data="staff_back_to_main"
            )
        ])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def quantity_selection(language: str, product_id: int, selected_quantity: int = 0) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(str(quantity), callback_data=f"staff_tryout_qty_{product_id}_{quantity}")
                for quantity in [1, 2, 3, 4, 5]
            ],
            [
                InlineKeyboardButton(str(quantity), callback_data=f"staff_tryout_qty_{product_id}_{quantity}")
                for quantity in [6, 8, 10, 15, 20]
            ],
            [
                InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.back', language)}",
                    callback_data="staff_tryout_select_products"
                )
            ],
        ]
        if selected_quantity > 0:
            rows.insert(2, [
                InlineKeyboardButton(
                    f"🗑 {i18n.get('staff.tryout.remove_product', language)}",
                    callback_data=f"staff_tryout_remove_{product_id}"
                )
            ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def create_summary(language: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"➕ {i18n.get('staff.tryout.add_more_products', language)}",
                    callback_data="staff_tryout_select_products"
                ),
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.confirm', language)}",
                    callback_data="staff_tryout_confirm_create"
                )
            ],
            [
                InlineKeyboardButton(
                    f"❌ {i18n.get('staff.cancel', language)}",
                    callback_data="staff_back_to_main"
                )
            ]
        ])

    @staticmethod
    def task_actions(language: str, task: dict) -> InlineKeyboardMarkup:
        buttons = []
        task_id = task.get('id')
        task_type = task.get('task_type')
        task_status = task.get('status')
        assigned_driver_user_id = task.get('assigned_driver_user_id')

        if task_status == 'open' and not assigned_driver_user_id:
            buttons.append([
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.tryout.accept_task', language)}",
                    callback_data=f"staff_tryout_accept_{task_id}"
                )
            ])

        if task_type == 'handoff' and task_status in {'open', 'assigned'}:
            buttons.append([
                InlineKeyboardButton(
                    f"📦 {i18n.get('staff.tryout.complete_handoff', language)}",
                    callback_data=f"staff_tryout_handoff_{task_id}"
                )
            ])

        if task_type == 'pickup' and task_status in {'open', 'assigned'}:
            buttons.append([
                InlineKeyboardButton(
                    f"♻️ {i18n.get('staff.tryout.record_pickup', language)}",
                    callback_data=f"staff_tryout_pickup_{task_id}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                f"👀 {i18n.get('staff.tryout.view_tryout', language)}",
                callback_data=f"staff_tryout_view_{task.get('tryout_id')}"
            )
        ])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def tryout_actions(
        language: str,
        tryout_id: int,
        pickup_task_id: Optional[int] = None,
        back_callback: str = "staff_back_to_main",
    ) -> InlineKeyboardMarkup:
        rows = []
        if pickup_task_id is not None:
            rows.append([
                InlineKeyboardButton(
                    f"♻️ {i18n.get('staff.tryout.record_pickup', language)}",
                    callback_data=f"staff_tryout_pickup_{pickup_task_id}"
                )
            ])
        rows.append([
            InlineKeyboardButton(
                f"📋 {i18n.get('staff.tryout.open_tasks', language)}",
                callback_data="staff_tryout_tasks"
            ),
            InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data=back_callback
            ),
        ])
        rows.append([
            InlineKeyboardButton(
                f"🔄 {i18n.get('staff.tryout.view_tryout', language)}",
                callback_data=f"staff_tryout_view_{tryout_id}"
            )
        ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def pickup_overview(language: str, state: dict) -> InlineKeyboardMarkup:
        task_id = int(state['task_id'])
        selected = state.get('selected', {})
        keyboard = []

        for product in state.get('products', []):
            product_id = int(product['product_id'])
            outstanding = format_quantity(product.get('units'))
            selected_units = format_quantity(selected.get(product_id, 0))
            keyboard.append([
                InlineKeyboardButton(
                    (
                        f"{product.get('product_name')} · "
                        f"{selected_units}/{outstanding}"
                    ),
                    callback_data=f"staff_tryout_pickup_edit_{task_id}_{product_id}"
                )
            ])

        if selected:
            keyboard.append([
                InlineKeyboardButton(
                    f"✅ {i18n.get('staff.tryout.pickup_submit', language)}",
                    callback_data=f"staff_tryout_pickup_submit_{task_id}"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    f"🧹 {i18n.get('staff.tryout.pickup_clear_selection', language)}",
                    callback_data=f"staff_tryout_pickup_clearall_{task_id}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                f"♻️ {i18n.get('staff.tryout.pickup_fill_all', language)}",
                callback_data=f"staff_tryout_pickup_all_{task_id}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data="staff_tryout_tasks"
            )
        ])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def pickup_quantity_selection(
        language: str,
        task_id: int,
        product_id: int,
        outstanding_units,
        selected_units=0,
    ) -> InlineKeyboardMarkup:
        outstanding = Decimal(str(outstanding_units or 0))
        candidates = []
        for value in (1, 2, 3, 4, 5, 6, 8, 10, 15, 20):
            candidate = Decimal(str(value))
            if candidate < outstanding:
                candidates.append(candidate)

        if outstanding > 0:
            candidates.append(outstanding)

        # Preserve order while removing duplicates.
        seen = set()
        unique_candidates = []
        for candidate in candidates:
            key = format_quantity(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)

        rows = []
        current_row = []
        for candidate in unique_candidates:
            label = format_quantity(candidate)
            if candidate == outstanding:
                label = f"All ({label})"
            current_row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=(
                        f"staff_tryout_pickup_qty_{task_id}_{product_id}_"
                        f"{int((candidate * 100).to_integral_value())}"
                    )
                )
            )
            if len(current_row) == 3:
                rows.append(current_row)
                current_row = []

        if current_row:
            rows.append(current_row)

        if Decimal(str(selected_units or 0)) > 0:
            rows.append([
                InlineKeyboardButton(
                    f"🗑 {i18n.get('staff.tryout.pickup_clear_product', language)}",
                    callback_data=f"staff_tryout_pickup_clear_{task_id}_{product_id}"
                )
            ])

        rows.append([
            InlineKeyboardButton(
                f"⬅️ {i18n.get('staff.back', language)}",
                callback_data=f"staff_tryout_pickup_back_{task_id}"
            )
        ])
        return InlineKeyboardMarkup(rows)
