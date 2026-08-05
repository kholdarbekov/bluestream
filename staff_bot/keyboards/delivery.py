"""
Delivery-related keyboards for Staff Bot
"""
from decimal import Decimal, InvalidOperation
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import List, Dict, Optional

from staff_bot.i18n import i18n
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS, FAILED_DELIVERY_REASONS
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import format_currency, format_quantity


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
                f"🔄 {i18n.get('staff.delivery.optimize_routes_button', language)}",
                callback_data="staff_optimize_routes",
            )
        ]]
        if show_share_location:
            keyboard.append([
                InlineKeyboardButton(
                    f"📍 {i18n.get('staff.delivery.share_location_button', language)}",
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
                emoji = "❌"
            else:
                emoji = {
                    'picked_up': '📦',
                    'in_transit': '🚚',
                    'arrived': '📍',
                    'delivered': '✅',
                }.get(next_status, '➡️')

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

    # ------------------------------------------------------------------
    # Driver cash handoff — ONE DECISION. Do not split this in two.
    # ------------------------------------------------------------------
    HANDOFF_ALL_CALLBACK = "staff_reconcile_submit_all"

    @staticmethod
    def freeze_handoff_amount(remaining_amount) -> Optional[Decimal]:
        """Freeze the server's ``remaining_cash_to_submit`` at money precision.

        Returns ``None`` when there is nothing to hand off. That is not a
        formatting detail: a handoff button that names no amount has nothing to
        record, and the only way for the tap to acquire an amount would be to
        let the server re-derive one from live data at tap time — which is the
        defect this whole surface exists to prevent.
        """
        if remaining_amount is None:
            return None
        try:
            amount = Decimal(str(remaining_amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return amount if amount > Decimal("0.00") else None

    @staticmethod
    def format_handoff_amount(amount: Decimal) -> str:
        """Render the frozen figure WITHOUT rounding any of it away.

        Whole amounts keep today's ``120,000`` look; a fractional remainder is
        shown in full rather than silently dropped, because this string is the
        number the driver agrees to hand over and the number that is recorded.
        """
        if amount == amount.to_integral_value():
            return f"{amount:,.0f}"
        return f"{amount:,.2f}"

    @staticmethod
    def parse_handoff_callback(data) -> Optional[Decimal]:
        """Read back the figure the tapped handoff button displayed.

        The inverse of the callback minted in :meth:`reconciliation_actions`.
        Returns ``None`` for a button that carries no amount (one rendered
        before this became a frozen figure, or a malformed payload) — the
        caller must then refuse to write rather than let the amount be
        invented downstream.
        """
        if not data:
            return None
        prefix = f"{DeliveryKeyboards.HANDOFF_ALL_CALLBACK}:"
        text = str(data)
        if not text.startswith(prefix):
            return None
        try:
            amount = Decimal(text[len(prefix):]).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None
        return amount if amount > Decimal("0.00") else None

    @staticmethod
    def reconciliation_actions(
        language: str,
        can_submit: bool = True,
        remaining_amount: float = None,
    ) -> InlineKeyboardMarkup:
        """Actions for the driver's reconciliation session view.

        ONE DECISION — the handoff button's label and its callback payload are
        minted from a SINGLE frozen value, the ``remaining_cash_to_submit`` of
        the very session payload this screen was drawn from. The amount the
        driver reads and the amount the tap posts are the same object; they
        cannot drift apart on a later edit because there is only one of them.

        Do NOT restore an amount-less ``staff_reconcile_submit_all`` callback.
        That button posted ``{}``, which made the server recompute the handoff
        from live ``CashCollectionEvent``s at tap time — so a COD collection
        landing between the render and the tap was written into a cash-custody
        record for an amount the driver never saw (sweep #8: shown 120,000,
        recorded 150,000). If there is nothing to hand off there is no button;
        if there is, the button names its own amount.
        """
        keyboard = []
        if can_submit:
            frozen_amount = DeliveryKeyboards.freeze_handoff_amount(remaining_amount)
            if frozen_amount is not None:
                label = i18n.get(
                    'staff.delivery.handoff_remaining_cash',
                    language,
                    amount=DeliveryKeyboards.format_handoff_amount(frozen_amount),
                )
                keyboard.append([InlineKeyboardButton(
                    f"💵 {label}",
                    # The literal prefix stays inline so the static routing
                    # guard in tests/unit/test_staff_bot_routing_regressions.py
                    # keeps seeing this callback.
                    callback_data=f"staff_reconcile_submit_all:{frozen_amount}"
                )])
            keyboard.append([InlineKeyboardButton(
                f"✏️ {i18n.get('staff.delivery.edit_reconciliation_cash', language)}",
                callback_data="staff_reconcile_submit"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_back_to_main"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def cod_debtor_list(
        language: str, customers: List[Dict], page: int, total_pages: int
    ) -> InlineKeyboardMarkup:
        """Inline list of COD debtors — **USER ROWS ONLY** (owner ruling A7).

        A7, verbatim: *"in staff bot there won't be any 'office' row in debtors
        list. The debtors list only shows the users, and the office debt is
        included in each coworker's debt."* There is no 🏢 place row and no
        place screen behind it; the office's debt reaches the driver through
        every coworker's row instead (A6/R-A), composed by
        ``StaffService.paginate_cod_debtors_for_staff``.

        WHY THE ``row_type`` DISPATCH SURVIVES THE DELETION. A place row is
        keyed on ``place_group_id`` and carries NO ``id``, so reading ``c['id']``
        unconditionally crashed the WHOLE list with ``KeyError: 'id'`` as soon as
        one place group existed. The service no longer emits such a row, but a
        staff_bot newer than its business_app (the documented deploy-skew window)
        can still be handed one — so anything that is not a person row is SKIPPED
        rather than rendered, and never subscripted.

        Person rows keep the existing ``staff_cod_customer_<id>`` callback (same
        statement view as before) and gain a ``👥xN`` marker only when the row
        represents several linked accounts. Page flips go through
        ``staff_cod_list_page_<n>``.
        """
        keyboard = []
        for c in customers:
            # A7: no place doorway. Skip defensively — never subscript a row
            # whose family we do not render.
            if c.get('row_type') == 'place' or c.get('id') is None:
                continue
            first = c.get('first_name') or ''
            last = c.get('last_name') or ''
            # Single-line name + amount. Telegram inline buttons render on one
            # line and truncate, so the phone is NOT crammed here — it's shown
            # on the customer's statement page once a row is tapped.
            name = (f"{first} {last}".strip() or c.get('phone') or '—')[:40]
            try:
                cluster_size = int(c.get('cluster_member_count') or 1)
            except (TypeError, ValueError):
                cluster_size = 1
            if cluster_size > 1:
                # One person, several phone accounts — the amount is the sum
                # across them, so say so rather than looking like a wrong total.
                name = f"{name} 👥x{cluster_size}"
            amount = format_currency(c.get('total_outstanding_amount') or 0, language=language)
            keyboard.append([InlineKeyboardButton(
                f"👤 {name} — 💰 {amount}",
                callback_data=f"staff_cod_customer_{c['id']}"
            )])
        if total_pages > 1:
            keyboard.append(CommonKeyboards.pagination(language, page, total_pages, 'staff_cod_list'))
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_cash_hub"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def cod_statement_actions(
        language: str,
        customer_id: int,
        *,
        can_collect: bool = True,
        back_callback: str = "staff_back_to_main",
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
            callback_data=back_callback
        )])
        return InlineKeyboardMarkup(keyboard)

    # ------------------------------------------------------------------
    # Bottle return keyboards
    # ------------------------------------------------------------------

    @staticmethod
    def bottle_return_options(language: str, delivery_id: int, suggested_bottles: int) -> InlineKeyboardMarkup:
        """Options for bottle return during delivery completion.

        Anchored on the PLACE's current bottle balance (`suggested_bottles`) —
        the empties standing at this door, which at a shared workplace includes
        a coworker's. The backend clamps that anchor at 0 so "All N returned"
        can never offer a negative count:
        - balance > 0  → "All N returned" / "Enter count" / "None returned"
        - balance == 0 → "0 bottles returned" / "Enter count" (the "None returned"
          row would duplicate the zero default, so it is dropped).
        """
        enter_btn = InlineKeyboardButton(
            f"✏️ {i18n.get('staff.delivery.bottles_enter_count', language)}",
            callback_data=f"staff_bottles_custom_{delivery_id}",
        )
        if suggested_bottles > 0:
            return InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ {i18n.get('staff.delivery.bottles_all_returned', language, count=suggested_bottles)}",
                    callback_data=f"staff_bottles_full_{delivery_id}",
                )],
                [enter_btn],
                [InlineKeyboardButton(
                    f"❌ {i18n.get('staff.delivery.bottles_none_returned', language)}",
                    callback_data=f"staff_bottles_none_{delivery_id}",
                )],
            ])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ {i18n.get('staff.delivery.bottles_zero_returned', language)}",
                callback_data=f"staff_bottles_full_{delivery_id}",
            )],
            [enter_btn],
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
        """Select the PLACE for a standalone bottle collection.

        One button per distinct place, labelled with that place's balance —
        every member's empties at that door, not one account's slice. A shared
        place is marked 👥; an over-returned place reads ``(↩N)`` rather than a
        bare minus sign.

        The caller decides which places are listed; this only renders them (see
        ``BottleCollectionHandler._actionable_places``).
        """
        keyboard = []
        for addr in addresses:
            addr_id = addr.get('address_id')
            title = addr.get('address_title') or addr.get('full_address', '')[:30]
            place_balance = float(addr.get('place_balance') or 0)
            marker = ' 👥' if addr.get('is_grouped') else ''
            # `format_quantity`, not `int()`: int() truncates toward zero, so a
            # place at -0.5 survives the caller's `!= 0` filter and would be
            # labelled "(↩0)".
            count = (
                f"↩{format_quantity(abs(place_balance))}" if place_balance < 0
                else format_quantity(place_balance)
            )
            keyboard.append([InlineKeyboardButton(
                f"📍 {title}{marker} ({count})",
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

    @staticmethod
    def bottle_search_results(
        language: str, customers: List[Dict]
    ) -> InlineKeyboardMarkup:
        """Inline list of customers found via bottle-collection search.

        Reuses the existing ``staff_bottle_customer_<id>`` callback so tapping
        a row behaves identically to the old per-result 'View bottle balance'
        button — the simplification is purely visual (one message instead of
        N reply messages).
        """
        keyboard = []
        for c in customers[:10]:
            first = c.get('first_name') or ''
            last = c.get('last_name') or ''
            name = f"{first} {last}".strip() or c.get('phone') or '—'
            phone = c.get('phone') or ''
            label = f"👤 {name} — 📞 {phone}" if phone else f"👤 {name}"
            keyboard.append([InlineKeyboardButton(
                label,
                callback_data=f"staff_bottle_customer_{c['id']}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_cash_hub"
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def bottle_collection_qty_picker(
        language: str, customer_id: int, address_id: int, balance: int
    ) -> InlineKeyboardMarkup:
        """Inline numeric picker for collection quantity, capped at ``balance``.

        Layout: a row of 1–5, an optional row of 6–10 when balance allows, a
        prominent "All (N)" shortcut, and Cancel. Buttons replace the old text
        prompt to remove human typo errors when drivers enter quantities at a
        customer's door. The callback encodes the selected qty directly:
        ``staff_bottle_qty_<customer_id>_<address_id>_<qty>``.
        """
        balance = max(0, int(balance))
        cap = min(balance, 10)
        keyboard: List[List[InlineKeyboardButton]] = []

        if cap >= 1:
            row1 = [
                InlineKeyboardButton(
                    str(n),
                    callback_data=f"staff_bottle_qty_{customer_id}_{address_id}_{n}",
                )
                for n in range(1, min(cap, 5) + 1)
            ]
            keyboard.append(row1)
        if cap > 5:
            row2 = [
                InlineKeyboardButton(
                    str(n),
                    callback_data=f"staff_bottle_qty_{customer_id}_{address_id}_{n}",
                )
                for n in range(6, cap + 1)
            ]
            keyboard.append(row2)
        if balance >= 1:
            keyboard.append([InlineKeyboardButton(
                f"📦 {i18n.get('staff.delivery.collect_all', language)} ({balance})",
                callback_data=f"staff_bottle_qty_{customer_id}_{address_id}_{balance}",
            )])
        # `staff_flow_cancel` (not `staff_back_to_main`) so the global flow-cancel
        # handler also clears `pending_bottle_collection_flow` and stops the text
        # router from intercepting subsequent menu taps.
        keyboard.append([InlineKeyboardButton(
            f"❌ {i18n.get('staff.cancel', language)}",
            callback_data="staff_flow_cancel",
        )])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def bottle_collection_note_prompt(language: str) -> InlineKeyboardMarkup:
        """Inline buttons for the (optional) note step.

        The driver can either type a note as text or tap "Save without note"
        to finalize with empty notes. Cancel routes via the global
        ``staff_flow_cancel`` handler so the pending flow flag is wiped.
        """
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"💾 {i18n.get('staff.delivery.save_without_note', language)}",
                callback_data="staff_bottle_collect_save_no_note",
            )],
            [InlineKeyboardButton(
                f"❌ {i18n.get('staff.cancel', language)}",
                callback_data="staff_flow_cancel",
            )],
        ])

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
                    f"✅ {i18n.get('staff.delivery.transfer_confirm_button', language, qty=qty, sender=sender)}",
                    callback_data=f"staff_transfer_confirm_{transfer_id}_{qty}"
                ),
                InlineKeyboardButton(
                    f"✏️ {i18n.get('staff.delivery.transfer_custom_count_button', language)}",
                    callback_data=f"staff_transfer_custom_{transfer_id}"
                ),
            ])
        keyboard.append([InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}",
            callback_data="staff_cash_hub"
        )])
        return InlineKeyboardMarkup(keyboard)
