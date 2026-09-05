"""
Delivery Status Update Handler for Staff Bot
Handles status transitions, delivered flow, failed flow, and cash collection.
"""
import logging
import math
from decimal import Decimal, InvalidOperation

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.utils.formatters import (
    format_delivery_status,
    format_currency,
    get_cod_cash_projection,
    format_active_delivery_summary,
    format_place_cod_lines,
    format_quantity,
    has_cash_due,
)
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.i18n import i18n
from staff_bot.utils import flow_state
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS

logger = logging.getLogger(__name__)

# Conversation states handled via the global text router in staff_bot.bot.
CASH_INPUT = 100
CASH_NOTE_INPUT = 101
RECONCILIATION_INPUT = 102
BOTTLE_RETURN_INPUT = 106


class StatusUpdateHandler(BaseHandler):
    """Handle delivery status transitions"""

    @require_auth
    @require_delivery_driver
    async def show_cash_hub(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the cash sub-menu (Reconciliation / Collect COD)."""
        language = await self._get_language(update, context)
        # The cash hub is a navigation destination, never a mid-flow step. Drop
        # any stale flow flags so an inline-Back into the hub can't leave a flow
        # armed to swallow the next text update.
        await flow_state.clear_pending_flows(context, update)
        title = f"💰 <b>{i18n.get('staff.cash.hub_title', language)}</b>"
        keyboard = MenuKeyboards.cash_hub(language)

        try:
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    title, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    title, reply_markup=keyboard, parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Error showing cash hub: {e}", exc_info=True)
            await self._handle_error(update, context)

    @staticmethod
    def _parse_amount(raw_text: str) -> float:
        """Coerce a driver-typed cash amount, fencing NON-FINITE values.

        `float(text)` returns `nan` for "nan" and `inf` for "inf"/"Infinity"/
        "1e400", and neither is caught by a sign check: `nan < 0` and `inf < 0`
        are both False. Both would then be posted as the non-standard JSON
        literals Python's json module emits AND re-parses, into
        `metadata['cash_collected']`.

        Decimal first (the same coercion the backend's SSOT money fence uses),
        `is_finite()` BEFORE any ordering comparison — Python's decimal is not
        IEEE-754, so comparing `Decimal('NaN')` RAISES — then a second
        finiteness check on the float, because `Decimal('1e400')` is perfectly
        finite until `float()` overflows it to `inf`.

        Raises ``ValueError`` (never ``InvalidOperation``) so every existing
        `except (TypeError, ValueError)` call site keeps catching it.
        """
        text = (raw_text or '').strip().replace(',', '').replace(' ', '')
        try:
            amount = Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Not a number")
        if not amount.is_finite():
            raise ValueError("Non-finite amount")
        cash_amount = float(amount)
        if not math.isfinite(cash_amount):
            raise ValueError("Amount overflows to infinity")
        if cash_amount < 0:
            raise ValueError("Negative amount")
        return cash_amount

    @staticmethod
    async def _clear_delivery_cash_flow(
        context: ContextTypes.DEFAULT_TYPE,
        update: Update = None,
    ):
        """Clear in-memory cash/reconciliation flow flags AND the Redis mirror.

        `update` is optional so existing callers that only have `context` keep
        working — but every call site should prefer to pass `update` so the
        Redis-side flow marker is also dropped and any pool-insertion
        suggestions queued while the driver was mid-flow get delivered
        immediately. The mirror is best-effort; if Redis is unreachable the
        in-memory state still clears.
        """
        context.user_data.pop('pending_delivery_cash_flow', None)
        context.user_data.pop('pending_reconciliation_flow', None)
        if update and update.effective_user:
            language = context.user_data.get('language') if context else None
            await flow_state.clear_and_drain(
                update.effective_user.id, context.bot, language=language
            )

    @staticmethod
    def _get_expected_cash_to_collect(delivery_info: dict) -> float:
        return get_cod_cash_projection(delivery_info)['expected_cash_to_collect']

    @staticmethod
    def _tapped_delivery_id(update: Update, fallback=None):
        """The delivery id trailing the callback the driver actually TAPPED.

        Every at-door callback ends in the delivery id (`staff_execute_status_
        {id}_{status}` is parsed by its own handler; `staff_bottles_full_{id}`,
        `staff_bottles_none_{id}`, `staff_collect_cash_{id}` … all end in it).
        Falls back rather than raising, because a lost `callback_query.data`
        must degrade to the flow's own id, not to a crash at the door.

        NOT the same helper as the bare `int(query.data.split('_')[-1])` in
        `view_active_delivery` / `navigate_to_address`, and deliberately so.
        This one answers "which delivery is this FLOW about" and DEGRADES to the
        id pinned when the flow started; those answer "which delivery does this
        callback name", their patterns (`^staff_navigate_` + digits + `$`)
        guarantee the id,
        and they have no flow to degrade to — a `None` there would reach
        `_anchor_current_delivery`, take its "nothing to compare" branch and
        hand back the stale snapshot, which is the wrong-customer bug itself.
        Loud is the right failure there; quiet is the right failure here. What
        both feed — the decision of which delivery to act on — has exactly one
        expression: `BaseHandler._anchor_current_delivery`.
        """
        query = getattr(update, 'callback_query', None)
        data = getattr(query, 'data', None) if query is not None else None
        try:
            return int(str(data).rsplit('_', 1)[-1])
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _order_brief(context: ContextTypes.DEFAULT_TYPE, language: str) -> str:
        """Short no-money order card prepended to status-change confirm/updated
        messages so the driver sees which order they're acting on. Returns '' if
        no current_delivery snapshot is available (renders without a brief)."""
        info = context.user_data.get('current_delivery') or {}
        if not info:
            return ''
        brief = format_active_delivery_summary(info, language, include_money=False)
        return f"{brief}\n\n" if brief else ''

    @staticmethod
    def _format_session_summary(session: dict, language: str) -> str:
        # `status` is a raw `DriverCashSessionStatus` value from the API. It used
        # to be printed verbatim, so a Russian driver read "Статус: force_closed".
        raw_status = session.get('status') or ''
        status = (
            i18n.get(f'staff.delivery.cash_session_status.{raw_status}', language)
            if raw_status
            else i18n.get('staff.common.not_available', language)
        )
        expected_cash = format_currency(session.get('expected_cash'), language=language)
        expected_on_hand = format_currency(session.get('expected_cash_on_hand'), language=language)
        declared_cash = session.get('declared_cash')
        declared_variance = format_currency(session.get('declared_variance'), language=language)
        session_age_days = session.get('session_age_days')
        lines = [
            f"🧾 <b>{i18n.get('staff.menu.cash_reconciliation', language)}</b>",
            f"{i18n.get('staff.delivery.current_status', language)}: {status}",
            f"💰 {i18n.get('staff.delivery.expected_cash_label', language)}: {expected_cash}",
            f"👛 {i18n.get('staff.delivery.expected_cash_on_hand_label', language)}: {expected_on_hand}",
        ]
        if session_age_days is not None:
            lines.append(
                i18n.get(
                    'staff.delivery.session_age_days',
                    language,
                    days=int(session_age_days or 0),
                )
            )
        if session.get('is_warning_due'):
            lines.append(i18n.get('staff.delivery.reconciliation_warning_due', language))
        if declared_cash is not None:
            lines.append(
                f"💵 {i18n.get('staff.delivery.declared_cash_label', language)}: "
                f"{format_currency(declared_cash, language=language)}"
            )
            lines.append(
                f"⚠️ {i18n.get('staff.delivery.cash_variance_label', language)}: {declared_variance}"
            )
        # Branch on the RAW value: `status` is now a localized label.
        if raw_status == 'partial':
            remaining = session.get('remaining_cash_to_submit') or 0
            lines.append(
                f"📌 {i18n.get('staff.delivery.remaining_to_submit', language)}: "
                f"{format_currency(remaining, language=language)}"
            )
        notes = session.get('notes')
        if notes:
            lines.append(f"💬 {notes}")
        risk_flags = session.get('risk_flags') or []
        if risk_flags:
            # Flags arrive as snake_case identifiers from
            # `DriverReconciliationService._build_risk_flags` and were joined
            # raw, so drivers read "Признаки риска: cash_on_hand_warning".
            flag_labels = ', '.join(
                i18n.get(f'staff.delivery.risk_flag.{flag}', language) for flag in risk_flags
            )
            lines.append(
                f"⚠️ {i18n.get('staff.delivery.risk_flags', language)}: {flag_labels}"
            )
        return '\n'.join(lines)

    async def _show_cash_collection_step(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        delivery_id: int,
        delivery_info: dict,
        language: str,
    ):
        """Draw the at-door cash screen for ``delivery_info``.

        One expression, because two callers need the identical screen: the
        "Delivered" confirm that opens the money step, and
        :meth:`_submit_delivery_completion` when a tap arrives carrying no
        figure for a door that still owes. A second hand-written copy of this
        block is how the two would drift apart on the amount they name.
        """
        cash_due_amount = self._get_expected_cash_to_collect(delivery_info)
        reserved_prepayment = float(delivery_info.get('cod_reserved_prepayment_amount') or 0)

        keyboard = DeliveryKeyboards.cash_collection_options(
            language, delivery_id, cash_due_amount
        )
        message_text = self._order_brief(context, language) + i18n.get(
            'staff.delivery.cash_collection',
            language,
            amount=format_currency(cash_due_amount, language=language),
        )
        if reserved_prepayment > 0:
            message_text += (
                f"\n💳 {i18n.get('staff.delivery.cod_prepaid_deduction', language)}: "
                f"{format_currency(reserved_prepayment, language=language)}"
            )
        # Grouped workplace: show the WHOLE place's open COD total so
        # the driver knows what is collectable at this door (spec 8).
        place_lines = format_place_cod_lines(delivery_info, language)
        if place_lines:
            message_text += "\n" + "\n".join(place_lines)

        await update.callback_query.edit_message_text(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )

    async def _submit_delivery_completion(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        delivery_id: int,
        cash_amount: float,
        notes: str = None,
    ):
        language = await self._get_language(update, context)

        # NEVER FILE AN AMOUNT NOBODY DECIDED. `submit_reconciliation_all`
        # already states this rule for the handoff screen — "an empty payload is
        # not 'no amount', it is 'server, decide the amount'... if a tap arrives
        # without a figure, redraw the screen" — and the door is the surface
        # where it actually costs money.
        #
        # A falsy `cash_amount` here means one of two very different things:
        #   * the driver deliberately collected nothing — the no-cash branch,
        #     which always carries the written reason it demands, so `notes` is
        #     set and this guard stands aside; or
        #   * the flow holding their CONFIRMED figure was cleared between the
        #     cash screen and this tap. `pending_delivery_cash_flow` is listed in
        #     `flow_state.PENDING_FLOW_USER_DATA_KEYS`, so any menu tap,
        #     `/start` or conversation escape drops it while the bottle prompt
        #     stays live on the driver's phone — no deploy required. The bottle
        #     buttons then read `flow.get('cash_amount', 0)` and filed 0 against
        #     a door that owed, with the "no cash due after COD" note attached
        #     below as if that were the finding.
        # Redraw the money step rather than guess in either direction: guessing
        # the expected amount over-credits a partial payment, and guessing 0
        # holds the driver short and erases the customer's debt.
        # tests/staff_bot/test_at_door_money_after_state_loss.py
        if not cash_amount and not notes:
            delivery_info = context.user_data.get('current_delivery') or {}
            if has_cash_due(delivery_info) and getattr(update, 'callback_query', None):
                logger.warning(
                    "Delivery %s reached completion with no cash figure while %s "
                    "is still due; redrawing the cash step instead of filing 0.",
                    delivery_id, self._get_expected_cash_to_collect(delivery_info),
                )
                await self._show_cash_collection_step(
                    update, context,
                    delivery_id=delivery_id,
                    delivery_info=delivery_info,
                    language=language,
                )
                return ConversationHandler.END
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        metadata = {'cash_collected': cash_amount}
        if notes:
            metadata['notes'] = notes
        elif cash_amount <= 0:
            metadata['notes'] = i18n.get('staff.delivery.no_cash_due_after_cod', language)

        # Include bottles_returned from the flow context
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        bottles_returned = flow.get('bottles_returned')
        if bottles_returned is not None:
            metadata['bottles_returned'] = bottles_returned

        async with api_client as client:
            response = await client.update_delivery_status(
                token, delivery_id, 'delivered', metadata=metadata
            )

        if not response.success:
            # A RETRY IS NOT A FAILURE. `StaffAPIClient._make_request` re-sends
            # only `RETRY_SAFE_METHODS` ({GET, HEAD, PUT}) after an AMBIGUOUS
            # failure, and any verb after a connect-phase one. **This PUT is in
            # that set** — deliberately, because its replay is provably zero
            # writes. So a PUT the backend COMMITTED and then failed to
            # acknowledge is still sent again — and the second attempt hits the
            # terminal-status guard
            # (`DELIVERY_STATUS_TRANSITIONS['delivered'] == []`) and 400s with
            # STAFF_INVALID_STATUS_TRANSITION. The bottles and the money both
            # landed exactly once (both writes are keyed), so rendering that as
            # a failure sends the driver back to a door with nothing left to do
            # — on an order that is already delivered and already billed — and
            # returns before the at-door flow is cleared, leaving it armed.
            # The transition the driver could reach on this screen is the one
            # THIS method just submitted, so an invalid-transition refusal here
            # means it is already recorded: acknowledge it idempotently.
            if getattr(response, 'error_code', None) != 'STAFF_INVALID_STATUS_TRANSITION':
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END
            logger.warning(
                "Delivery %s refused the 'delivered' transition (%s); treating it "
                "as an already-recorded completion and clearing the at-door flow.",
                delivery_id, getattr(response, 'error', None),
            )

        await self._clear_delivery_cash_flow(context, update)
        if context.user_data.get('current_delivery'):
            context.user_data['current_delivery']['status'] = 'delivered'

        message = (
            f"{self._order_brief(context, language)}"
            f"✅ {i18n.get('staff.delivery.delivered_success', language)}\n"
            f"💵 {i18n.get('staff.delivery.cash_recorded', language, amount=format_currency(cash_amount, language=language))}"
        )
        if notes:
            message += f"\n💬 {notes}"

        if update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML',
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML',
            )
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def initiate_status_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initiate a delivery status change (confirmation step)"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_status_{delivery_id}_{new_status}
            parts = query.data.split('_')
            delivery_id = int(parts[2])
            new_status = '_'.join(parts[3:])  # Handle statuses like 'in_transit'

            # Anchor on the TAPPED delivery before any of the money/bottle
            # figures below are read out of the snapshot.
            delivery_info = await self._anchor_current_delivery(update, context, delivery_id)
            if delivery_info is None:
                await self._refuse_stale_card(update, language)
                return

            # Store pending status change
            context.user_data['pending_status'] = {
                'delivery_id': delivery_id,
                'new_status': new_status,
            }

            # Special handling for different statuses
            if new_status == 'failed':
                # Show reason selection
                keyboard = DeliveryKeyboards.failed_reasons(language, delivery_id)
                await query.edit_message_text(
                    self._order_brief(context, language)
                    + i18n.get('staff.delivery.select_fail_reason', language),
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                return

            if new_status == 'delivered':
                cash_due_amount = self._get_expected_cash_to_collect(delivery_info)
                reserved_prepayment = float(delivery_info.get('cod_reserved_prepayment_amount') or 0)

                # RAIL-AGNOSTIC (plan 2026-08-08-open-receivable-ssot).
                #
                # The gate used to be `(payment_method == 'cash' and due > 0) or
                # is_unsettled_electronic(...)`, and it then OVERRODE the amount
                # with the full `total_amount` on the stated grounds that
                # "outstanding_amount / total_amount are equivalent here (no
                # partial cash collected yet)". That premise is FALSE for an
                # order edited upward after an online settlement — the customer
                # has already paid most of it — so the override is deleted.
                #
                # The amount now comes from `_get_expected_cash_to_collect` ONLY,
                # which is the same seam that feeds the submitted
                # `cash_collected` in `_complete_delivery_with_cash`. One call,
                # so what the driver is shown and what is recorded cannot diverge.
                if has_cash_due(delivery_info):
                    await self._show_cash_collection_step(
                        update, context,
                        delivery_id=delivery_id,
                        delivery_info=delivery_info,
                        language=language,
                    )
                    return

            # Standard confirmation for other statuses
            status_text = format_delivery_status(new_status, language)
            text = (
                self._order_brief(context, language)
                + i18n.get('staff.delivery.confirm_status', language, status=status_text)
            )

            keyboard = CommonKeyboards.confirm_cancel(
                language,
                confirm_data=f"staff_execute_status_{delivery_id}_{new_status}",
                cancel_data=f"staff_view_active_{delivery_id}"
            )

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error initiating status change: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def execute_status_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Execute the confirmed status change"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_execute_status_{delivery_id}_{new_status}
            parts = query.data.split('_')
            delivery_id = int(parts[3])
            new_status = '_'.join(parts[4:])

            # The bottle prompt below is anchored on the snapshot, and
            # `_order_brief` titles every screen from it — so both must be the
            # TAPPED delivery's, not whichever card was opened last.
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(update, language)
                return

            # For non-cash delivered orders: check for returnable bottles first
            if new_status == 'delivered':
                expected_bottles = self._get_expected_bottles(context)
                if expected_bottles > 0:
                    # Set up flow context and show bottle prompt
                    context.user_data['pending_delivery_cash_flow'] = {
                        'delivery_id': delivery_id,
                        'cash_amount': 0,
                        'flow_type': 'non_cash_delivered',
                        'awaiting_bottle_count': False,
                    }
                    keyboard, message = self._build_bottle_prompt(language, delivery_id, context)
                    await query.edit_message_text(
                        f"{self._order_brief(context, language)}{message}",
                        reply_markup=keyboard, parse_mode='HTML'
                    )
                    return

            metadata = context.user_data.pop('status_metadata', {})

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, new_status, metadata
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            # Success message
            status_text = format_delivery_status(new_status, language)
            success_msg = i18n.get('staff.delivery.status_updated', language, status=status_text)

            # Update current_delivery context
            if context.user_data.get('current_delivery'):
                context.user_data['current_delivery']['status'] = new_status

            if new_status in ('delivered', 'failed'):
                # Terminal status - go back to active deliveries
                keyboard = CommonKeyboards.back_button(language, "staff_active_deliveries")
            else:
                # Non-terminal - show delivery actions again
                keyboard = DeliveryKeyboards.active_delivery_actions(
                    language, delivery_id, new_status
                )

            await query.edit_message_text(
                f"{self._order_brief(context, language)}✅ {success_msg}",
                reply_markup=keyboard,
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error executing status change: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def select_fail_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle failed delivery reason selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_failed_reason_{delivery_id}_{reason}
            parts = query.data.split('_')
            delivery_id = int(parts[3])
            reason = '_'.join(parts[4:])

            # The confirmation below is titled from the snapshot
            # (`_order_brief`) and stamps `status='failed'` into it, so the
            # snapshot must be the TAPPED delivery's -- otherwise a reason
            # picked on an older card tells the driver a live order failed and
            # corrupts the open stop's cached card.
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(update, language)
                return

            async with api_client as client:
                response = await client.update_delivery_status(
                    token, delivery_id, 'failed',
                    metadata={'fail_reason': reason}
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            reason_text = i18n.get(f'staff.delivery.reason.{reason}', language)
            if context.user_data.get('current_delivery'):
                context.user_data['current_delivery']['status'] = 'failed'
            await query.edit_message_text(
                f"{self._order_brief(context, language)}"
                f"❌ {i18n.get('staff.delivery.marked_failed', language)}\n"
                f"{i18n.get('staff.delivery.fail_reason_label', language)}: {reason_text}",
                reply_markup=CommonKeyboards.back_button(language, "staff_active_deliveries"),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error selecting fail reason: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_full_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete delivery and record the full cash amount."""
        query = update.callback_query
        await query.answer()

        try:
            delivery_id = int(query.data.split('_')[-1])
            delivery_info = await self._anchor_current_delivery(update, context, delivery_id)
            if delivery_info is None:
                await self._refuse_stale_card(
                    update, await self._get_language(update, context)
                )
                return
            cash_amount = self._get_expected_cash_to_collect(delivery_info)
            await self._maybe_show_bottle_prompt_or_submit(
                update,
                context,
                delivery_id=delivery_id,
                cash_amount=cash_amount,
            )
        except Exception as e:
            logger.error(f"Error confirming cash collection: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_partial_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for a partial cash amount before delivery completion."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_id = int(query.data.split('_')[-1])
            # The typed amount is submitted against THIS delivery, and the
            # bottle prompt that follows reads the snapshot — anchor first.
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(update, language)
                return
            # F-2: set the flag *before* the prompt render and clear it on
            # render failure. Otherwise an `edit_message_text` exception
            # (network blip, message-too-old, parse error) leaves
            # `pending_delivery_cash_flow` set with no UI to drive it — the
            # next text the user sends gets parsed as cash for an order
            # they never confirmed they were collecting on.
            context.user_data['pending_delivery_cash_flow'] = {
                'delivery_id': delivery_id,
                'flow_type': 'partial',
            }
            # C-2: mirror the active-flow marker into Redis so the webhook
            # server's pool_insertion_suggestion_handler defers any inbound
            # Accept-keyboard mid-flow instead of letting it interrupt the
            # cash-amount prompt the user is about to answer.
            await flow_state.mark_active(
                update.effective_user.id, 'pending_delivery_cash_flow'
            )
            try:
                await query.edit_message_text(
                    i18n.get('staff.delivery.enter_cash_amount', language),
                    reply_markup=CommonKeyboards.flow_cancel(language),
                    parse_mode='HTML'
                )
            except Exception:
                context.user_data.pop('pending_delivery_cash_flow', None)
                await flow_state.clear_active(update.effective_user.id)
                raise

            return CASH_INPUT

        except Exception as e:
            logger.error(f"Error editing cash amount: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_no_cash_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for the required note when no cash was collected."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            delivery_id = int(query.data.split('_')[-1])
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(update, language)
                return
            context.user_data['pending_delivery_cash_flow'] = {
                'delivery_id': delivery_id,
                'flow_type': 'none',
                'cash_amount': 0.0,
            }
            await flow_state.mark_active(
                update.effective_user.id, 'pending_delivery_cash_flow'
            )
            try:
                await query.edit_message_text(
                    i18n.get('staff.delivery.enter_no_cash_reason', language),
                    reply_markup=CommonKeyboards.flow_cancel(language),
                    parse_mode='HTML',
                )
            except Exception:
                # F-2: clear the flag if the prompt couldn't render — see
                # start_partial_cash_collection above for rationale.
                context.user_data.pop('pending_delivery_cash_flow', None)
                await flow_state.clear_active(update.effective_user.id)
                raise
            return CASH_NOTE_INPUT

        except Exception as e:
            logger.error(f"Error starting zero-cash delivery flow: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_cash_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive a partial cash amount and then require an audit note."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        if flow.get('flow_type') != 'partial':
            return ConversationHandler.END

        try:
            try:
                cash_amount = self._parse_amount(update.message.text)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return CASH_INPUT

            # Over-collection is allowed: a customer may pay down older debt
            # at the door, especially grocery stores. The backend allocates
            # to the current order first then to oldest open payments; any
            # remainder lands on the customer's contract as credit.
            if cash_amount <= 0:
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return CASH_INPUT

            flow['cash_amount'] = cash_amount
            context.user_data['pending_delivery_cash_flow'] = flow
            await update.message.reply_text(
                i18n.get('staff.delivery.enter_partial_cash_reason', language),
                reply_markup=CommonKeyboards.flow_cancel(language),
                parse_mode='HTML',
            )
            return CASH_NOTE_INPUT

        except Exception as e:
            logger.error(f"Error receiving cash amount: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def receive_cash_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive the required note for a partial or zero-cash delivery completion."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        if flow.get('flow_type') not in {'partial', 'none'}:
            return ConversationHandler.END

        try:
            note = (update.message.text or '').strip()
            if not note:
                await update.message.reply_text(
                    i18n.get('staff.delivery.note_required', language)
                )
                return CASH_NOTE_INPUT

            delivery_id = flow.get('delivery_id')
            cash_amount = flow.get('cash_amount', 0.0)
            return await self._maybe_show_bottle_prompt_or_submit(
                update,
                context,
                delivery_id=delivery_id,
                cash_amount=cash_amount,
                notes=note,
            )

        except Exception as e:
            logger.error(f"Error receiving cash collection note: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def show_reconciliation_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the driver's open reconciliation session."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_reconciliation_session(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            session = response.data or {}
            text = self._format_session_summary(session, language)
            keyboard = DeliveryKeyboards.reconciliation_actions(
                language,
                can_submit=session.get('status') in {'open', 'partial', 'overdue'},
                remaining_amount=session.get('remaining_cash_to_submit'),
            )

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error showing reconciliation session: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_reconciliation_submit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to enter the counted cash for reconciliation."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        context.user_data['pending_reconciliation_flow'] = {'action': 'submit'}
        await flow_state.mark_active(
            update.effective_user.id, 'pending_reconciliation_flow'
        )
        try:
            await query.edit_message_text(
                i18n.get('staff.delivery.enter_declared_cash', language),
                reply_markup=CommonKeyboards.flow_cancel(language),
                parse_mode='HTML',
            )
        except Exception:
            # F-2: a failed prompt render must not leave the flag set with no UI
            # to drive it — the next text would be parsed as reconciliation cash.
            context.user_data.pop('pending_reconciliation_flow', None)
            await flow_state.clear_active(update.effective_user.id)
            raise
        return RECONCILIATION_INPUT

    @require_auth
    @require_delivery_driver
    async def submit_reconciliation_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Hand off EXACTLY the amount the tapped button displayed.

        ONE DECISION (sweep #8). The figure travels with the tap: it was frozen
        into the callback by ``DeliveryKeyboards.reconciliation_actions`` at the
        moment the screen was drawn, and it is posted verbatim, so the
        cash-custody record and the button the driver read carry the same
        number by construction.

        This used to post ``{}``. An empty payload is not "no amount" — it is
        "server, decide the amount", and the server decides it from live
        ``CashCollectionEvent``s at tap time. One COD collection completing
        between the render and the tap therefore wrote a handoff the driver had
        never seen and never agreed to (measured: shown 120,000, recorded
        150,000), with no second confirmation step. Never post an amount-less
        handoff again: if a tap arrives without a figure, redraw the screen so
        the driver can confirm a fresh one.

        The remainder is not lost when a collection lands in the gap — it stays
        on the session and appears on the next screen, named, on a button the
        driver can read before tapping.
        """
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        frozen_amount = DeliveryKeyboards.parse_handoff_callback(getattr(query, 'data', None))
        if frozen_amount is None:
            # The tap carried no figure (a button rendered before the amount was
            # frozen into the callback, or a malformed payload). Writing here
            # would mean handing the amount decision to the server — the defect
            # itself. Redraw instead; the new button names what it will record.
            await self.show_reconciliation_session(update, context)
            return

        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.submit_reconciliation_session(
                    token, {'declared_cash': float(frozen_amount)}
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            await self._clear_delivery_cash_flow(context, update)
            session = response.data or {}
            title_key = (
                'staff.delivery.reconciliation_partial_recorded'
                if session.get('status') == 'partial'
                else 'staff.delivery.reconciliation_submitted'
            )
            message = (
                f"✅ {i18n.get(title_key, language)}"
                f"\n\n{self._format_session_summary(session, language)}"
            )
            await query.edit_message_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_back_to_main"),
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"Error submitting full reconciliation session: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_reconciliation_declared_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Submit the declared driver cash amount for reconciliation."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        if not context.user_data.get('pending_reconciliation_flow'):
            return ConversationHandler.END

        try:
            try:
                declared_cash = self._parse_amount(update.message.text)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_amount', language)
                )
                return RECONCILIATION_INPUT

            async with api_client as client:
                response = await client.submit_reconciliation_session(
                    token,
                    {'declared_cash': declared_cash},
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            await self._clear_delivery_cash_flow(context, update)
            session = response.data or {}
            title_key = (
                'staff.delivery.reconciliation_partial_recorded'
                if session.get('status') == 'partial'
                else 'staff.delivery.reconciliation_submitted'
            )
            success_title = i18n.get(title_key, language)
            message = f"✅ {success_title}\n\n{self._format_session_summary(session, language)}"
            await update.message.reply_text(
                message,
                reply_markup=CommonKeyboards.back_button(language, "staff_back_to_main"),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error submitting reconciliation session: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    async def mark_preparing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mark an order as preparing"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_mark_preparing_{order_id}
            order_id = int(query.data.split('_')[-1])

            async with api_client as client:
                response = await client.mark_order_preparing(token, order_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            await query.edit_message_text(
                f"✅ {i18n.get('staff.delivery.marked_preparing', language)}",
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error marking as preparing: {e}", exc_info=True)
            await self._handle_error(update, context)

    # ------------------------------------------------------------------
    # Returnable bottle return step (inserted between cash and submission)
    # ------------------------------------------------------------------

    def _get_expected_bottles(self, context: ContextTypes.DEFAULT_TYPE) -> float:
        """Get expected returnable bottles from the current delivery context."""
        delivery_info = context.user_data.get('current_delivery', {})
        return float(delivery_info.get('expected_returnable_bottles', 0))

    def _get_suggested_return_count(self, context: ContextTypes.DEFAULT_TYPE):
        """Suggested bottles-returned count = the empties standing at this PLACE.

        The place is the address group when the delivery address is grouped,
        else the address itself — so at a shared workplace this includes a
        coworker's empties, and the driver may legitimately be handed more than
        this customer ever received. The backend CLAMPS it at 0
        (`_customer_bottle_balance`) so "All N returned" can never offer a
        negative count; `_get_place_signed_balance` is the unclamped companion.

        This is the prompt's anchor and the value submitted when the driver taps
        "All returned". Distinct from `_get_expected_bottles`, which only GATES
        whether the prompt appears (based on this order's returnable quantity).

        NOT `int(float(...))`: truncation toward zero turned a place holding
        0 < b < 1 into a suggestion of 0, and `_build_bottle_prompt` then found
        `signed >= 0` and announced "no empties are on record for this
        customer" — factually wrong, and the exact mirror of the bug the
        over-returned arm was added to fix. Integral balances still come back as
        `int` so the prompt and the keyboard read "All 4 returned", never
        "All 4.0 returned"; only a real fraction survives as one.
        """
        delivery_info = context.user_data.get('current_delivery', {})
        balance = float(delivery_info.get('customer_bottle_balance', 0) or 0)
        if balance <= 0:
            return 0
        return int(balance) if balance.is_integer() else balance

    def _get_place_signed_balance(self, context: ContextTypes.DEFAULT_TYPE) -> float:
        """The place's SIGNED balance; negative means over-returned.

        Additional to the clamped anchor, never a replacement for it. Absent on
        a delivery snapshot taken before the backend field shipped, which reads
        as 0 — i.e. today's behaviour.
        """
        delivery_info = context.user_data.get('current_delivery', {})
        return float(delivery_info.get('place_bottle_balance_signed', 0) or 0)

    def _build_bottle_prompt(self, language: str, delivery_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple[InlineKeyboardMarkup, str]:
        """Build (keyboard, message) for the bottle-return prompt, anchored on the
        PLACE's current bottle balance.

        Three states, not two: an over-returned place used to be told "no
        empties are on record for this customer yet", which is factually wrong —
        there IS a record and it is negative.
        """
        suggested = self._get_suggested_return_count(context)
        keyboard = DeliveryKeyboards.bottle_return_options(language, delivery_id, suggested)
        if suggested > 0:
            message = i18n.get(
                'staff.delivery.bottles_return_prompt', language, balance=suggested
            )
        else:
            signed = self._get_place_signed_balance(context)
            if signed < 0:
                message = i18n.get(
                    'staff.delivery.bottles_return_prompt_over_returned', language,
                    count=format_quantity(abs(signed)),
                )
            else:
                message = i18n.get(
                    'staff.delivery.bottles_return_prompt_no_balance', language
                )
        return keyboard, message

    async def _maybe_show_bottle_prompt_or_submit(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        delivery_id: int,
        cash_amount: float,
        notes: str = None,
    ):
        """Check if order has returnable bottles. If yes, show bottle prompt.
        Otherwise proceed directly to delivery completion."""
        expected_bottles = self._get_expected_bottles(context)
        if expected_bottles > 0:
            # Store cash info for later submission
            flow = context.user_data.get('pending_delivery_cash_flow') or {}
            flow['delivery_id'] = delivery_id
            flow['cash_amount'] = cash_amount
            flow['cash_notes'] = notes
            flow['awaiting_bottle_count'] = False
            context.user_data['pending_delivery_cash_flow'] = flow

            language = await self._get_language(update, context)
            keyboard, message = self._build_bottle_prompt(language, delivery_id, context)
            message = f"{self._order_brief(context, language)}{message}"
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    message, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    message, reply_markup=keyboard, parse_mode='HTML'
                )
            return
        else:
            # No returnable bottles — submit directly
            return await self._submit_delivery_completion(
                update, context,
                delivery_id=delivery_id,
                cash_amount=cash_amount,
                notes=notes,
            )

    @require_auth
    @require_delivery_driver
    async def confirm_full_bottle_return(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """All expected bottles returned — proceed to delivery completion."""
        query = update.callback_query
        await query.answer()
        try:
            flow = context.user_data.get('pending_delivery_cash_flow') or {}
            # The anchor submitted here IS the place balance read off the
            # snapshot, so the snapshot must belong to the tapped card.
            delivery_id = self._tapped_delivery_id(update, flow.get('delivery_id'))
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(
                    update, await self._get_language(update, context)
                )
                return
            flow['delivery_id'] = delivery_id
            flow['bottles_returned'] = self._get_suggested_return_count(context)
            context.user_data['pending_delivery_cash_flow'] = flow

            return await self._submit_delivery_completion(
                update, context,
                delivery_id=delivery_id,
                cash_amount=flow.get('cash_amount', 0),
                notes=flow.get('cash_notes'),
            )
        except Exception as e:
            logger.error(f"Error confirming full bottle return: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_custom_bottle_return(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt driver to enter custom bottle count."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        # The typed count is submitted for THIS delivery, and `receive_bottle_count`
        # has no callback of its own to re-derive it from — pin it here.
        delivery_id = self._tapped_delivery_id(update, flow.get('delivery_id'))
        if await self._anchor_current_delivery(update, context, delivery_id) is None:
            await self._refuse_stale_card(update, language)
            return ConversationHandler.END
        flow['delivery_id'] = delivery_id
        flow['awaiting_bottle_count'] = True
        context.user_data['pending_delivery_cash_flow'] = flow

        await query.edit_message_text(
            i18n.get('staff.delivery.enter_bottle_count', language),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML'
        )
        return BOTTLE_RETURN_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_bottle_count(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive typed bottle count from driver."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_delivery_cash_flow') or {}
        if not flow.get('awaiting_bottle_count'):
            return ConversationHandler.END

        try:
            text = update.message.text.strip()
            try:
                count = int(text)
            except (TypeError, ValueError):
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_bottle_count', language)
                )
                return BOTTLE_RETURN_INPUT

            if count < 0:
                await update.message.reply_text(
                    i18n.get('staff.delivery.invalid_bottle_count', language)
                )
                return BOTTLE_RETURN_INPUT

            flow['bottles_returned'] = count
            flow['awaiting_bottle_count'] = False
            context.user_data['pending_delivery_cash_flow'] = flow

            return await self._submit_delivery_completion(
                update, context,
                delivery_id=flow['delivery_id'],
                cash_amount=flow.get('cash_amount', 0),
                notes=flow.get('cash_notes'),
            )
        except Exception as e:
            logger.error(f"Error receiving bottle count: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def skip_bottle_return(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """No bottles returned — proceed to delivery completion."""
        query = update.callback_query
        await query.answer()
        try:
            flow = context.user_data.get('pending_delivery_cash_flow') or {}
            delivery_id = self._tapped_delivery_id(update, flow.get('delivery_id'))
            if await self._anchor_current_delivery(update, context, delivery_id) is None:
                await self._refuse_stale_card(
                    update, await self._get_language(update, context)
                )
                return
            flow['delivery_id'] = delivery_id
            flow['bottles_returned'] = 0
            context.user_data['pending_delivery_cash_flow'] = flow

            return await self._submit_delivery_completion(
                update, context,
                delivery_id=delivery_id,
                cash_amount=flow.get('cash_amount', 0),
                notes=flow.get('cash_notes'),
            )
        except Exception as e:
            logger.error(f"Error skipping bottle return: {e}", exc_info=True)
            await self._handle_error(update, context)
