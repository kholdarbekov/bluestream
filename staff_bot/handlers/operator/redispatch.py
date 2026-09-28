"""
Failed Delivery Handler for Staff Bot (operators / dispatchers).

Lists the deliveries that failed and are waiting for a new date, and lets an
operator give one a date (F9, F12 of
docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md).
The re-date is `OrderScheduleService.reschedule` on the backend. It lands the
delivery in today's pool, or holds it until its day's first shift (R25 of
docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md), and the
operator is told which.

The flow is stateless. The card a tap came from rides in its callback data
(`origin`: 0 a list card, 1 the failure alert), and every tap that draws dates
asks the backend for that delivery's bounds first. Nothing is read from the
card, the alert or `user_data`, and the bot never reads its own clock to pick a
date, so a card drawn before midnight offers the backend's new today.
"""
import logging
from datetime import date, datetime
from typing import List, Optional, Tuple

from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from shared.enums import DeliveryStatus
from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.operator import OperatorKeyboards
from staff_bot.utils.formatters import (
    escape_html,
    format_currency,
    format_fail_reason,
    format_local_date,
    format_local_time,
)
from staff_bot.permissions import require_auth, require_operator
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)

# The list shows this many cards and says how many it left out (F9).
LIST_CARD_LIMIT = 15
# The backend refused because the delivery changed under the card. Anything
# else (5xx, transport, rate limit) leaves the card to be tapped again.
CARD_REFUSAL_STATUSES = frozenset({400, 404, 409})


class RedispatchHandler(BaseHandler):
    """Operator flow that gives failed deliveries a new date."""

    @require_auth
    @require_operator
    async def show_failed_deliveries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The menu entry: the list's header replaces the menu message."""
        await self._show_list(update, context, edit_in_place=True)

    @require_auth
    @require_operator
    async def show_failed_deliveries_from_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """[📋 All failed deliveries] on a failure alert: the list as NEW messages.

        Never an edit, so the alert stays the record of what failed.
        """
        await self._show_list(update, context, edit_in_place=False)

    async def _show_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit_in_place: bool):
        """Show failed deliveries, each card with its [📅 Pick a new date]."""
        query = update.callback_query
        language = await self._get_language(update, context)
        if query:
            # Stop the spinner now. An alert's button would otherwise spin until
            # Telegram gives up, and a refusal below becomes a chat message.
            await self._safe_callback_answer(query, None, show_alert=False)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_failed_deliveries(token)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return

            data = response.data
            if isinstance(data, list):
                deliveries, total = data, len(data)
            else:
                deliveries = (data or {}).get('items', [])
                # The backend's real count, not the length of its capped page (F9).
                total = (data or {}).get('total', len(deliveries))

            target = query.message if query else update.message

            if not deliveries:
                text = f"✅ {i18n.get('staff.redispatch.none', language)}"
                keyboard = CommonKeyboards.back_button(language)
                if query and edit_in_place:
                    await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                else:
                    await target.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            shown = deliveries[:LIST_CARD_LIMIT]
            header_lines = [
                f"🔄 <b>{i18n.get('staff.redispatch.title', language)}</b>",
                i18n.get('staff.redispatch.pick', language),
            ]
            if total > len(shown):
                header_lines.append(
                    i18n.get('staff.redispatch.showing', language, shown=len(shown), total=total)
                )
            header = '\n'.join(header_lines)
            if query and edit_in_place:
                await query.edit_message_text(header, parse_mode='HTML')
            else:
                await target.reply_text(header, parse_mode='HTML')

            for d in shown:
                await target.reply_text(
                    self._card_text(d, language),
                    parse_mode='HTML',
                    reply_markup=OperatorKeyboards.redispatch_card(d.get('delivery_id'), 0, language),
                )

        except Exception as e:
            logger.error(f"Error showing failed deliveries: {e}", exc_info=True)
            await self._handle_error(update, context)

    @staticmethod
    def _card_text(delivery: dict, language: str) -> str:
        """One list card: the order, the customer, and why and when it failed (F9)."""
        order_num = escape_html(delivery.get('order_number') or i18n.get('staff.common.not_available', language))
        customer = escape_html(delivery.get('customer_name', ''))
        phone = escape_html(delivery.get('customer_phone') or '')
        address = escape_html(delivery.get('address', ''))
        total = format_currency(delivery.get('total_amount'), language=language)
        reason = delivery.get('failed_delivery_reason') or ''
        driver = escape_html(delivery.get('driver_name') or '')
        failed_at = format_local_date(delivery.get('failed_at'), '%d.%m %H:%M')
        attempts = delivery.get('delivery_attempts') or 0

        lines = [f"📦 <b>#{order_num}</b>"]
        if customer:
            lines.append(f"👤 {customer}")
        if phone:
            lines.append(f"📞 {phone}")
        if address:
            lines.append(f"📍 {address}")
        lines.append(f"💰 {total}")
        if reason:
            lines.append(f"❗ {format_fail_reason(reason, language)}")
        if driver:
            lines.append(f"🚚 {i18n.get('staff.redispatch.driver', language)}: {driver}")
        if failed_at:
            lines.append(f"🕒 {i18n.get('staff.redispatch.failed_at', language)}: {failed_at}")
        lines.append(f"🔁 {i18n.get('staff.redispatch.attempts', language)}: {attempts}")
        return '\n'.join(lines)

    @require_auth
    @require_operator
    async def show_date_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """[📅 Pick a new date]: the card's keyboard becomes the date step (F12).

        Only the keyboard changes, so the card keeps naming the order. A bare
        `staff_redispatch_do_<id>` is a list card drawn before this step existed.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        await self._safe_callback_answer(query, None, show_alert=False)
        try:
            parts = self._callback_parts(query.data, 'staff_redispatch_do_')
            delivery_id = int(parts[0])
            origin = int(parts[1]) if len(parts) > 1 else 0
            bounds = await self._published_bounds(update, context, delivery_id, language)
            if bounds is not None:
                await self._swap_keyboard(
                    query, OperatorKeyboards.redispatch_date_step(delivery_id, origin, *bounds, language)
                )
        except Exception as e:
            logger.error(f"Error showing the re-date step: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def show_day_grid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """[🗓 Pick a day]: every day the backend allows, asked for again (F12)."""
        query = update.callback_query
        language = await self._get_language(update, context)
        await self._safe_callback_answer(
            query, i18n.get('staff.redispatch.pick_day_prompt', language), show_alert=False
        )
        try:
            delivery_id, origin = (int(part) for part in self._callback_parts(query.data, 'staff_redispatch_pick_'))
            bounds = await self._published_bounds(update, context, delivery_id, language)
            if bounds is not None:
                await self._swap_keyboard(
                    query, OperatorKeyboards.redispatch_day_grid(delivery_id, origin, *bounds, language)
                )
        except Exception as e:
            logger.error(f"Error showing the re-date day grid: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def restore_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """[⬅️ Back] on the date step: the card's own keyboard again, with no fetch."""
        query = update.callback_query
        language = await self._get_language(update, context)
        await self._safe_callback_answer(query, None, show_alert=False)
        try:
            delivery_id, origin = (int(part) for part in self._callback_parts(query.data, 'staff_redispatch_back_'))
            await self._swap_keyboard(query, OperatorKeyboards.redispatch_card(delivery_id, origin, language))
        except Exception as e:
            logger.error(f"Error restoring the failed-delivery card: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def redispatch_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A date tap: re-date the failed delivery to the day on the button (F12).

        Posts that day and nothing else. The backend judges it again under its
        locks, so a day that stopped being allowed after the step was drawn is
        refused with its own reason instead of re-dated. The reason arrives as
        a message under the card. So does the success reply: the card keeps
        naming the order and the customer, which is who its "contact them"
        line means, and loses only its buttons.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        await self._safe_callback_answer(query, None, show_alert=False)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            raw_id, raw_day = self._callback_parts(query.data, 'staff_redispatch_on_')
            delivery_id = int(raw_id)
            delivery_date = datetime.strptime(raw_day, '%Y%m%d').date()
        except (ValueError, AttributeError):
            await self._handle_error(update, context)
            return

        try:
            async with api_client as client:
                response = await client.redispatch_delivery(token, delivery_id, delivery_date)

            if not response.success:
                # Never `_refuse_on_card` here: this delivery may already be
                # re-dated. A double-tapped date posts again after the first
                # re-date committed (one chat's updates run one at a time) and is
                # refused as no longer failed. Editing that refusal over the card
                # would say the re-date failed and erase which order the success
                # reply under it is about.
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                else:
                    await self._handle_api_response_error(update, response, language)
                return
            text = self._success_text(response.data or {}, language)
        except Exception as e:
            logger.error(f"Error re-dating failed delivery: {e}", exc_info=True)
            await self._handle_error(update, context)
            return

        # PAST THIS POINT THE RE-DATE IS COMMITTED. A Telegram refusal of the redraw
        # must not share the POST's `except` (the convention in create_user.py): "an
        # error occurred" would say the re-date failed, and lose the customer line,
        # which may be the only thing telling the operator to contact the customer.
        back = CommonKeyboards.back_button(language)
        if isinstance(query.message, Message):
            await self._safe_reply_text(query.message, text, reply_markup=back)
        elif update.effective_chat:
            await self._safe_send_to_chat(update.effective_chat, text, reply_markup=back)
        # It has left the failed list, so none of the card's buttons can work.
        try:
            await self._swap_keyboard(query, None)
        except Exception as exc:  # noqa: BLE001 -- tidy-up only; the reply above is the answer
            logger.warning(
                "Delivery %s re-dated but its card's buttons could not be removed: %s", delivery_id, exc
            )

    @staticmethod
    def _success_text(landed: dict, language: str) -> str:
        """The day it landed on, when drivers get it, and whether the customer was told.

        Reads only what the backend published for THIS re-date. The customer
        notice was decided under its lock and cannot be worked out again
        afterwards, so the bot never tries.
        """
        landed_on = format_local_date(landed.get('delivery_date'), '%d.%m')
        lines = [f"✅ {i18n.get('staff.redispatch.success', language, date=landed_on)}"]
        release_at = landed.get('release_at')
        if release_at:
            lines.append(i18n.get(
                'staff.redispatch.success_held',
                language,
                date=format_local_date(release_at, '%d.%m'),
                time=format_local_time(datetime.fromisoformat(release_at)),
            ))
        elif landed.get('status') == DeliveryStatus.SCHEDULED.value:
            lines.append(i18n.get('staff.redispatch.success_in_pool', language))
        # A held row with no release instant (its order is still PENDING) gets
        # the date alone: nothing true can be said about when drivers see it.

        # The admin modal's three-way `customerNotice` rule, on the same fields.
        if landed.get('notifies_customer'):
            channel = landed.get('customer_channel')
            if channel == 'telegram':
                lines.append(f"📨 {i18n.get('staff.redispatch.customer_notified_telegram', language)}")
            elif channel == 'email':
                lines.append(f"📧 {i18n.get('staff.redispatch.customer_notified_email', language)}")
            else:
                # The normal case for a customer an operator created in this bot:
                # no Telegram and no email.
                lines.append(f"⚠️ {i18n.get('staff.redispatch.customer_unreachable', language)}")
        return '\n'.join(lines)

    async def _published_bounds(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, delivery_id: int, language: str
    ) -> Optional[Tuple[date, date]]:
        """The first and last day the backend allows for this delivery.

        None once the operator has been told why there are none.
        """
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return None
        async with api_client as client:
            response = await client.get_failed_delivery(token, delivery_id)
        if not response.success:
            await self._refuse_on_card(update, response, language)
            return None
        row = (response.data or {}).get('delivery') or {}
        return (
            date.fromisoformat(row['reschedule_min_date']),
            date.fromisoformat(row['reschedule_max_date']),
        )

    async def _refuse_on_card(self, update: Update, response, language: str) -> None:
        """A date-drawing tap's refusal replaces the card with its reason and no buttons.

        The delivery changed under the card (someone re-dated it, its order
        closed), so any button left on it would only be refused again. A
        failure that is not about the delivery leaves the card as it was, so
        the same tap can be repeated. Only `do_` and `pick_` come here.
        """
        if response.status_code == 401:
            await self._handle_auth_error(update, language)
        elif response.status_code in CARD_REFUSAL_STATUSES:
            await update.callback_query.edit_message_text(
                f"❌ {self._resolve_response_error(language, response, html=True)}",
                parse_mode='HTML',
            )
        else:
            await self._handle_api_response_error(update, response, language)

    @staticmethod
    async def _swap_keyboard(query, keyboard) -> None:
        """Replace only the card's keyboard (None takes it away).

        A double tap draws the same keyboard again, which Telegram refuses as
        "message is not modified". The screen is already right, so that one
        refusal is not an error.
        """
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest as exc:
            if 'not modified' not in str(exc).lower():
                raise

    @staticmethod
    def _callback_parts(data: str, prefix: str) -> List[str]:
        """The `_`-separated fields after `prefix`, digits only per the bot.py patterns."""
        return data[len(prefix):].split('_')
