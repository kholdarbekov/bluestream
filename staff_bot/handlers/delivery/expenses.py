"""Add expense: the driver's cash expense claim, one conversation (spec 2026-11-17).

Cash hub -> category -> amount -> a screen where a note and/or a receipt photo may be added -> Send for approval.
The draft lives in ONE `user_data` key (`flow_state.EXPENSE_FLOW_KEY`), so a menu tap, a timeout or a Cancel drops
all of it, and every button the flow ever drew that is tapped afterwards is refused out loud (`_live_draft`).

The backend decides everything that is money (the cap, the trip, the category and note rules); this module only
refuses what it can know on the spot (a typed number that is not an amount, or above the cash he holds) so the
driver does not wait for a round trip, and never posts a claim the screen did not show as complete.
"""
import logging
import uuid
from decimal import Decimal
from html import escape
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES, DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES
from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils.flow_state import EXPENSE_FLOW_KEY
from staff_bot.utils.formatters import format_currency
from staff_bot.utils.money_input import parse_typed_money

logger = logging.getLogger(__name__)

EXPENSE_CATEGORY, EXPENSE_AMOUNT, EXPENSE_CONFIRM = 130, 131, 132


class ExpenseHandler(BaseHandler):
    """The Add expense conversation."""

    # What the conversation's own timeout clears (it is not displaceable today, but the timeout factory
    # takes the keys so it never drops a sibling flow's draft).
    FLOW_KEYS = (EXPENSE_FLOW_KEY,)

    # -- helpers -----------------------------------------------------------------------------------------

    @staticmethod
    def _needs_note(draft: dict) -> bool:
        return draft.get('category') in DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES

    @classmethod
    def _can_send(cls, draft: dict) -> bool:
        """A claim is complete with a category and an amount, plus a note where the category demands one."""
        if draft.get('category') is None or draft.get('amount') is None:
            return False
        return bool(draft.get('note')) or not cls._needs_note(draft)

    async def _live_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str, *,
                          complete: bool) -> Optional[dict]:
        """The driver's draft if this tap or message still belongs to it; otherwise refuse and return None.

        Gone means a timeout, a menu tap or a Send already took it. A tap is then a stale button in the
        scrollback: one toast and the message becomes the Cash hub. For Send (`complete=True`) a draft without
        a category and an amount is just as dead -- a button from an earlier draft, tapped while a new one is
        still at its category picker. Nothing is posted either way.
        """
        draft = context.user_data.get(EXPENSE_FLOW_KEY)
        if isinstance(draft, dict) and (
            not complete or (draft.get('category') is not None and draft.get('amount') is not None)
        ):
            return draft
        if update.callback_query is not None:
            await self._refuse_stale_tap(update, language, reply_markup=MenuKeyboards.cash_hub(language))
        else:
            await self._answer_gone_flow(update, context, EXPENSE_FLOW_KEY)
        return None

    @staticmethod
    def _category_label(language: str, category: str) -> str:
        emoji = DeliveryKeyboards.EXPENSE_CATEGORY_EMOJI[category]
        return f"{emoji} {i18n.get(f'staff.expense.category.{category}', language)}"

    def _confirm_text(self, language: str, draft: dict) -> str:
        lines = [
            f"<b>{i18n.get('staff.expense.new_title', language)}</b>",
            f"{self._category_label(language, draft['category'])} · "
            f"{format_currency(draft['amount'], language=language)}",
        ]
        if draft.get('note'):
            lines.append(f"📝 {i18n.get('staff.expense.note_label', language)}: {escape(draft['note'])}")
        if draft.get('receipt_file_id'):
            lines.append(f"📎 {i18n.get('staff.expense.receipt_attached', language)}")
        lines.append("")
        if self._needs_note(draft) and not draft.get('note'):
            lines.append(i18n.get('staff.error.api.expense_note_required', language))
        elif not (draft.get('note') and draft.get('receipt_file_id')):
            lines.append(i18n.get('staff.expense.add_note_or_photo', language))
        return "\n".join(lines)

    async def _show_confirm(self, update: Update, language: str, draft: dict, *, new_key: bool = True) -> int:
        """Draw the draft's screen. A draft that just changed gets a new idempotency key; one that is only
        redrawn keeps its own, so a re-tap of Send after a lost answer is the same claim."""
        if new_key:
            draft['idempotency_key'] = uuid.uuid4().hex
        await update.effective_message.reply_text(
            self._confirm_text(language, draft),
            reply_markup=DeliveryKeyboards.expense_confirm(language, self._can_send(draft)),
            parse_mode='HTML',
        )
        return EXPENSE_CONFIRM

    # -- conversation steps ------------------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_expense(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cash hub -> Add expense: read the trip and the cash he holds, then ask what he paid for."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # A fresh claim: nothing an earlier, abandoned draft held carries over.
        context.user_data.pop(EXPENSE_FLOW_KEY, None)

        async with api_client as client:
            response = await client.get_expense_options(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return ConversationHandler.END

        options = response.data or {}
        try:
            max_amount = float(options.get('max_amount') or 0)
        except (TypeError, ValueError):
            max_amount = 0.0
        if not options.get('has_active_trip') or max_amount <= 0:
            await query.edit_message_text(
                i18n.get('staff.expense.no_trip_cash', language),
                reply_markup=MenuKeyboards.cash_hub(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        context.user_data[EXPENSE_FLOW_KEY] = {
            "max_amount": max_amount,
            "category": None,
            "amount": None,
            "note": None,
            "receipt_file_id": None,
            "idempotency_key": None,
        }
        held = i18n.get(
            'staff.expense.cash_held_now', language, amount=format_currency(max_amount, language=language)
        )
        await query.edit_message_text(
            f"<b>{i18n.get('staff.expense.new_title', language)}</b>\n{held}\n\n"
            f"{i18n.get('staff.expense.pick_category', language)}",
            reply_markup=DeliveryKeyboards.expense_categories(language),
            parse_mode='HTML',
        )
        return EXPENSE_CATEGORY

    @require_auth
    @require_delivery_driver
    async def pick_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A category button. Picking one starts the claim over: what was typed for another category is dropped."""
        query = update.callback_query
        language = await self._get_language(update, context)
        draft = await self._live_draft(update, context, language, complete=False)
        if draft is None:
            return ConversationHandler.END
        category = (query.data or "")[len(DeliveryKeyboards.EXPENSE_CATEGORY_CALLBACK_PREFIX):]
        if category not in DRIVER_EXPENSE_CATEGORIES:
            await query.answer()
            await self._handle_error(update, context)
            return EXPENSE_CATEGORY
        await query.answer()

        draft.update(category=category, amount=None, note=None, receipt_file_id=None, idempotency_key=None)
        await query.edit_message_text(
            f"{self._category_label(language, category)}\n\n{i18n.get('staff.expense.enter_amount', language)}",
            parse_mode='HTML',
        )
        return EXPENSE_AMOUNT

    @require_auth
    @require_delivery_driver
    async def receive_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The typed amount: a positive number, no more than the cash he holds."""
        language = await self._get_language(update, context)
        draft = await self._live_draft(update, context, language, complete=False)
        if draft is None:
            return ConversationHandler.END
        try:
            amount = parse_typed_money(update.message.text)
        except (TypeError, ValueError):
            amount = Decimal(0)
        if amount <= 0:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_amount', language), parse_mode='HTML')
            return EXPENSE_AMOUNT
        if amount > Decimal(str(draft['max_amount'])):
            await update.message.reply_text(
                i18n.get(
                    'staff.expense.above_cash_held', language,
                    amount=format_currency(draft['max_amount'], language=language),
                ),
                parse_mode='HTML',
            )
            return EXPENSE_AMOUNT

        draft['amount'] = float(amount)
        return await self._show_confirm(update, language, draft)

    @require_auth
    @require_delivery_driver
    async def receive_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A typed note on the confirm screen."""
        language = await self._get_language(update, context)
        draft = await self._live_draft(update, context, language, complete=True)
        if draft is None:
            return ConversationHandler.END
        note = (update.message.text or "").strip()
        if not note:
            return EXPENSE_CONFIRM
        draft['note'] = note
        return await self._show_confirm(update, language, draft)

    @require_auth
    @require_delivery_driver
    async def receive_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A receipt photo on the confirm screen; its caption becomes the note."""
        language = await self._get_language(update, context)
        draft = await self._live_draft(update, context, language, complete=True)
        if draft is None:
            return ConversationHandler.END
        draft['receipt_file_id'] = update.message.photo[-1].file_id
        caption = (update.message.caption or "").strip()
        if caption:
            draft['note'] = caption
        return await self._show_confirm(update, language, draft)

    @require_auth
    @require_delivery_driver
    async def send_expense(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send for approval: post the claim exactly as the screen showed it."""
        query = update.callback_query
        language = await self._get_language(update, context)
        draft = await self._live_draft(update, context, language, complete=True)
        if draft is None:
            return ConversationHandler.END
        await query.answer()
        if not self._can_send(draft):
            # An old Send tapped on a draft that still lacks its required note: show the screen again.
            return await self._show_confirm(update, language, draft, new_key=False)

        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        payload = {
            "category": draft['category'],
            "amount": draft['amount'],
            "note": draft['note'],
            "receipt_file_id": draft['receipt_file_id'],
            "idempotency_key": draft['idempotency_key'],
        }
        async with api_client as client:
            response = await client.create_expense(token, payload)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return EXPENSE_CONFIRM

        context.user_data.pop(EXPENSE_FLOW_KEY, None)
        answer = response.data or {}
        category_label = i18n.get(f"staff.expense.category.{draft['category']}", language)
        sent = i18n.get(
            'staff.expense.sent', language,
            category=category_label, amount=format_currency(draft['amount'], language=language),
        )
        lines = [f"✅ {sent}", i18n.get('staff.expense.sent_explainer', language)]
        if answer.get('hand_in_now') is not None and answer.get('expenses_waiting_total') is not None:
            lines.append("")
            lines.append(i18n.get(
                'staff.expense.hand_in_now_waiting', language,
                amount=format_currency(answer['hand_in_now'], language=language),
                waiting=format_currency(answer['expenses_waiting_total'], language=language),
            ))
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=DeliveryKeyboards.expense_sent(language),
            parse_mode='HTML',
        )
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def cancel_expense(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel: drop the draft and show the Cash hub."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        context.user_data.pop(EXPENSE_FLOW_KEY, None)
        await query.edit_message_text(
            i18n.get('staff.cancelled', language),
            reply_markup=MenuKeyboards.cash_hub(language),
            parse_mode='HTML',
        )
        return ConversationHandler.END


    # -- My expenses -------------------------------------------------------------------------------------

    def _list_text(self, language: str, data: dict) -> str:
        """The list: waiting claims first, then the decided ones. Each row is its category, amount and status."""
        title = f"<b>{i18n.get('staff.expense.my_expenses', language)}</b>"
        waiting, decided = data.get('waiting') or [], data.get('decided') or []
        if not waiting and not decided:
            return f"{title}\n\n{i18n.get('staff.expense.none', language)}"

        def line(claim: dict) -> str:
            amount = format_currency(claim['amount'], language=language)
            approved = claim.get('approved_amount')
            if claim['status'] == 'approved' and approved is not None and approved != claim['amount']:
                amount = f"{format_currency(approved, language=language)} / {amount}"
            status = i18n.get(f"staff.expense.status.{claim['status']}", language)
            return f"{self._category_label(language, claim['category'])} · {amount} · {status}"

        lines = [title]
        for header_key, claims in (('staff.expense.waiting_header', waiting), ('staff.expense.decided_header', decided)):
            if claims:
                lines.append("")
                lines.append(f"<b>{i18n.get(header_key, language)}</b>")
                lines.extend(line(claim) for claim in claims)
        return "\n".join(lines)

    async def _read_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str) -> Optional[dict]:
        """The driver's claims from the backend, or None after the refusal has been shown."""
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return None
        async with api_client as client:
            response = await client.list_my_expenses(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return None
        return response.data or {}

    async def _draw_list(self, update: Update, language: str, data: dict) -> None:
        await update.callback_query.edit_message_text(
            self._list_text(language, data),
            reply_markup=DeliveryKeyboards.my_expenses(language, data.get('waiting') or []),
            parse_mode='HTML',
        )

    @require_auth
    @require_delivery_driver
    async def show_my_expenses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cash hub -> My expenses: the waiting claims (each with Withdraw) and the last decided ones."""
        await update.callback_query.answer()
        language = await self._get_language(update, context)
        data = await self._read_list(update, context, language)
        if data is not None:
            await self._draw_list(update, language, data)

    @require_auth
    @require_delivery_driver
    async def ask_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Withdraw on a waiting claim: ask before posting. A claim that is no longer waiting redraws the list."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        expense_id = int(query.data.rsplit('_', 1)[-1])
        data = await self._read_list(update, context, language)
        if data is None:
            return
        claim = next((row for row in data.get('waiting') or [] if row['id'] == expense_id), None)
        if claim is None:
            await self._draw_list(update, language, data)
            return
        await query.edit_message_text(
            i18n.get(
                'staff.expense.withdraw_confirm', language,
                category=i18n.get(f"staff.expense.category.{claim['category']}", language),
                amount=format_currency(claim['amount'], language=language),
            ),
            reply_markup=DeliveryKeyboards.expense_withdraw_confirm(language, expense_id),
            parse_mode='HTML',
        )

    @require_auth
    @require_delivery_driver
    async def confirm_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Yes: take the claim back; the screen then says what he hands in now."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        expense_id = int(query.data.rsplit('_', 1)[-1])
        async with api_client as client:
            response = await client.withdraw_expense(token, expense_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return
        answer = response.data or {}
        await query.edit_message_text(
            i18n.get(
                'staff.expense.withdrawn', language,
                amount=format_currency(answer.get('hand_in_now') or 0, language=language),
            ),
            reply_markup=DeliveryKeyboards.expense_sent(language),
            parse_mode='HTML',
        )
