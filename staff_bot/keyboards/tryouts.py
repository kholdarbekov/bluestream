"""Inline keyboards for try-out task workflows in the staff bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from i18n import i18n


class TryoutKeyboards:
    """Keyboard helpers for try-out workflows."""

    @staticmethod
    def product_list(language: str, products: list) -> InlineKeyboardMarkup:
        keyboard = []
        for product in products:
            suffix = " ♻️" if product.get('tracks_returnable_bottles') else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{product.get('name')}{suffix}",
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
    def quantity_selection(language: str, product_id: int) -> InlineKeyboardMarkup:
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
                f"👁️ {i18n.get('staff.tryout.view_tryout', language)}",
                callback_data=f"staff_tryout_view_{task.get('tryout_id')}"
            )
        ])
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def tryout_actions(language: str, tryout_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"📋 {i18n.get('staff.tryout.open_tasks', language)}",
                    callback_data="staff_tryout_tasks"
                ),
                InlineKeyboardButton(
                    f"⬅️ {i18n.get('staff.back', language)}",
                    callback_data="staff_back_to_main"
                ),
            ],
            [
                InlineKeyboardButton(
                    f"🔄 {i18n.get('staff.tryout.view_tryout', language)}",
                    callback_data=f"staff_tryout_view_{tryout_id}"
                )
            ],
        ])
